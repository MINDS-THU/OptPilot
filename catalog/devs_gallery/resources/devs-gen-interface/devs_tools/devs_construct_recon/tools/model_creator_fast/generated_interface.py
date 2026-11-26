"""Deterministic extraction of generated Python model interfaces."""

from __future__ import annotations

import ast
from collections.abc import Iterable

from ...base_types import GeneratedPythonInterface


def _is_direct_self_attribute(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _iter_assignment_targets(node: ast.AST):
    if isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            yield from _iter_assignment_targets(element)
        return
    yield node


def _callable_leaf_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _MethodAssignmentVisitor(ast.NodeVisitor):
    """Visit a method body without leaking into nested scopes."""

    def __init__(self, import_aliases: dict[str, str]) -> None:
        self.instance_attributes: set[str] = set()
        self.call_bindings: dict[str, set[str]] = {}
        self.import_aliases = import_aliases
        self.local_instance_bindings: dict[str, set[str]] = {}
        self.local_callable_aliases: dict[str, str] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def _resolve_callable_name(self, node: ast.AST) -> str | None:
        name = _callable_leaf_name(node)
        if not name:
            return None
        return self.local_callable_aliases.get(name, self.import_aliases.get(name, name))

    def _constructed_classes(self, value: ast.AST | None) -> set[str]:
        """Resolve common, statically unambiguous child construction forms."""
        if value is None:
            return set()
        if isinstance(value, ast.Call):
            called_name = self._resolve_callable_name(value.func)
            return {called_name} if called_name else set()
        if isinstance(value, ast.Name):
            return set(self.local_instance_bindings.get(value.id, ()))
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            return set().union(*(self._constructed_classes(item) for item in value.elts))
        if isinstance(value, ast.Dict):
            return set().union(*(self._constructed_classes(item) for item in value.values))
        if isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return self._constructed_classes(value.elt)
        if isinstance(value, ast.DictComp):
            return self._constructed_classes(value.value)
        if isinstance(value, ast.IfExp):
            return self._constructed_classes(value.body) | self._constructed_classes(value.orelse)
        return set()

    def _record_target(self, target: ast.AST, value: ast.AST | None = None) -> None:
        for candidate in _iter_assignment_targets(target):
            if not _is_direct_self_attribute(candidate):
                continue
            name = candidate.attr
            if name.startswith("_"):
                continue
            self.instance_attributes.add(name)
            called_names = self._constructed_classes(value)
            if called_names:
                self.call_bindings.setdefault(name, set()).update(called_names)

    def _record_local_target(self, target: ast.AST, value: ast.AST) -> None:
        if not isinstance(target, ast.Name):
            return
        constructed_classes = self._constructed_classes(value)
        callable_name = (
            self._resolve_callable_name(value)
            if isinstance(value, (ast.Name, ast.Attribute))
            else None
        )
        # Respect reassignment order so a stale earlier child binding cannot be
        # attributed to an unrelated value later in the same method.
        self.local_instance_bindings.pop(target.id, None)
        self.local_callable_aliases.pop(target.id, None)
        if constructed_classes:
            self.local_instance_bindings[target.id] = constructed_classes
        elif callable_name:
            self.local_callable_aliases[target.id] = callable_name

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_target(target, node.value)
            self._record_local_target(target, node.value)
        self.generic_visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_target(node.target, node.value)
        if node.value is not None:
            self._record_local_target(node.target, node.value)
            self.generic_visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_target(node.target, node.value)
        self.generic_visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        # Recognize homogeneous child collections populated after assignment:
        # self.workers.append(Worker(...)) / self.workers.extend([...]).
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"append", "extend"}
            and _is_direct_self_attribute(func.value)
            and node.args
        ):
            attribute = func.value.attr
            if not attribute.startswith("_"):
                self.instance_attributes.add(attribute)
                called_names = self._constructed_classes(node.args[0])
                if called_names:
                    self.call_bindings.setdefault(attribute, set()).update(called_names)
        self.generic_visit(node)


def _module_import_aliases(module: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in module.body:
        if not isinstance(statement, ast.ImportFrom):
            continue
        for imported in statement.names:
            if imported.name == "*":
                continue
            aliases[imported.asname or imported.name] = imported.name
    return aliases


def _is_property(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_callable_leaf_name(decorator) == "property" for decorator in method.decorator_list)


def extract_generated_python_interface(
    source: str,
    class_name: str,
    *,
    filename: str = "<generated_model>",
    child_class_names: Iterable[str] | None = None,
) -> GeneratedPythonInterface:
    """Extract the exact public surface declared directly by ``class_name``.

    This intentionally does not infer domain semantics. It records only syntax
    present in the generated source so later generators can avoid inventing
    state names from prose descriptions.
    """

    module = ast.parse(source, filename=filename)
    matching_classes = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if len(matching_classes) != 1:
        raise ValueError(
            f"Expected exactly one generated class named {class_name!r} in {filename}; "
            f"found {len(matching_classes)}."
        )

    target_class = matching_classes[0]
    import_aliases = _module_import_aliases(module)
    instance_attributes: set[str] = set()
    call_bindings: dict[str, set[str]] = {}
    properties: set[str] = set()
    public_methods: set[str] = set()

    for member in target_class.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_property(member):
            if not member.name.startswith("_"):
                properties.add(member.name)
        elif not member.name.startswith("_"):
            public_methods.add(member.name)
        visitor = _MethodAssignmentVisitor(import_aliases)
        for statement in member.body:
            visitor.visit(statement)
        instance_attributes.update(visitor.instance_attributes)
        for attribute, called_names in visitor.call_bindings.items():
            call_bindings.setdefault(attribute, set()).update(called_names)

    known_child_classes = set(child_class_names) if child_class_names is not None else None
    child_instances = {
        attribute: next(iter(called_names))
        for attribute, called_names in sorted(call_bindings.items())
        if len(called_names) == 1
        and (
            known_child_classes is None
            or next(iter(called_names)) in known_child_classes
        )
    }
    return GeneratedPythonInterface(
        instance_attributes=sorted(instance_attributes),
        properties=sorted(properties),
        public_methods=sorted(public_methods - properties),
        child_instances=child_instances,
    )
