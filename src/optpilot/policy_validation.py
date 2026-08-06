"""Environment-declared static policy checks for generated candidate code (F5).

An environment that lets methods edit executable policy code declares its
static contract once, in the environment config's ``policyValidation`` block:

- ``entrypoint``: one file must define exactly one top-level function with
  the declared name (synchronous unless ``allowAsync``), taking at most
  ``maxArguments`` named arguments and no ``*args``/``**kwargs``; no other
  top-level statement may rebind or shadow that name.
- ``forbiddenImports``: module roots generated code must not import.
- ``forbiddenNames``: identifiers generated code must not bind anywhere.
- ``lints``: string constants generated code must not contain, each with an
  authored message explaining the correct usage.

The declaration travels in the candidate context (``context.policyValidation``)
so any code-editing method can apply it generically with
:func:`validate_policy_sources` instead of hardcoding per-environment AST
checks.  These are authoring-contract lints for early, high-quality feedback
to code-generating methods — not a security boundary; candidate code is
executed under the evaluator's own isolation regardless.
"""

from __future__ import annotations

import ast
from typing import Any, List, Mapping


_POLICY_KEYS = {"entrypoint", "forbiddenImports", "forbiddenNames", "lints"}
_ENTRYPOINT_KEYS = {"allowAsync", "callable", "file", "maxArguments"}
_LINT_KEYS = {"forbiddenConstant", "id", "message"}
MAX_POLICY_LIST_ITEMS = 64
MAX_ENTRYPOINT_ARGUMENTS = 16


def validate_policy_declaration(policy: Any, location: str) -> None:
    """Validate the authored ``policyValidation`` declaration shape."""

    if not isinstance(policy, Mapping):
        raise ValueError(f"{location} policyValidation must be an object.")
    if not set(policy) <= _POLICY_KEYS:
        raise ValueError(
            f"{location} policyValidation may contain only "
            f"{sorted(_POLICY_KEYS)}."
        )
    if not policy:
        raise ValueError(
            f"{location} policyValidation must declare at least one check."
        )
    entrypoint = policy.get("entrypoint")
    if entrypoint is not None:
        if not isinstance(entrypoint, Mapping) or not (
            {"callable", "file"} <= set(entrypoint) <= _ENTRYPOINT_KEYS
        ):
            raise ValueError(
                f"{location} policyValidation.entrypoint must declare file and "
                f"callable (optional: maxArguments, allowAsync)."
            )
        file_name = entrypoint["file"]
        if (
            not isinstance(file_name, str)
            or not file_name
            or file_name.startswith(("/", "\\"))
            or ".." in file_name.split("/")
        ):
            raise ValueError(
                f"{location} policyValidation.entrypoint.file must be a "
                "portable relative path."
            )
        callable_name = entrypoint["callable"]
        if not isinstance(callable_name, str) or not callable_name.isidentifier():
            raise ValueError(
                f"{location} policyValidation.entrypoint.callable must be a "
                "Python identifier."
            )
        max_arguments = entrypoint.get("maxArguments", 0)
        if (
            isinstance(max_arguments, bool)
            or not isinstance(max_arguments, int)
            or not 0 <= max_arguments <= MAX_ENTRYPOINT_ARGUMENTS
        ):
            raise ValueError(
                f"{location} policyValidation.entrypoint.maxArguments must be "
                f"an integer between 0 and {MAX_ENTRYPOINT_ARGUMENTS}."
            )
        if not isinstance(entrypoint.get("allowAsync", False), bool):
            raise ValueError(
                f"{location} policyValidation.entrypoint.allowAsync must be a boolean."
            )
    for key in ("forbiddenImports", "forbiddenNames"):
        values = policy.get(key)
        if values is None:
            continue
        if (
            not isinstance(values, list)
            or len(values) > MAX_POLICY_LIST_ITEMS
            or any(
                not isinstance(item, str) or not item.isidentifier()
                for item in values
            )
        ):
            raise ValueError(
                f"{location} policyValidation.{key} must be a list of at most "
                f"{MAX_POLICY_LIST_ITEMS} Python identifiers."
            )
    lints = policy.get("lints")
    if lints is not None:
        if not isinstance(lints, list) or len(lints) > MAX_POLICY_LIST_ITEMS:
            raise ValueError(
                f"{location} policyValidation.lints must be a list of at most "
                f"{MAX_POLICY_LIST_ITEMS} entries."
            )
        seen_ids = set()
        for index, lint in enumerate(lints):
            if not isinstance(lint, Mapping) or set(lint) != _LINT_KEYS:
                raise ValueError(
                    f"{location} policyValidation.lints[{index}] must declare "
                    "exactly id, forbiddenConstant, and message."
                )
            if any(
                not isinstance(lint[key], str) or not lint[key]
                for key in _LINT_KEYS
            ):
                raise ValueError(
                    f"{location} policyValidation.lints[{index}] fields must be "
                    "non-empty strings."
                )
            if lint["id"] in seen_ids:
                raise ValueError(
                    f"{location} policyValidation.lints[{index}].id is duplicated."
                )
            seen_ids.add(lint["id"])


def validate_policy_sources(
    sources: Mapping[str, str],
    policy: Mapping[str, Any],
) -> List[str]:
    """Apply one environment's declared policy checks to candidate sources.

    ``sources`` maps candidate-relative file paths to their text.  Returns a
    list of human-readable violations; an empty list means the sources
    conform.  The declaration is assumed valid per
    :func:`validate_policy_declaration`.
    """

    errors: List[str] = []
    trees: dict[str, ast.Module] = {}
    for path, text in sources.items():
        try:
            trees[path] = ast.parse(text, filename=path)
        except SyntaxError as error:
            errors.append(f"{path} is not valid Python: {error.msg} (line {error.lineno}).")
    if errors:
        return errors

    entrypoint = policy.get("entrypoint")
    if isinstance(entrypoint, Mapping):
        errors.extend(_check_entrypoint(trees, entrypoint))
    forbidden_imports = frozenset(policy.get("forbiddenImports", []) or [])
    forbidden_names = frozenset(policy.get("forbiddenNames", []) or [])
    lints = [
        lint for lint in policy.get("lints", []) or [] if isinstance(lint, Mapping)
    ]
    for path, tree in trees.items():
        for node in ast.walk(tree):
            for root in _imported_module_roots(node):
                if root in forbidden_imports:
                    errors.append(
                        f"{path} imports forbidden module {root!r}; the "
                        "environment policy allows only the documented "
                        "candidate contract."
                    )
            for name in forbidden_names:
                if _binds_name(node, name):
                    errors.append(
                        f"{path} binds forbidden identifier {name!r}."
                    )
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for lint in lints:
                    if node.value == lint.get("forbiddenConstant"):
                        errors.append(f"{path}: {lint.get('message')}")
    return sorted(set(errors))


def _check_entrypoint(
    trees: Mapping[str, ast.Module], entrypoint: Mapping[str, Any]
) -> List[str]:
    file_name = str(entrypoint.get("file"))
    callable_name = str(entrypoint.get("callable"))
    max_arguments = int(entrypoint.get("maxArguments", 0))
    allow_async = bool(entrypoint.get("allowAsync", False))
    tree = trees.get(file_name)
    if tree is None:
        return [f"Missing required policy file {file_name!r}."]
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == callable_name
    ]
    if len(definitions) != 1:
        return [
            f"{file_name} must define exactly one top-level {callable_name}()."
        ]
    definition = definitions[0]
    errors: List[str] = []
    if isinstance(definition, ast.AsyncFunctionDef) and not allow_async:
        errors.append(f"{file_name} {callable_name}() must be synchronous.")
    arguments = definition.args
    named_count = (
        len(arguments.posonlyargs) + len(arguments.args) + len(arguments.kwonlyargs)
    )
    if (
        named_count > max_arguments
        or arguments.vararg is not None
        or arguments.kwarg is not None
    ):
        errors.append(
            f"{file_name} {callable_name}() must accept at most "
            f"{max_arguments} argument(s) and no *args/**kwargs."
        )
    for statement in tree.body:
        if statement is definition:
            continue
        if any(_binds_name(node, callable_name) for node in ast.walk(statement)):
            errors.append(
                f"{file_name} must not rebind or shadow top-level {callable_name}()."
            )
            break
    return errors


def _binds_name(node: ast.AST, name: str) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.Name):
        return node.id == name and isinstance(node.ctx, ast.Store)
    if isinstance(node, ast.arg):
        return node.arg == name
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return any(
            (alias.asname or alias.name.rsplit(".", 1)[-1]) == name
            for alias in node.names
        )
    return False


def _imported_module_roots(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name.split(".", 1)[0] for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.level:
            return ()
        root = str(node.module or "").split(".", 1)[0]
        return (root,) if root else ()
    return ()
