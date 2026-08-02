"""Static contract for student-friendly generated runner arguments.

Generated simulators are meant to be runnable immediately from the interface.
Their ``argparse`` declarations therefore double as the canonical suggested
scenario: the same literal defaults populate the Run form and are exercised by
the generator's smoke run.  This module checks that contract without importing
or executing generated code.

Imported or hand-authored simulators are not checked here.  They may still
declare genuinely required inputs; this contract applies only at the generated
runner acceptance boundary.
"""

from __future__ import annotations

import ast
import math
import re
from typing import Any


class RunnerArgumentContractError(ValueError):
    """Raised when a generated runner cannot provide a complete demo scenario."""


_SCALAR_TYPES = (str, bool, int, float)
_BOOLEAN_ACTION_DEFAULTS = {
    "store_true": False,
    "store_false": True,
}
_LONG_FLAG_RE = re.compile(r"^--[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError) as exc:
        raise ValueError("must be a literal value") from exc


def _is_finite_scalar(value: Any) -> bool:
    if type(value) not in _SCALAR_TYPES:
        return False
    return not (type(value) is float and not math.isfinite(value))


def _parser_names(tree: ast.Module) -> set[str]:
    argparse_modules = {"argparse"}
    parser_constructors = {"ArgumentParser"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "argparse":
                    argparse_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "argparse":
            for alias in node.names:
                if alias.name == "ArgumentParser":
                    parser_constructors.add(alias.asname or alias.name)

    def is_constructor(call: ast.Call) -> bool:
        function = call.func
        return (
            isinstance(function, ast.Name)
            and function.id in parser_constructors
        ) or (
            isinstance(function, ast.Attribute)
            and function.attr == "ArgumentParser"
            and isinstance(function.value, ast.Name)
            and function.value.id in argparse_modules
        )

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call) or not is_constructor(value):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _argument_label(call: ast.Call, index: int) -> str:
    flags = [
        node.value
        for node in call.args
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    return next(
        (flag for flag in flags if flag.startswith("--")),
        flags[0] if flags else f"argument #{index}",
    )


def _validate_argument(call: ast.Call, index: int) -> list[str]:
    label = _argument_label(call, index)
    problems: list[str] = []
    flags: list[str] = []
    for node in call.args:
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            problems.append(f"{label}: argument names must be literal strings")
            continue
        flags.append(node.value)
    long_flags = [flag for flag in flags if flag.startswith("--")]
    if not long_flags:
        problems.append(
            f"{label}: generated runner inputs must use an optional long flag such as --seed"
        )
    elif not any(_LONG_FLAG_RE.fullmatch(flag) for flag in long_flags):
        problems.append(
            f"{label}: long flag must use letters, digits, underscores, or hyphens"
        )
    if any(not flag.startswith("-") for flag in flags):
        problems.append(f"{label}: positional argument names are not supported")

    keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
    if len(keywords) != len(call.keywords):
        problems.append(f"{label}: **kwargs are not supported in generated arguments")

    if "nargs" in keywords:
        problems.append(
            f"{label}: nargs/list-style inputs are not supported; expose one scalar value instead"
        )
    if "const" in keywords:
        problems.append(f"{label}: const-style inputs are not supported")

    action: str | None = None
    if "action" in keywords:
        try:
            action_value = _literal(keywords["action"])
        except ValueError:
            problems.append(f"{label}: action must be a literal string")
        else:
            if action_value not in {"store", "store_true", "store_false"}:
                problems.append(
                    f"{label}: action={action_value!r} is list-like or unsupported"
                )
            elif isinstance(action_value, str):
                action = action_value

    if "required" in keywords:
        try:
            required = _literal(keywords["required"])
        except ValueError:
            problems.append(f"{label}: required must be the literal False")
        else:
            if required is not False:
                problems.append(
                    f"{label}: generated scenario inputs cannot be required; "
                    "provide a suggested default"
                )

    if "default" not in keywords:
        problems.append(
            f"{label}: provide an explicit literal default from the simulation scenario"
        )
        return problems

    try:
        default = _literal(keywords["default"])
    except ValueError:
        problems.append(f"{label}: default must be a literal scalar value")
        return problems
    if not _is_finite_scalar(default):
        problems.append(
            f"{label}: default must be a finite str, bool, int, or float scalar"
        )
        return problems

    if action in _BOOLEAN_ACTION_DEFAULTS:
        expected = _BOOLEAN_ACTION_DEFAULTS[action]
        if default is not expected:
            problems.append(
                f"{label}: action={action!r} must use default={expected!r} "
                "so the value can be toggled"
            )

    type_node = keywords.get("type")
    if isinstance(type_node, ast.Name):
        if type_node.id == "bool":
            problems.append(
                f"{label}: argparse type=bool is ambiguous; use a boolean action or a scalar parser"
            )
        elif type_node.id == "str" and type(default) is not str:
            problems.append(f"{label}: string input requires a string default")
        elif type_node.id == "int" and type(default) is not int:
            problems.append(f"{label}: integer input requires an integer default")
        elif type_node.id == "float" and type(default) not in (int, float):
            problems.append(f"{label}: numeric input requires a numeric default")
        elif type_node.id in {"list", "tuple", "set", "dict"}:
            problems.append(f"{label}: collection-valued argument types are not supported")

    if "choices" in keywords:
        try:
            choices = _literal(keywords["choices"])
        except ValueError:
            problems.append(f"{label}: choices must be a literal list or tuple")
        else:
            if not isinstance(choices, (list, tuple)) or not choices:
                problems.append(f"{label}: choices must be a non-empty literal list or tuple")
            elif not all(_is_finite_scalar(choice) for choice in choices):
                problems.append(f"{label}: every choice must be a finite scalar")
            elif default not in choices:
                problems.append(f"{label}: default {default!r} is not one of the declared choices")

    return problems


def find_runner_argument_violations(
    source: str, *, filename: str = "<generated_runner>"
) -> tuple[str, ...]:
    """Return deterministic generated-runner argument contract violations."""

    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, TypeError, ValueError) as exc:
        return (f"Runner could not be parsed: {exc}",)

    parser_names = _parser_names(tree)
    if not parser_names:
        return ("Runner must construct argparse.ArgumentParser.",)

    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in parser_names
        ):
            calls.append(node)
    calls.sort(key=lambda node: (node.lineno, node.col_offset))
    if not calls:
        return (
            "Generated runner must declare at least one scalar scenario argument, "
            "including its simulation horizon.",
        )

    problems: list[str] = []
    for index, call in enumerate(calls, start=1):
        problems.extend(_validate_argument(call, index))
    return tuple(problems)


def require_runner_argument_contract(
    source: str, *, filename: str = "<generated_runner>"
) -> None:
    """Require every generated argument to provide a usable demo value."""

    violations = find_runner_argument_violations(source, filename=filename)
    if violations:
        details = "\n- ".join(violations)
        raise RunnerArgumentContractError(
            "Generated runner arguments do not define a complete suggested scenario:\n"
            f"- {details}"
        )
