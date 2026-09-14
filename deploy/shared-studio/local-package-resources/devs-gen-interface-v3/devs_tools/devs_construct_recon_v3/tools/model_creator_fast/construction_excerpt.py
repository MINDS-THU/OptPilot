"""Lossless source excerpts needed to construct a generated child model."""

import ast
import textwrap


_DEVS_LIFECYCLE_METHODS = frozenset(
    {"initialize", "deltext", "deltint", "deltcon", "lambdaf", "exit"}
)


def _construction_method_calls(
    node: ast.AST,
    method_names: set[str],
    class_name: str,
) -> set[str]:
    calls: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        function = child.func
        if (
            isinstance(function, ast.Attribute)
            and (
                isinstance(function.value, ast.Name)
                and function.value.id in {"self", class_name}
            )
            and function.attr in method_names
        ):
            calls.add(function.attr)
        if (
            isinstance(function, ast.Name)
            and function.id == "getattr"
            and child.args
            and isinstance(child.args[0], ast.Name)
            and child.args[0].id == "self"
        ):
            # The selected method is unknowable without executing the program.
            # Retain every non-lifecycle method instead of guessing one.
            calls.update(method_names)
    return calls


def extract_child_construction_excerpt(source: str, class_name: str) -> str:
    """Return exact ``__init__`` source plus reachable construction helpers.

    The program is not evaluated and port expressions are not interpreted, so
    loops, conditions, expressions, and helper calls remain exactly as written.
    DEVS lifecycle methods are never pulled in as helpers.
    """

    tree = ast.parse(source)
    candidates = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one top-level class named {class_name!r}; "
            f"found {len(candidates)}"
        )
    class_node = candidates[0]
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    initializer = methods.get("__init__")
    if initializer is None:
        raise ValueError(f"Class {class_name!r} has no explicit __init__ method")

    allowed_helpers = set(methods) - _DEVS_LIFECYCLE_METHODS - {"__init__"}
    reachable: set[str] = set()
    pending = list(
        _construction_method_calls(initializer, allowed_helpers, class_name)
    )
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(
            _construction_method_calls(methods[name], allowed_helpers, class_name)
            - reachable
        )

    bases = ", ".join(ast.unparse(base) for base in class_node.bases)
    header = f"class {class_name}({bases}):" if bases else f"class {class_name}:"
    selected_methods = {"__init__", *reachable}
    selected = [
        node
        for node in class_node.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        or (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in selected_methods
        )
    ]
    source_lines = source.splitlines()
    method_sources = []
    for member in selected:
        if member.end_lineno is None:
            raise ValueError(f"Could not recover source for a member of {class_name}")
        start_line = member.lineno
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start_line = min(
                [member.lineno]
                + [decorator.lineno for decorator in member.decorator_list]
            )
        segment = "\n".join(source_lines[start_line - 1 : member.end_lineno])
        method_sources.append(textwrap.indent(textwrap.dedent(segment), "    "))
    return header + "\n" + "\n\n".join(method_sources)
