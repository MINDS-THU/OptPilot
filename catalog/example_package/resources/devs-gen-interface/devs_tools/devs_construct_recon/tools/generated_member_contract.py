"""Static contract checks for generated model-member references.

The generator builds one model at a time, so Python's normal compiler cannot
detect a misspelled attribute on a child model.  This module uses the exact
``generated_interface`` records in the model registry to catch the narrow case
we *can* prove is wrong without executing generated code: a reference that
walks through a known child binding and then names a member that child does not
declare.

Unknown roots, dynamic indexing, and attributes whose type is not described by
the registry are intentionally ignored.  The check is therefore a guardrail,
not a general Python type checker.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any


@dataclass(frozen=True)
class GeneratedMemberViolation:
    """One statically proven reference to an undeclared child member."""

    expression: str
    owner_class: str
    member: str
    line: int
    column: int
    declared_members: tuple[str, ...]

    def describe(self) -> str:
        suggestion = get_close_matches(
            self.member,
            self.declared_members,
            n=3,
            cutoff=0.45,
        )
        if suggestion:
            available = f" Closest declared name(s): {', '.join(suggestion)}."
        elif self.declared_members:
            preview = ", ".join(self.declared_members[:12])
            suffix = " ..." if len(self.declared_members) > 12 else ""
            available = f" Declared public names: {preview}{suffix}."
        else:
            available = " The generated class declares no public members."
        return (
            f"line {self.line}: {self.expression!r} references undeclared member "
            f"{self.owner_class}.{self.member}.{available}"
        )


class GeneratedMemberContractError(ValueError):
    """Raised when generated code contradicts an exact registry interface."""

    def __init__(self, violations: Iterable[GeneratedMemberViolation], filename: str):
        self.violations = tuple(violations)
        details = "\n".join(f"- {item.describe()}" for item in self.violations)
        super().__init__(
            "Generated member contract failed for "
            f"{filename}. Use the exact generated_interface names from the registry:\n"
            f"{details}"
        )


@dataclass(frozen=True)
class _Interface:
    members: frozenset[str]
    children: Mapping[str, str]


@dataclass(frozen=True)
class _Resolved:
    class_name: str
    traversed_child: bool = False


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _normalize_registry(registry: Any) -> dict[str, _Interface]:
    """Accept either the saved mapping, its list snapshot, or model objects."""

    registry = _plain(registry)
    if isinstance(registry, Mapping):
        if "flat_registry_view" in registry:
            registry = _plain(registry.get("flat_registry_view"))
        if isinstance(registry, Mapping) and "class_name" in registry:
            entries = [registry]
        elif isinstance(registry, Mapping):
            entries = list(registry.values())
        else:
            entries = []
    elif isinstance(registry, list):
        entries = registry
    else:
        entries = []

    result: dict[str, _Interface] = {}
    for raw_entry in entries:
        entry = _plain(raw_entry)
        if not isinstance(entry, Mapping):
            continue
        class_name = entry.get("class_name")
        raw_interface = _plain(entry.get("generated_interface"))
        if not isinstance(class_name, str) or not isinstance(raw_interface, Mapping):
            continue
        children = _plain(raw_interface.get("child_instances", {}))
        if not isinstance(children, Mapping):
            children = {}
        exact_children = {
            name: child_class
            for name, child_class in children.items()
            if isinstance(name, str) and isinstance(child_class, str)
        }
        members: set[str] = set(exact_children)
        for field in ("instance_attributes", "properties", "public_methods"):
            names = _plain(raw_interface.get(field, []))
            if isinstance(names, (list, tuple, set, frozenset)):
                members.update(name for name in names if isinstance(name, str))
        result[class_name] = _Interface(
            members=frozenset(members),
            children=exact_children,
        )
    return result


def _attribute_chain(node: ast.AST) -> tuple[str, tuple[str, ...]] | None:
    attributes: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        attributes.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    return current.id, tuple(reversed(attributes))


def _call_leaf_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _ScopeCollector(ast.NodeVisitor):
    """Collect one lexical scope without leaking aliases into nested scopes."""

    def __init__(self) -> None:
        self.assignments: dict[str, list[ast.AST]] = {}
        self.attributes: list[ast.Attribute] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.attributes.append(node)
        self.generic_visit(node)

    def _record_assignment(self, target: ast.AST, value: ast.AST | None) -> None:
        if isinstance(target, ast.Name) and value is not None:
            self.assignments.setdefault(target.id, []).append(value)
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._record_assignment(element, None)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_assignment(target, node.value)
            self.visit(target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_assignment(node.target, node.value)
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._record_assignment(node.target, node.value)
        self.visit(node.target)
        self.visit(node.value)


def _scope_contents(nodes: Iterable[ast.stmt]) -> _ScopeCollector:
    collector = _ScopeCollector()
    for node in nodes:
        collector.visit(node)
    return collector


def _resolve_child_expression(
    expression: ast.AST,
    aliases: Mapping[str, _Resolved],
    interfaces: Mapping[str, _Interface],
    root_class_name: str,
) -> _Resolved | None:
    if isinstance(expression, ast.Call):
        return (
            _Resolved(root_class_name)
            if _call_leaf_name(expression.func) == root_class_name
            else None
        )
    chain = _attribute_chain(expression)
    if chain is None:
        if isinstance(expression, ast.Name):
            return aliases.get(expression.id)
        return None
    base_name, attributes = chain
    resolved = aliases.get(base_name)
    if resolved is None:
        return None
    current_class = resolved.class_name
    traversed_child = resolved.traversed_child
    for attribute in attributes:
        interface = interfaces.get(current_class)
        if interface is None:
            return None
        current_class = interface.children.get(attribute)
        if current_class is None:
            return None
        traversed_child = True
    return _Resolved(current_class, traversed_child)


def _resolve_aliases(
    collector: _ScopeCollector,
    interfaces: Mapping[str, _Interface],
    root_class_name: str,
    seeds: Mapping[str, _Resolved],
) -> dict[str, _Resolved]:
    aliases = dict(seeds)
    # A generated runner uses straight-line assignments. Requiring exactly one
    # assignment keeps the analysis conservative when control flow or rebinding
    # makes a name ambiguous.
    pending = {
        name: values[0]
        for name, values in collector.assignments.items()
        if len(values) == 1 and name not in aliases
    }
    changed = True
    while changed:
        changed = False
        for name, expression in tuple(pending.items()):
            resolved = _resolve_child_expression(
                expression,
                aliases,
                interfaces,
                root_class_name,
            )
            if resolved is None:
                continue
            aliases[name] = resolved
            pending.pop(name)
            changed = True
    return aliases


def _check_attribute(
    node: ast.Attribute,
    aliases: Mapping[str, _Resolved],
    interfaces: Mapping[str, _Interface],
    source: str,
) -> GeneratedMemberViolation | None:
    chain = _attribute_chain(node)
    if chain is None:
        return None
    base_name, attributes = chain
    resolved = aliases.get(base_name)
    if resolved is None:
        return None
    current_class = resolved.class_name
    traversed_child = resolved.traversed_child
    for attribute in attributes:
        interface = interfaces.get(current_class)
        if interface is None:
            return None
        child_class = interface.children.get(attribute)
        if child_class is not None:
            current_class = child_class
            traversed_child = True
            continue
        if not traversed_child:
            # The registry cannot describe the type of an arbitrary root
            # attribute. Do not guess and do not reject it.
            return None
        if attribute in interface.members:
            # This is a known scalar/property/method, but the registry does not
            # describe its return type. Any further chain is intentionally left
            # to runtime validation.
            return None
        expression = ast.get_source_segment(source, node) or (
            f"{base_name}." + ".".join(attributes)
        )
        return GeneratedMemberViolation(
            expression=expression,
            owner_class=current_class,
            member=attribute,
            line=getattr(node, "lineno", 0),
            column=getattr(node, "col_offset", 0),
            declared_members=tuple(sorted(interface.members)),
        )
    return None


def find_generated_member_violations(
    source: str,
    registry: Any,
    root_class_name: str,
    *,
    filename: str = "<generated_code>",
) -> tuple[GeneratedMemberViolation, ...]:
    """Return only member mismatches proven by registry child bindings."""

    interfaces = _normalize_registry(registry)
    root_interface = interfaces.get(root_class_name)
    if root_interface is None or not root_interface.children:
        return ()
    module = ast.parse(source, filename=filename)
    findings: list[GeneratedMemberViolation] = []
    seen: set[tuple[int, int, str, str]] = set()

    def inspect_scope(
        nodes: Iterable[ast.stmt],
        seeds: Mapping[str, _Resolved],
    ) -> None:
        collector = _scope_contents(nodes)
        aliases = _resolve_aliases(
            collector,
            interfaces,
            root_class_name,
            seeds,
        )
        for attribute in collector.attributes:
            violation = _check_attribute(attribute, aliases, interfaces, source)
            if violation is None:
                continue
            key = (
                violation.line,
                violation.column,
                violation.owner_class,
                violation.member,
            )
            if key not in seen:
                seen.add(key)
                findings.append(violation)

    inspect_scope(module.body, {})
    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef) and node.name == root_class_name:
            for member in node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                positional = [*member.args.posonlyargs, *member.args.args]
                seeds = (
                    {positional[0].arg: _Resolved(root_class_name)}
                    if positional
                    and not any(
                        _call_leaf_name(decorator) == "staticmethod"
                        for decorator in member.decorator_list
                    )
                    else {}
                )
                inspect_scope(member.body, seeds)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Root-class methods were inspected above with their ``self`` seed.
            parent_is_root_method = any(
                isinstance(parent, ast.ClassDef)
                and parent.name == root_class_name
                and node in parent.body
                for parent in ast.walk(module)
            )
            if not parent_is_root_method:
                inspect_scope(node.body, {})

    return tuple(sorted(findings, key=lambda item: (item.line, item.column)))


def require_generated_member_contract(
    source: str,
    registry: Any,
    root_class_name: str,
    *,
    filename: str = "<generated_code>",
) -> None:
    """Raise when source uses a provably undeclared generated child member."""

    violations = find_generated_member_violations(
        source,
        registry,
        root_class_name,
        filename=filename,
    )
    if violations:
        raise GeneratedMemberContractError(violations, filename)
