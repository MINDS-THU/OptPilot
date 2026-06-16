"""Target environment adapters."""

from __future__ import annotations

import json
import hashlib
import importlib
import inspect
import csv
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


class PythonCallableTargetAdapter:
    def __init__(self, definition: Dict[str, Any], study_spec):
        self.definition = definition
        self.study_spec = study_spec
        self._callable = None

    def evaluate(self, artifact_spec: Dict[str, Any], instance: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        if self._callable is None:
            config = self.definition.get("config", {})
            module = importlib.import_module(config["module"])
            self._callable = getattr(module, config["callable"])
        result = self._callable(artifact_spec, instance, context)
        if not isinstance(result, dict):
            raise TypeError("Target callable must return a dict.")
        metric_values = _extract_metric_values(result)
        return {
            "status": result.get("status", "success"),
            "metric_values": metric_values,
            "constraint_results": dict(result.get("constraint_results", {})),
            "artifacts": list(result.get("artifacts", [])),
            "event_summary": dict(result.get("event_summary", {})),
        }


class ConfiguredEnvironmentTargetAdapter:
    """Evaluate environments described by the  EnvironmentConfig schema."""

    def __init__(self, definition: Dict[str, Any], study_spec):
        self.definition = definition
        self.study_spec = study_spec
        self._callable = None

    def evaluate(self, artifact_spec: Dict[str, Any], instance: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        config = self.definition.get("config", {})
        evaluate = config.get("evaluate", {})
        metrics = config.get("metrics", {})
        workspace = Path(artifact_spec.get("workspace") or context["workspace"]).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        candidate_root = Path(artifact_spec.get("candidateRoot", workspace)).resolve()
        instance_index = int(context["instance_index"])
        metrics_path = _workspace_path(workspace, metrics.get("path", "metrics.json"))
        instance_path = _workspace_path(workspace, f"instance_{instance_index}.json")
        candidate_path = _configured_candidate_path(workspace, candidate_root, artifact_spec, config)
        stdout_path = _workspace_path(workspace, f"stdout_{instance_index}.log")
        stderr_path = _workspace_path(workspace, f"stderr_{instance_index}.log")

        _write_json(instance_path, instance)
        candidate_payload_path = _workspace_path(workspace, "candidate.json")
        _write_json(candidate_payload_path, artifact_spec)

        eval_type = evaluate.get("type")
        process_result = None
        python_result = None
        cwd = _workspace_path(workspace, evaluate.get("cwd", "."))
        placeholders = {
            "python": sys.executable,
            "workspace": str(workspace),
            "candidate_root": str(candidate_root),
            "candidate_file": str(candidate_path),
            "candidate": str(candidate_path),
            "candidate_json": str(candidate_payload_path),
            "metrics_file": str(metrics_path),
            "instance_file": str(instance_path),
            "trial_id": context["trial_id"],
            "study_id": context["study_id"],
            "instance_index": str(instance_index),
        }

        if eval_type == "python":
            python_result = self._evaluate_python(evaluate, artifact_spec, instance, context)
        elif eval_type == "command":
            command = _format_command(evaluate["command"], placeholders)
            env = os.environ.copy()
            declared_python_path = os.pathsep.join(str(path) for path in evaluate.get("pythonPath", []) or [] if path)
            env.update({key: _format_string(str(value), placeholders) for key, value in evaluate.get("env", {}).items()})
            if env.get("PYTHONPATH"):
                env["PYTHONPATH"] = _absolute_pythonpath(env["PYTHONPATH"], Path.cwd())
            if declared_python_path:
                env["PYTHONPATH"] = (
                    declared_python_path
                    if not env.get("PYTHONPATH")
                    else declared_python_path + os.pathsep + env["PYTHONPATH"]
                )
            timeout = int(
                evaluate.get(
                    "timeoutSeconds",
                    context.get("resource_profile", {}).get(
                        "timeoutSeconds",
                        self.study_spec.target.get("runtimeContract", {}).get("timeoutSeconds", 600),
                    ),
                )
            )
            try:
                process_result = subprocess.run(
                    command,
                    cwd=str(cwd),
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                stdout_path.write_text(_coerce_process_text(exc.stdout), encoding="utf-8")
                stderr_path.write_text(_coerce_process_text(exc.stderr), encoding="utf-8")
                raise
            stdout_path.write_text(process_result.stdout, encoding="utf-8")
            stderr_path.write_text(process_result.stderr, encoding="utf-8")
            if process_result.returncode != 0:
                raise RuntimeError(
                    f"Configured environment command failed with exit code {process_result.returncode}: "
                    f"{process_result.stderr.strip()}"
                )
        else:
            raise ValueError(f"Unsupported configured evaluate.type: {eval_type!r}")

        metric_values, status, constraint_results, event_summary = self._extract_metrics(
            metrics,
            workspace,
            cwd,
            python_result,
            process_result,
        )
        artifacts = [
            {"type": "json", "name": "instance", "path": str(instance_path)},
            {"type": "json", "name": "candidate_payload", "path": str(candidate_payload_path)},
        ]
        if process_result is not None:
            artifacts.extend(
                [
                    {"type": "log", "name": "stdout", "path": str(stdout_path)},
                    {"type": "log", "name": "stderr", "path": str(stderr_path)},
                ]
            )
        if metrics_path.exists():
            artifacts.append({"type": "json", "name": "metrics", "path": str(metrics_path)})
        artifacts.extend(_collect_artifact_files(workspace, workspace, config.get("filesToSave", [])))

        records_report = _extract_records(workspace, config.get("recordsToExtract", []))
        if records_report:
            records_report_path = workspace / "records_to_extract_report.json"
            _write_json(records_report_path, records_report)
            artifacts.append({"type": "json", "name": "records_to_extract_report", "path": str(records_report_path)})
            event_summary["recordsToExtract"] = records_report

        manifest_path = Path(artifact_spec.get("manifestPath", workspace / "workspace_manifest.json")).resolve()
        readonly_report = _check_readonly_files(workspace, manifest_path)
        if readonly_report is not None:
            readonly_report_path = workspace / "readonly_report.json"
            _write_json(readonly_report_path, readonly_report)
            artifacts.append({"type": "json", "name": "readonly_report", "path": str(readonly_report_path)})
            event_summary["readonly_violations"] = readonly_report["violations"]

        event_summary.setdefault("adapter", "configured_environment")
        event_summary.setdefault("evaluate_type", eval_type)
        if process_result is not None:
            event_summary.setdefault("return_code", process_result.returncode)
            event_summary.setdefault("command", command)
            event_summary.setdefault("cwd", str(cwd))

        return {
            "status": status,
            "metric_values": metric_values,
            "constraint_results": constraint_results,
            "artifacts": artifacts,
            "event_summary": event_summary,
        }

    def _evaluate_python(
        self,
        evaluate: Dict[str, Any],
        artifact_spec: Dict[str, Any],
        instance: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        if self._callable is None:
            for path in reversed(evaluate.get("pythonPath", []) or []):
                if path and path not in sys.path:
                    sys.path.insert(0, path)
            module_name, _, attr = str(evaluate["callable"]).partition(":")
            if not module_name or not attr:
                raise ValueError("evaluate.callable must use 'module:function' format.")
            module = importlib.import_module(module_name)
            self._callable = getattr(module, attr)
        result = self._callable(artifact_spec, instance, context)
        if not isinstance(result, dict):
            raise TypeError("Configured Python evaluator must return a dict.")
        return result

    def _extract_metrics(
        self,
        metrics: Dict[str, Any],
        workspace: Path,
        cwd: Path,
        python_result: Optional[Dict[str, Any]],
        process_result,
    ):
        source = metrics.get("source")
        if source == "return":
            if python_result is None:
                raise ValueError("metrics.source return requires evaluate.type python.")
            payload = python_result
        elif source == "file":
            path = _workspace_path(workspace, metrics["path"])
            if not path.exists():
                raise FileNotFoundError(f"Configured environment did not create metrics file: {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
        elif source == "stdout":
            if process_result is None:
                raise ValueError("metrics.source stdout requires evaluate.type command.")
            payload = json.loads(process_result.stdout)
        elif source == "sqlite":
            payload = _query_sqlite_metrics(_workspace_path(workspace, metrics["database"]), metrics["query"])
        elif source == "custom":
            payload = _run_custom_metrics_extractor(metrics, workspace, cwd, python_result, process_result)
        else:
            raise ValueError(f"Unsupported metrics.source: {source!r}")
        if not isinstance(payload, dict):
            raise TypeError("Configured environment metrics payload must be a JSON object/dict.")
        return (
            _extract_metric_values(payload),
            str(payload.get("status", "success")),
            dict(payload.get("constraint_results", {})),
            dict(payload.get("event_summary", {})),
        )


class CLITargetAdapter:
    def __init__(self, definition: Dict[str, Any], study_spec):
        self.definition = definition
        self.study_spec = study_spec

    def evaluate(self, artifact_spec: Dict[str, Any], instance: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        config = self.definition.get("config", {})
        workspace = Path(context["workspace"])
        instance_index = int(context["instance_index"])
        artifact_path = workspace / config.get("artifactPath", f"cli_artifact_{instance_index}.json")
        instance_path = workspace / config.get("instancePath", f"cli_instance_{instance_index}.json")
        output_path = workspace / config.get("outputPath", f"cli_result_{instance_index}.json")
        stdout_path = workspace / config.get("stdoutPath", f"cli_stdout_{instance_index}.log")
        stderr_path = workspace / config.get("stderrPath", f"cli_stderr_{instance_index}.log")

        _write_json(artifact_path, artifact_spec)
        _write_json(instance_path, instance)

        placeholders = {
            "python": sys.executable,
            "artifact_path": str(artifact_path),
            "instance_path": str(instance_path),
            "output_path": str(output_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "workspace": str(workspace),
            "trial_id": context["trial_id"],
            "study_id": context["study_id"],
            "instance_index": str(instance_index),
        }
        command = _format_command(config["command"], placeholders)
        cwd = _resolve_optional_path(config.get("workingDir"), self.study_spec)
        env = os.environ.copy()
        env.update({key: _format_string(str(value), placeholders) for key, value in config.get("env", {}).items()})
        timeout = int(
            config.get(
                "timeoutSeconds",
                context.get("resource_profile", {}).get(
                    "timeoutSeconds",
                    self.study_spec.target.get("runtimeContract", {}).get("timeoutSeconds", 600),
                ),
            )
        )

        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(_coerce_process_text(exc.stdout), encoding="utf-8")
            stderr_path.write_text(_coerce_process_text(exc.stderr), encoding="utf-8")
            raise
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(
                f"CLI target command failed with exit code {completed.returncode}: {completed.stderr.strip()}"
            )
        if not output_path.exists():
            raise FileNotFoundError(f"CLI target did not create output file: {output_path}")

        result = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise TypeError("CLI target output must be a JSON object.")
        artifacts = list(result.get("artifacts", []))
        artifacts.extend(
            [
                {"type": "json", "name": "cli_artifact_input", "path": str(artifact_path)},
                {"type": "json", "name": "cli_instance_input", "path": str(instance_path)},
                {"type": "json", "name": "cli_result_output", "path": str(output_path)},
                {"type": "log", "name": "cli_stdout", "path": str(stdout_path)},
                {"type": "log", "name": "cli_stderr", "path": str(stderr_path)},
            ]
        )
        event_summary = dict(result.get("event_summary", {}))
        event_summary.update(
            {
                "adapter": "cli",
                "command": command,
                "return_code": completed.returncode,
                "timeout_seconds": timeout,
            }
        )
        return {
            "status": result.get("status", "success"),
            "metric_values": dict(result.get("metric_values", {})),
            "constraint_results": dict(result.get("constraint_results", {})),
            "artifacts": artifacts,
            "event_summary": event_summary,
        }


class ReadOnlySQLiteQuery:
    """Read-only SQLite query capability for method-visible data artifacts."""

    WRITE_KEYWORDS = {
        "attach",
        "create",
        "delete",
        "detach",
        "drop",
        "insert",
        "pragma",
        "replace",
        "update",
        "vacuum",
    }

    def __init__(self, definition: Dict[str, Any], study_spec=None):
        config = definition.get("config", definition)
        database = config.get("path") or config.get("database")
        if not database:
            raise ValueError("ReadOnlySQLiteQuery requires config.path or config.database.")
        if study_spec is not None:
            self.database = study_spec.resolve_path(str(database))
        else:
            self.database = Path(str(database)).resolve()
        self.max_rows = int(config.get("maxRows", 1000) or 1000)

    def query(self, sql: str, params: Optional[List[Any]] = None, *, max_rows: Optional[int] = None) -> Dict[str, Any]:
        statement = str(sql).strip()
        if not statement:
            raise ValueError("SQL query must be non-empty.")
        _reject_mutating_sql(statement)
        limit = int(max_rows or self.max_rows)
        uri = f"file:{self.database}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(statement, list(params or []))
            rows = cursor.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
        return {
            "database": str(self.database),
            "columns": list(rows[0].keys()) if rows else [],
            "rows": [dict(row) for row in rows],
            "row_count": len(rows),
            "truncated": truncated,
        }


def _format_command(command: Any, placeholders: Dict[str, str]) -> List[str]:
    if isinstance(command, str):
        return [_format_string(part, placeholders) for part in shlex.split(command)]
    return [_format_string(str(part), placeholders) for part in command]


def _reject_mutating_sql(statement: str) -> None:
    lowered = statement.lower()
    stripped = lowered.lstrip()
    if not (stripped.startswith("select") or stripped.startswith("with")):
        raise ValueError("Only SELECT/WITH queries are allowed for read-only SQLite interfaces.")
    for token in ReadOnlySQLiteQuery.WRITE_KEYWORDS:
        if token in {part.strip(" ;\n\t\r(),") for part in lowered.split()}:
            raise ValueError(f"SQL keyword {token!r} is not allowed in read-only SQLite interfaces.")


def _absolute_pythonpath(value: str, base_dir: Path) -> str:
    entries = []
    for item in value.split(os.pathsep):
        if not item:
            entries.append(item)
            continue
        path = Path(item)
        entries.append(str(path if path.is_absolute() else (base_dir / path).resolve()))
    return os.pathsep.join(entries)


def _format_string(value: str, placeholders: Dict[str, str]) -> str:
    return value.format(**placeholders)


def _resolve_optional_path(value: Any, study_spec) -> Optional[Path]:
    if not value:
        return None
    return study_spec.resolve_path(str(value))


def _configured_candidate_path(
    workspace: Path,
    candidate_root: Path,
    artifact_spec: Dict[str, Any],
    config: Dict[str, Any],
) -> Path:
    candidate = config.get("candidate", {})
    required = candidate.get("required", []) or []
    if len(required) == 1:
        return _workspace_path(candidate_root, required[0])
    files = artifact_spec.get("files", [])
    if isinstance(files, list) and len(files) == 1 and isinstance(files[0], dict):
        candidate_path = files[0].get("path")
        if candidate_path:
            return _workspace_path(candidate_root, candidate_path)
    return candidate_root


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def _coerce_process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _workspace_path(workspace: Path, value: Any) -> Path:
    path = Path(str(value))
    workspace_resolved = workspace.resolve()
    if path.is_absolute():
        resolved = path.resolve()
        if resolved != workspace_resolved and workspace_resolved not in resolved.parents:
            raise ValueError(f"Workspace path escapes root: {value!r}")
        return resolved
    resolved = (workspace / path).resolve()
    if resolved != workspace_resolved and workspace_resolved not in resolved.parents:
        raise ValueError(f"Workspace path escapes root: {value!r}")
    return resolved


def _extract_metric_values(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("Workspace metric file must be a JSON object.")
    if "metric_values" in payload:
        metric_values = payload["metric_values"]
    elif "metrics" in payload:
        metric_values = payload["metrics"]
    else:
        excluded = {"status", "constraint_results", "artifacts", "event_summary"}
        metric_values = {key: value for key, value in payload.items() if key not in excluded}
    if not isinstance(metric_values, dict):
        raise TypeError("Workspace metrics must be a JSON object.")
    return dict(metric_values)


def _query_sqlite_metrics(database: Path, query: str) -> Dict[str, Any]:
    if not database.exists():
        raise FileNotFoundError(f"SQLite metrics database does not exist: {database}")
    with sqlite3.connect(str(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(query).fetchone()
    if row is None:
        raise ValueError("SQLite metrics query returned no rows.")
    return dict(row)


def _extract_records(workspace: Path, records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not records:
        return None
    streams = []
    for record in records:
        source = record.get("source")
        name = str(record["name"])
        if source == "custom":
            rows, source_path = _run_custom_record_extractor(record, workspace)
        else:
            path = _workspace_path(workspace, record["path"])
            rows = _read_record_rows(path, record)
            source_path = str(path)
        output_path = workspace / "extracted_records" / f"{name}.jsonl"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        streams.append(
            {
                "name": name,
                "source": source,
                "path": source_path,
                "record_count": len(rows),
                "contentRef": str(output_path),
            }
        )
    return {"streams": streams}


def _run_custom_metrics_extractor(
    metrics: Dict[str, Any],
    workspace: Path,
    cwd: Path,
    python_result: Optional[Dict[str, Any]],
    process_result,
) -> Dict[str, Any]:
    component = _load_python_component(metrics["implementation"])
    payload = {
        "workspace": str(workspace),
        "cwd": str(cwd),
        "metrics": dict(metrics),
        "config": dict(metrics.get("config", {})),
        "python_result": python_result,
        "process_result": _process_result_payload(process_result),
    }
    result = _invoke_custom_component(component, payload, method_name="extract")
    if not isinstance(result, dict):
        raise TypeError("Custom metrics extractor must return a dict.")
    return result


def _run_custom_record_extractor(record: Dict[str, Any], workspace: Path) -> tuple[List[Dict[str, Any]], str]:
    component = _load_python_component(record["implementation"])
    payload = {
        "workspace": str(workspace),
        "record": dict(record),
        "config": dict(record.get("config", {})),
    }
    result = _invoke_custom_component(component, payload, method_name="extract")
    source_path = str(workspace)
    if isinstance(result, dict):
        source_path = str(result.get("path") or result.get("source_path") or source_path)
        rows = result.get("rows", result.get("records"))
    else:
        rows = result
    if not isinstance(rows, list):
        raise TypeError("Custom record extractor must return a list of rows or a dict containing rows.")
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("Custom record extractor rows must be JSON objects.")
    return rows, source_path


def _load_python_component(implementation: str):
    if not str(implementation).startswith("python:"):
        raise ValueError(f"Custom implementation must start with 'python:': {implementation!r}")
    module_name, _, attr = str(implementation)[len("python:") :].partition(":")
    if not module_name or not attr:
        raise ValueError(f"Custom implementation must use python:module:object format: {implementation!r}")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _invoke_custom_component(component, payload: Dict[str, Any], *, method_name: str):
    target = component
    if inspect.isclass(component):
        target = component(payload.get("config", {}))
    if hasattr(target, method_name):
        return getattr(target, method_name)(payload)
    if callable(target):
        return target(payload)
    raise TypeError(f"Custom component must be callable or define {method_name}().")


def _process_result_payload(process_result) -> Optional[Dict[str, Any]]:
    if process_result is None:
        return None
    return {
        "returncode": process_result.returncode,
        "stdout": process_result.stdout,
        "stderr": process_result.stderr,
        "args": list(process_result.args) if isinstance(process_result.args, list) else process_result.args,
    }


def _read_record_rows(path: Path, record: Dict[str, Any]) -> List[Dict[str, Any]]:
    source = record.get("source")
    if not path.exists():
        raise FileNotFoundError(f"Record extraction source does not exist: {path}")
    if source == "jsonl":
        rows = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise TypeError(f"JSONL record in {path} must be an object.")
                    rows.append(item)
        return rows
    if source == "csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if source == "sqlite_table":
        table = str(record["table"])
        if not table.replace("_", "").isalnum():
            raise ValueError(f"Unsafe SQLite table name for record extraction: {table!r}")
        query = f"select * from {table}"
        return _query_sqlite_records(path, query)
    if source == "sqlite_query":
        return _query_sqlite_records(path, str(record["query"]))
    raise ValueError(f"Unsupported record extraction source: {source!r}")


def _query_sqlite_records(database: Path, query: str) -> List[Dict[str, Any]]:
    with sqlite3.connect(str(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query).fetchall()
    return [dict(row) for row in rows]


def _collect_artifact_files(workspace: Path, cwd: Path, patterns: List[Any]) -> List[Dict[str, Any]]:
    artifacts: List[Dict[str, Any]] = []
    seen = set()
    workspace_resolved = workspace.resolve()
    for pattern in patterns:
        pattern_text = str(pattern)
        if Path(pattern_text).is_absolute() or ".." in Path(pattern_text).parts:
            continue
        matches = sorted(cwd.glob(pattern_text))
        for path in matches:
            resolved = path.resolve()
            if resolved.is_dir() or resolved in seen:
                continue
            if resolved != workspace_resolved and workspace_resolved not in resolved.parents:
                continue
            seen.add(resolved)
            artifacts.append(
                {
                    "type": _artifact_type(str(resolved)),
                    "name": resolved.name,
                    "path": str(resolved),
                }
            )
    return artifacts


def _artifact_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in {".log", ".txt", ".out", ".err"}:
        return "log"
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        return "sqlite"
    if suffix in {".py", ".cpp", ".c", ".h", ".hpp", ".js", ".ts"}:
        return "code"
    return "file"


def _check_readonly_files(workspace: Path, manifest_path: Path) -> Optional[Dict[str, Any]]:
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    readonly_files = manifest.get("readonly_files", [])
    if not readonly_files:
        return None
    violations = []
    checked = []
    workspace_resolved = workspace.resolve()
    for entry in readonly_files:
        if not isinstance(entry, dict):
            continue
        relative_path = entry.get("path")
        if not isinstance(relative_path, str):
            continue
        path = _workspace_path(workspace_resolved, relative_path)
        expected_sha = entry.get("sha256")
        expected_size = entry.get("sizeBytes")
        if not path.exists():
            violations.append(
                {
                    "path": relative_path,
                    "type": "missing",
                    "expected_sha256": expected_sha,
                    "expected_sizeBytes": expected_size,
                }
            )
            continue
        actual_sha = _sha256_file(path)
        actual_size = path.stat().st_size
        checked.append(
            {
                "path": relative_path,
                "sha256": actual_sha,
                "sizeBytes": actual_size,
            }
        )
        if expected_sha and actual_sha != expected_sha:
            violations.append(
                {
                    "path": relative_path,
                    "type": "modified",
                    "expected_sha256": expected_sha,
                    "actual_sha256": actual_sha,
                    "expected_sizeBytes": expected_size,
                    "actual_sizeBytes": actual_size,
                }
            )
    return {
        "checked_count": len(checked),
        "violation_count": len(violations),
        "checked": checked,
        "violations": violations,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
