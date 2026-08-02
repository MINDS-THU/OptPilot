"""Static contracts for generated simulation result files.

The generator uses this check before accepting LLM-authored runner code.  The
execution backend uses the same checks before declaring ``summary.json`` and
``event_trace.jsonl`` in a new portable manifest.  Keeping the detectors
dependency-free avoids importing or executing generated code and prevents the
two sides from drifting.
"""

from __future__ import annotations

import ast


RESULT_FILE = "summary.json"
RESULT_MARKER = "OPTPILOT_RESULT_FILE"
RESULT_WRITER = "write_simulation_summary"
RESULTS_ENV = "OPTPILOT_SIMULATION_RESULTS_DIR"
RESULT_SCHEMA = "devs.simulation-result.v1"
TRACE_FILE = "event_trace.jsonl"
TRACE_HELPER_MODULE = "devs_project.devs_utils.event_trace"
TRACE_ATTACHER = "attach_event_trace"


class _DirectNameCalls(ast.NodeVisitor):
    """Collect plain function calls without treating definitions as execution."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.names.add(node.func.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _called_names(nodes: list[ast.stmt]) -> set[str]:
    collector = _DirectNameCalls()
    for node in nodes:
        collector.visit(node)
    return collector.names


def _is_coordinator_assignment(statement: ast.stmt) -> bool:
    if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
        return False
    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    if not any(isinstance(target, ast.Name) and target.id == "sim" for target in targets):
        return False
    value = statement.value
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "Coordinator"
    )


def _is_trace_attachment(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == TRACE_ATTACHER
        and len(call.args) == 2
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "sim"
        and isinstance(call.args[1], ast.Name)
        and call.args[1].id == "model"
        and not call.keywords
    )


def _is_sim_initialize(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "initialize"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "sim"
        and not call.args
        and not call.keywords
    )


def _is_sim_exit(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "exit"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "sim"
        and not call.args
        and not call.keywords
    )


def _statement_bodies(node: ast.AST):
    """Yield ordered executable statement lists, including nested branches."""

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    for _field, value in ast.iter_fields(node):
        if isinstance(value, list) and all(isinstance(item, ast.stmt) for item in value):
            if value:
                yield value
            for statement in value:
                yield from _statement_bodies(statement)
        elif isinstance(value, ast.AST):
            yield from _statement_bodies(value)


def _has_event_trace_contract(tree: ast.Module) -> bool:
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == TRACE_HELPER_MODULE
        and any(
            alias.name == TRACE_ATTACHER and alias.asname in (None, TRACE_ATTACHER)
            for alias in node.names
        )
        for node in tree.body
    )
    if not imported:
        return False

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    module_statements = [
        node
        for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    reachable = _called_names(module_statements)
    pending = list(reachable)
    while pending:
        name = pending.pop()
        function = functions.get(name)
        if function is None:
            continue
        for called in _called_names(function.body) - reachable:
            reachable.add(called)
            pending.append(called)

    execution_scopes = [module_statements]
    execution_scopes.extend(
        functions[name].body for name in reachable if name in functions
    )

    # Require the generated runner's deliberately simple order in one reachable
    # lexical body: construct the root Coordinator, attach tracing, initialize,
    # and exit. This rejects a dormant helper, a mere import, a late attachment,
    # or a normal execution path that can never finalize its trace footer.
    for statements in execution_scopes:
        scope = ast.Module(body=statements, type_ignores=[])
        for body in _statement_bodies(scope):
            coordinator_indexes = [
                index
                for index, statement in enumerate(body)
                if _is_coordinator_assignment(statement)
            ]
            attach_indexes = [
                index
                for index, statement in enumerate(body)
                if _is_trace_attachment(statement)
            ]
            initialize_indexes = [
                index
                for index, statement in enumerate(body)
                if _is_sim_initialize(statement)
            ]
            exit_indexes = [
                index
                for index, statement in enumerate(body)
                if _is_sim_exit(statement)
            ]
            if any(
                coordinator < attach < initialize < exit_index
                for coordinator in coordinator_indexes
                for attach in attach_indexes
                for initialize in initialize_indexes
                for exit_index in exit_indexes
            ):
                return True
    return False


def _has_result_summary_contract(tree: ast.Module) -> bool:
    """Recognize the complete, reachable ``summary.json`` writer contract."""

    marker_found = False
    functions: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == RESULT_MARKER
                for target in targets
            ):
                marker_found = (
                    isinstance(node.value, ast.Constant)
                    and node.value.value == RESULT_FILE
                )
        elif isinstance(node, ast.FunctionDef):
            functions[node.name] = node
    writer_definition = functions.get(RESULT_WRITER)
    if not marker_found or writer_definition is None:
        return False

    writer_literals = {
        node.value
        for node in ast.walk(writer_definition)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    writer_attributes = {
        node.attr for node in ast.walk(writer_definition) if isinstance(node, ast.Attribute)
    }
    if (
        not {
            RESULTS_ENV,
            RESULT_SCHEMA,
            "metrics",
            "run",
        }.issubset(writer_literals)
        or not {"write_text", "replace"}.issubset(writer_attributes)
        or not ({"dump", "dumps"} & writer_attributes)
        or "isfinite" not in writer_attributes
    ):
        return False

    reachable = _called_names(
        [
            node
            for node in tree.body
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
    )
    pending = list(reachable)
    while pending:
        name = pending.pop()
        if name == RESULT_WRITER:
            return True
        function = functions.get(name)
        if function is None:
            continue
        for called in _called_names(function.body) - reachable:
            reachable.add(called)
            pending.append(called)
    return False


def declared_result_files(
    source: str, *, filename: str = "<generated_runner>"
) -> tuple[str, ...]:
    """Return only result files backed by complete static runner contracts."""

    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, TypeError, ValueError):
        return ()

    results: list[str] = []
    if _has_result_summary_contract(tree):
        results.append(RESULT_FILE)
    if _has_event_trace_contract(tree):
        results.append(TRACE_FILE)
    return tuple(results)


def require_result_summary_contract(
    source: str, *, filename: str = "<generated_runner>"
) -> None:
    """Reject newly generated runner code that cannot publish its summary."""

    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, TypeError, ValueError) as exc:
        raise ValueError(f"Generated runner is not valid Python: {exc}") from exc

    # A recurring hallucination is ``model.output[name].value``.  xDEVS 3
    # ports expose ``values`` and the singular spelling crashes every run.  Be
    # precise about the rejected shape so unrelated user objects may still
    # define an ordinary ``value`` attribute.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "value":
            continue
        indexed = node.value
        if (
            isinstance(indexed, ast.Subscript)
            and isinstance(indexed.value, ast.Attribute)
            and indexed.value.attr in {"input", "output"}
        ):
            raise ValueError(
                "Generated runner uses the invalid xDEVS Port.value API. "
                "Use stable model state for metrics, or Port.values only when "
                "its transient semantics are explicitly intended."
            )

    if RESULT_FILE not in declared_result_files(source, filename=filename):
        raise ValueError(
            "Generated runner is missing the complete summary.json contract. "
            "Define module-level OPTPILOT_RESULT_FILE, implement the atomic "
            "write_simulation_summary helper from the reference, and call it "
            "after sim.exit()."
        )


def require_event_trace_contract(
    source: str, *, filename: str = "<generated_runner>"
) -> None:
    """Reject generated code that cannot produce the standard event trace."""

    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, TypeError, ValueError) as exc:
        raise ValueError(f"Generated runner is not valid Python: {exc}") from exc
    if not _has_event_trace_contract(tree):
        raise ValueError(
            "Generated runner is missing the event_trace.jsonl contract. "
            "Import attach_event_trace from the generated event_trace helper "
            "and call attach_event_trace(sim, model) after Coordinator creation "
            "and before sim.initialize(), then call sim.exit() after simulation."
        )


__all__ = [
    "RESULT_FILE",
    "RESULT_MARKER",
    "RESULT_SCHEMA",
    "RESULT_WRITER",
    "RESULTS_ENV",
    "TRACE_ATTACHER",
    "TRACE_FILE",
    "TRACE_HELPER_MODULE",
    "declared_result_files",
    "require_event_trace_contract",
    "require_result_summary_contract",
]
