"""Deterministic extraction of generated Python model interfaces."""

from __future__ import annotations

import ast
import json
import os
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path

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


def _inherits_xdevs_component(
    target_class: ast.ClassDef,
    module: ast.Module,
) -> bool:
    """Return whether the class directly inherits an imported xDEVS model base."""

    xdevs_bases: set[str] = set()
    for statement in module.body:
        if not isinstance(statement, ast.ImportFrom) or statement.module != "xdevs.models":
            continue
        for imported in statement.names:
            if imported.name in {"Atomic", "Coupled"}:
                xdevs_bases.add(imported.asname or imported.name)
    return any(
        isinstance(base, ast.Name) and base.id in xdevs_bases
        for base in target_class.bases
    )


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

    # Atomic and Coupled inherit these public port maps from xdevs.models.Component.
    # Coupled generators legitimately use them when wiring child ports, even though
    # the assignments live in the framework rather than in generated source.
    if _inherits_xdevs_component(target_class, module):
        instance_attributes.update({"input", "output"})

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


def refresh_generated_interface_registry(bundle_root: str | Path) -> Path:
    """Refresh only generated interfaces after a source-code repair."""

    root = Path(bundle_root).resolve(strict=True)
    registry_path = root / "devs_project" / "system_model_info.json"
    metadata = registry_path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("system_model_info.json must be a regular file")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(registry, dict):
        raise ValueError("system_model_info.json must contain an object")

    class_names = {
        str(entry.get("class_name") or name)
        for name, entry in registry.items()
        if isinstance(entry, dict)
    }
    for registry_name, entry in registry.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid registry entry for {registry_name!r}")
        class_name = str(entry.get("class_name") or registry_name)
        raw_path = Path(str(entry.get("file_path") or ""))
        try:
            devs_index = raw_path.parts.index("devs_project")
        except ValueError as exc:
            raise ValueError(
                f"Registry path for {class_name!r} is outside devs_project"
            ) from exc
        source_candidate = root / Path(*raw_path.parts[devs_index:])
        source_metadata = source_candidate.lstat()
        if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(source_metadata.st_mode):
            raise ValueError(f"Generated source for {class_name!r} must be a regular file")
        source_path = source_candidate.resolve(strict=True)
        source_path.relative_to(root)
        entry["generated_interface"] = extract_generated_python_interface(
            source_path.read_text(encoding="utf-8"),
            class_name,
            filename=str(source_path),
            child_class_names=class_names,
        ).model_dump(mode="json")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=registry_path.parent,
            prefix=f".{registry_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(registry, temporary, indent=2, ensure_ascii=False)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, registry_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return registry_path
