"""Small, deterministic checks for generated xDEVS source.

Only locally provable integration failures are fatal. Style choices and inferred
simulation semantics are left to execution and the benchmark evaluator.
"""

import ast
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class LintIssue:
    rule_id: str
    message: str
    line: int
    column: int = 0
    fatal: bool = True

    def format(self) -> str:
        level = "ERROR" if self.fatal else "WARNING"
        return f"[{level}:{self.rule_id}] line {self.line}: {self.message}"


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_self_method_call(call: ast.Call, method: str | None = None) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and (method is None or call.func.attr == method)
    )


def _function_arguments(method: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    positional = [*method.args.posonlyargs, *method.args.args]
    if positional and positional[0].arg == "self":
        positional = positional[1:]
    names = [item.arg for item in positional]
    if method.args.vararg is not None:
        names.append(f"*{method.args.vararg.arg}")
    names.extend(item.arg for item in method.args.kwonlyargs)
    if method.args.kwarg is not None:
        names.append(f"**{method.args.kwarg.arg}")
    return names


def _literal_port_name(call: ast.Call) -> str | None:
    if _name_of(call.func) != "Port":
        return None
    candidate: ast.AST | None = call.args[1] if len(call.args) >= 2 else None
    if candidate is None:
        candidate = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "name"),
            None,
        )
    if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
        return candidate.value
    return None


def _self_attribute_name(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


def _extract_port_names(
    model_class: ast.ClassDef,
    initializer: ast.FunctionDef | ast.AsyncFunctionDef,
    method_name: str,
) -> tuple[set[str], bool]:
    """Return statically registered port names and whether all were resolvable."""
    methods = {
        node.name: node
        for node in model_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    reachable: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    visited: set[str] = set()

    def visit(method: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if method.name in visited:
            return
        visited.add(method.name)
        reachable.append(method)
        for node in ast.walk(method):
            if not isinstance(node, ast.Call) or not _is_self_method_call(node):
                continue
            helper = methods.get(node.func.attr)
            if helper is not None:
                visit(helper)

    visit(initializer)
    port_attributes: dict[str, str] = {}
    for method in reachable:
        for node in ast.walk(method):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not isinstance(node.value, ast.Call):
                continue
            port_name = _literal_port_name(node.value)
            if port_name is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                attribute = _self_attribute_name(target)
                if attribute is not None:
                    port_attributes[attribute] = port_name

    names: set[str] = set()
    complete = True
    for method in reachable:
        for node in ast.walk(method):
            if not isinstance(node, ast.Call) or not _is_self_method_call(node, method_name):
                continue
            if len(node.args) != 1 or node.keywords:
                complete = False
                continue
            argument = node.args[0]
            port_name = (
                _literal_port_name(argument) if isinstance(argument, ast.Call) else None
            )
            if port_name is None:
                attribute = _self_attribute_name(argument)
                port_name = port_attributes.get(attribute) if attribute is not None else None
            if port_name is None:
                complete = False
            else:
                names.add(port_name)
    return names, complete


def _instance_reference(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return _self_attribute_name(node)


def _coupling_endpoint(node: ast.AST) -> tuple[str, str, str] | None:
    if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Attribute):
        return None
    direction = node.value.attr
    if direction not in {"input", "output"}:
        return None
    instance = _instance_reference(node.value.value)
    if instance is None:
        return None
    try:
        port = ast.literal_eval(node.slice)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(port, str):
        return None
    return instance, direction, port


def _lint_child_coupling_ports(
    model_class: ast.ClassDef,
    child_ports: Mapping[str, Mapping[str, Iterable[str]]],
) -> list[LintIssue]:
    """Reject a literal coupling to a port absent from the child interface."""
    initializer = next(
        (
            node
            for node in model_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "__init__"
        ),
        None,
    )
    if initializer is None:
        return []

    instance_classes: dict[str, str] = {}
    for node in ast.walk(initializer):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        if not isinstance(node.value, ast.Call):
            continue
        class_name = _name_of(node.value.func)
        if class_name not in child_ports:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            instance = _instance_reference(target)
            if instance is not None:
                instance_classes[instance] = class_name

    issues: list[LintIssue] = []
    for call in (
        node
        for node in ast.walk(initializer)
        if isinstance(node, ast.Call) and _is_self_method_call(node, "add_coupling")
    ):
        for argument in call.args[:2]:
            endpoint = _coupling_endpoint(argument)
            if endpoint is None:
                continue
            instance, direction, port = endpoint
            class_name = instance_classes.get(instance)
            if class_name is None:
                continue
            declared = set(child_ports[class_name].get(direction, ()))
            if port not in declared:
                issues.append(
                    LintIssue(
                        "CHILD_COUPLING_PORT_MISMATCH",
                        f"{model_class.name} couples {instance}.{direction}[{port!r}], "
                        f"but {class_name} declares {sorted(declared)!r}.",
                        argument.lineno,
                        argument.col_offset,
                        fatal=False,
                    )
                )
    return issues


def _lint_expected_interface(
    model_class: ast.ClassDef,
    *,
    model_init_args: Sequence[str] | None,
    input_ports: Iterable[str] | None,
    output_ports: Iterable[str] | None,
) -> list[LintIssue]:
    """Report plan/code interface differences without blocking generation."""
    initializer = next(
        (
            node
            for node in model_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "__init__"
        ),
        None,
    )
    issues: list[LintIssue] = []
    line = initializer.lineno if initializer is not None else model_class.lineno
    column = initializer.col_offset if initializer is not None else model_class.col_offset
    if model_init_args is not None:
        actual = _function_arguments(initializer) if initializer is not None else []
        expected = list(model_init_args)
        if actual != expected:
            issues.append(
                LintIssue(
                    "INIT_INTERFACE_MISMATCH",
                    f"constructor parameters are {actual!r}; plan has {expected!r}.",
                    line,
                    column,
                    fatal=False,
                )
            )
    for expected, method_name, rule_id in (
        (input_ports, "add_in_port", "INPUT_PORT_INTERFACE_MISMATCH"),
        (output_ports, "add_out_port", "OUTPUT_PORT_INTERFACE_MISMATCH"),
    ):
        if expected is None or initializer is None:
            continue
        actual, complete = _extract_port_names(model_class, initializer, method_name)
        expected_set = set(expected)
        if complete and actual != expected_set:
            issues.append(
                LintIssue(
                    rule_id,
                    f"registered ports are {sorted(actual)!r}; plan has {sorted(expected_set)!r}.",
                    line,
                    column,
                    fatal=False,
                )
            )
    return issues


def lint_model_code(
    source: str,
    *,
    expected_class_name: str | None = None,
    expected_model_type: str | None = None,
    expected_model_init_args: Sequence[str] | None = None,
    expected_input_ports: Iterable[str] | None = None,
    expected_output_ports: Iterable[str] | None = None,
    expected_child_init_args: Mapping[str, Sequence[tuple[str, str]]] | None = None,
    expected_child_ports: Mapping[str, Mapping[str, Iterable[str]]] | None = None,
) -> list[LintIssue]:
    """Check import-time and literal composition failures without executing code."""
    del expected_child_init_args  # Scenario values are evaluated by the benchmark.
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            LintIssue(
                "SYNTAX_ERROR",
                exc.msg,
                exc.lineno or 1,
                (exc.offset or 1) - 1,
            )
        ]

    issues: list[LintIssue] = []
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    atomic_names = {"Atomic"}
    coupled_names = {"Coupled"}
    bound_names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bound_names.add(statement.name)
        elif isinstance(statement, ast.Import):
            bound_names.update(alias.asname or alias.name.split(".")[0] for alias in statement.names)
        elif isinstance(statement, ast.ImportFrom):
            bound_names.update(alias.asname or alias.name for alias in statement.names)
            for alias in statement.names:
                if alias.name == "Atomic":
                    atomic_names.add(alias.asname or alias.name)
                elif alias.name == "Coupled":
                    coupled_names.add(alias.asname or alias.name)

    expected_class: ast.ClassDef | None = None
    if expected_class_name:
        expected_class = next((node for node in classes if node.name == expected_class_name), None)
        if expected_class is None:
            issues.append(
                LintIssue(
                    "EXPECTED_CLASS_MISSING",
                    f"Expected class {expected_class_name} was not defined.",
                    1,
                )
            )
        else:
            if expected_model_type in {"atomic", "coupled"}:
                required = atomic_names if expected_model_type == "atomic" else coupled_names
                label = "Atomic" if expected_model_type == "atomic" else "Coupled"
                if not any(_name_of(base) in required for base in expected_class.bases):
                    issues.append(
                        LintIssue(
                            "MODEL_TYPE_MISMATCH",
                            f"{expected_class_name} must inherit from {label}.",
                            expected_class.lineno,
                            expected_class.col_offset,
                        )
                    )
            issues.extend(
                _lint_expected_interface(
                    expected_class,
                    model_init_args=expected_model_init_args,
                    input_ports=expected_input_ports,
                    output_ports=expected_output_ports,
                )
            )
            if expected_child_ports:
                issues.extend(_lint_child_coupling_ports(expected_class, expected_child_ports))

    future_annotations = any(
        isinstance(statement, ast.ImportFrom)
        and statement.module == "__future__"
        and any(alias.name == "annotations" for alias in statement.names)
        for statement in tree.body
    )
    if not future_annotations:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.BitOr)
                and any(
                    isinstance(operand, ast.Constant) and isinstance(operand.value, str)
                    for operand in (node.left, node.right)
                )
            ):
                issues.append(
                    LintIssue(
                        "RUNTIME_FORWARD_REF_UNION",
                        "A string cannot be one operand of a runtime union; import the type and do not quote it.",
                        node.lineno,
                        node.col_offset,
                    )
                )

    unresolved: dict[str, ast.Name] = {}
    for node in ast.walk(tree):
        relevant: list[ast.AST] = []
        if isinstance(node, ast.ClassDef):
            relevant.extend(node.bases)
        elif not future_annotations and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            relevant.extend(arg.annotation for arg in node.args.args if arg.annotation)
            relevant.extend(arg.annotation for arg in node.args.kwonlyargs if arg.annotation)
            if node.returns:
                relevant.append(node.returns)
        elif not future_annotations and isinstance(node, ast.AnnAssign):
            relevant.append(node.annotation)
        for expression in relevant:
            for child in ast.walk(expression):
                if (
                    isinstance(child, ast.Name)
                    and child.id in {"Atomic", "Coupled"}
                    and child.id not in bound_names
                ):
                    unresolved.setdefault(child.id, child)
    for name, node in sorted(unresolved.items()):
        issues.append(
            LintIssue(
                "UNRESOLVED_XDEVS_NAME",
                f"{name} is used at runtime but is not imported or defined.",
                node.lineno,
                node.col_offset,
            )
        )

    return sorted(issues, key=lambda item: (item.line, item.column, item.rule_id))


def format_lint_feedback(issues: Iterable[LintIssue]) -> str:
    rendered = "\n".join(issue.format() for issue in issues)
    return "Deterministic xDEVS integration diagnostics:\n" + rendered
