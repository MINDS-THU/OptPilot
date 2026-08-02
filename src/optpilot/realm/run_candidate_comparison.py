"""Bounded candidate comparison derived from one canonical run snapshot.

The initial projection consumes immutable candidate, contract, and result facts
already present in one :class:`RunLedgerSnapshot`.  One optional changed-file
diff reauthorizes both exact selections and performs bounded retained-content
reads.  Neither path creates a workspace, materialized projection, lease, or
execution. Presentation selections remain integrity-checked coordinates rather
than bearer credentials, so an actor-bound service must authorize the snapshot
before calling :meth:`RunCandidateComparisonProjection.from_snapshot`.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Mapping

from ._validation import freeze_json, nonnegative_int, required_text, thaw_json
from .errors import RealmConflict
from .refs import canonical_json_bytes
from .run_candidate_evidence import CandidateEvaluationEvidenceIndex
from .run_candidate_outcomes import RunCandidateOutcomeComparison
from .run_candidate_results import CandidateResultIndex
from .run_records import SUPPORTED_CANDIDATE_FORMATS
from .run_snapshot import RunLedgerSnapshot
from .run_workbench import validate_run_workbench_selection
from .selections import SelectionEligibility, SelectionRef
from ..retained_file_candidates import validate_sealed_file_candidate_spec


RUN_CANDIDATE_COMPARISON_SCHEMA = "optpilot.run-candidate-comparison.v3"
RUN_CANDIDATE_INPUT_COMPARISON_SCHEMA = "optpilot.run-candidate-input-comparison.v1"
RUN_CANDIDATE_FILE_TEXT_DIFF_SCHEMA = "optpilot.candidate-file-text-diff.v1"
RUN_CANDIDATE_COMPARISON_MAX_PARAMETERS = 256
RUN_CANDIDATE_COMPARISON_MAX_VALUE_BYTES = 8 * 1024
RUN_CANDIDATE_COMPARISON_VALUE_BUDGET_BYTES = 64 * 1024
RUN_CANDIDATE_COMPARISON_MAX_RESPONSE_BYTES = 256 * 1024
RUN_CANDIDATE_COMPARISON_MAX_DEFINITION_TEXT_BYTES = 256
RUN_CANDIDATE_FILE_TEXT_MAX_BYTES = 48 * 1024
RUN_CANDIDATE_FILE_TEXT_MAX_LINES = 4_000
RUN_CANDIDATE_FILE_DIFF_MAX_BYTES = 96 * 1024

_PRIVATE_PRESENTATION_KEYS = frozenset(
    {
        "absolute_path",
        "binding",
        "binding_id",
        "command",
        "content_ref",
        "credential",
        "cwd",
        "host_path",
        "launch_token",
        "lease",
        "owner",
        "owner_id",
        "path",
        "port",
        "principal",
        "provider",
        "secret",
        "token",
        "workspace_path",
    }
)


def _is_private_presentation_key(value: Any) -> bool:
    normalized = str(value).casefold().replace("-", "_")
    return normalized in _PRIVATE_PRESENTATION_KEYS or normalized.endswith(
        ("_path", "_port", "_secret", "_token")
    )


def _contains_private_presentation_material(value: Any, *, depth: int = 0) -> bool:
    if depth > 24:
        return True
    if isinstance(value, str):
        lowered = value.casefold()
        return (
            value.startswith(("/", "~/", "~\\"))
            or re.match(r"^[A-Za-z]:[\\/]", value) is not None
            or lowered.startswith("file://")
        )
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _is_private_presentation_key(
                key
            ) or _contains_private_presentation_material(child, depth=depth + 1):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(
            _contains_private_presentation_material(child, depth=depth + 1)
            for child in value
        )
    return False


def _is_private_parameter_name(value: str) -> bool:
    """Return whether a parameter label itself is unsafe to present.

    Hiding only the corresponding value is insufficient: names such as
    ``api_token`` or an absolute path disclose the same private presentation
    material even when both value cells are redacted.
    """

    return _is_private_presentation_key(
        value
    ) or _contains_private_presentation_material(value)


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    return "array"


def _bounded_text(value: Any) -> tuple[str | None, bool]:
    if not isinstance(value, str):
        return None, False
    if _contains_private_presentation_material(value):
        return None, True
    encoded = value.encode("utf-8")
    limit = RUN_CANDIDATE_COMPARISON_MAX_DEFINITION_TEXT_BYTES
    if len(encoded) <= limit:
        return value, False
    prefix = encoded[:limit]
    while prefix:
        try:
            return prefix.decode("utf-8"), True
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return None, True


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(float(value)) else None


def _parameter_definition(value: Any) -> dict[str, Any]:
    definition = value if isinstance(value, Mapping) else {}
    value_type = definition.get("valueType", definition.get("type"))
    if not isinstance(value_type, str) or len(value_type.encode("utf-8")) > 64:
        value_type = None
    description, description_truncated = _bounded_text(definition.get("description"))
    unit, unit_truncated = _bounded_text(definition.get("unit"))
    return {
        "value_type": value_type,
        "description": description,
        "description_truncated": description_truncated,
        "unit": unit,
        "unit_truncated": unit_truncated,
        "min": _finite_number(definition.get("min")),
        "max": _finite_number(definition.get("max")),
    }


def _missing_value() -> dict[str, Any]:
    return {
        "present": False,
        "included": False,
        "value": None,
        "kind": None,
        "encoded_bytes": 0,
        "reason": None,
    }


def _value_preview(
    *,
    parameter_name: str,
    present: bool,
    value: Any,
    remaining_budget: list[int],
) -> dict[str, Any]:
    if not present:
        return _missing_value()
    kind = _json_kind(value)
    if _is_private_parameter_name(
        parameter_name
    ) or _contains_private_presentation_material(value):
        return {
            "present": True,
            "included": False,
            "value": None,
            "kind": kind,
            "encoded_bytes": None,
            "reason": "private_presentation_material",
        }
    public_value = thaw_json(value)
    encoded_bytes = len(canonical_json_bytes(public_value))
    if encoded_bytes > RUN_CANDIDATE_COMPARISON_MAX_VALUE_BYTES:
        return {
            "present": True,
            "included": False,
            "value": None,
            "kind": kind,
            "encoded_bytes": encoded_bytes,
            "reason": "value_preview_too_large",
        }
    if encoded_bytes > remaining_budget[0]:
        return {
            "present": True,
            "included": False,
            "value": None,
            "kind": kind,
            "encoded_bytes": encoded_bytes,
            "reason": "value_preview_budget_exhausted",
        }
    remaining_budget[0] -= encoded_bytes
    return {
        "present": True,
        "included": True,
        "value": public_value,
        "kind": kind,
        "encoded_bytes": encoded_bytes,
        "reason": None,
    }


def _search_space(candidate_contract: Mapping[str, Any]) -> Mapping[str, Any]:
    validation = candidate_contract.get("validation")
    if not isinstance(validation, Mapping):
        return MappingProxyType({})
    config = validation.get("config")
    if not isinstance(config, Mapping):
        return MappingProxyType({})
    search_space = config.get("searchSpace")
    return search_space if isinstance(search_space, Mapping) else MappingProxyType({})


def _empty_summary(*, rows: int = 0) -> dict[str, int]:
    return {
        "rows": rows,
        "same": 0,
        "changed": 0,
        "added": 0,
        "removed": 0,
        "hidden": 0,
    }


def _candidate_input_unavailable(
    *,
    candidate_format: str,
    eligibility: SelectionEligibility,
    rows: int = 0,
) -> dict[str, Any]:
    return {
        "schema": RUN_CANDIDATE_INPUT_COMPARISON_SCHEMA,
        "format": candidate_format,
        "eligibility": eligibility.to_dict(),
        "summary": _empty_summary(rows=rows),
        "parameters": None,
        "files": None,
        "metadata": None,
    }


def _file_manifest_comparison(
    *,
    candidate_contract: Mapping[str, Any],
    baseline_spec: Mapping[str, Any],
    comparison_spec: Mapping[str, Any],
) -> dict[str, Any]:
    baseline_manifest = thaw_json(baseline_spec)
    comparison_manifest = thaw_json(comparison_spec)
    try:
        validate_sealed_file_candidate_spec(baseline_manifest, candidate_contract)
        validate_sealed_file_candidate_spec(comparison_manifest, candidate_contract)
    except (TypeError, ValueError):
        return _candidate_input_unavailable(
            candidate_format="files",
            eligibility=SelectionEligibility.unavailable(
                "candidate_file_manifest_unavailable",
                "The exact retained file manifests are not available in the "
                "canonical sealed-candidate shape; outcome comparison remains "
                "available.",
            ),
        )

    baseline_files = {item["path"]: item for item in baseline_manifest["files"]}
    comparison_files = {item["path"]: item for item in comparison_manifest["files"]}
    paths = sorted(
        set(baseline_files) | set(comparison_files),
        key=lambda value: value.encode("utf-8"),
    )
    if len(paths) > RUN_CANDIDATE_COMPARISON_MAX_PARAMETERS:
        return _candidate_input_unavailable(
            candidate_format="files",
            eligibility=SelectionEligibility.unavailable(
                "candidate_file_manifest_limit_exceeded",
                "Candidate file comparison exceeds the bounded manifest row limit.",
            ),
            rows=len(paths),
        )

    counts = _empty_summary(rows=len(paths))
    rows = []
    for path in paths:
        baseline = baseline_files.get(path)
        comparison = comparison_files.get(path)
        if baseline is None:
            change = "added"
        elif comparison is None:
            change = "removed"
        elif baseline == comparison:
            change = "same"
        else:
            change = "changed"
        counts[change] += 1
        rows.append(
            {
                "path": path,
                "baseline": (
                    {
                        "present": True,
                        "size_bytes": baseline["sizeBytes"],
                        "executable": baseline["executable"],
                    }
                    if baseline is not None
                    else {"present": False, "size_bytes": None, "executable": None}
                ),
                "comparison": (
                    {
                        "present": True,
                        "size_bytes": comparison["sizeBytes"],
                        "executable": comparison["executable"],
                    }
                    if comparison is not None
                    else {"present": False, "size_bytes": None, "executable": None}
                ),
                "change": change,
                "content_equal": (
                    None
                    if baseline is None or comparison is None
                    else baseline["sha256"] == comparison["sha256"]
                ),
                "executable_equal": (
                    None
                    if baseline is None or comparison is None
                    else baseline["executable"] == comparison["executable"]
                ),
            }
        )
    entrypoint_change = (
        "same"
        if baseline_manifest["entrypoint"] == comparison_manifest["entrypoint"]
        else "changed"
    )
    return {
        "schema": RUN_CANDIDATE_INPUT_COMPARISON_SCHEMA,
        "format": "files",
        "eligibility": SelectionEligibility.ready().to_dict(),
        "summary": counts,
        "parameters": None,
        "files": {
            "rows": rows,
            "entrypoint": {
                "baseline": baseline_manifest["entrypoint"],
                "comparison": comparison_manifest["entrypoint"],
                "change": entrypoint_change,
            },
            "directories": {
                "baseline_count": len(baseline_manifest["directories"]),
                "comparison_count": len(comparison_manifest["directories"]),
                "same": baseline_manifest["directories"] == comparison_manifest["directories"],
            },
            "options_equal": baseline_manifest["options"] == comparison_manifest["options"],
            "text_diff": None,
        },
        "metadata": None,
    }


def _file_text_diff_unavailable(
    *,
    relative_path: str,
    code: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema": RUN_CANDIDATE_FILE_TEXT_DIFF_SCHEMA,
        "relative_path": relative_path,
        "eligibility": SelectionEligibility.unavailable(code, reason).to_dict(),
        "baseline": None,
        "comparison": None,
        "diff": None,
    }


def _read_candidate_text(
    *,
    selection_content: Any,
    selection: SelectionRef,
    relative_path: str,
    present: bool,
) -> tuple[dict[str, Any], str] | dict[str, Any]:
    if not present:
        return (
            {
                "present": False,
                "size_bytes": 0,
                "line_count": 0,
                "ends_with_newline": False,
            },
            "",
        )
    selected = selection_content.read_range(
        selection=selection,
        relative_path=relative_path,
        offset=0,
        length=RUN_CANDIDATE_FILE_TEXT_MAX_BYTES + 1,
    )
    if not selected.eligibility.eligible:
        return {
            "code": selected.eligibility.code,
            "reason": selected.eligibility.reason,
        }
    if selected.total_size is None or selected.data is None:
        return {
            "code": "candidate_file_text_unavailable",
            "reason": "The retained file bytes are unavailable.",
        }
    if selected.total_size > RUN_CANDIDATE_FILE_TEXT_MAX_BYTES:
        return {
            "code": "candidate_file_text_too_large",
            "reason": (
                "This file exceeds the bounded text-diff size limit; use the "
                "read-only content viewer instead."
            ),
        }
    try:
        text = selected.data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {
            "code": "candidate_file_text_not_utf8",
            "reason": "This retained file is not UTF-8 text.",
        }
    if "\x00" in text:
        return {
            "code": "candidate_file_text_binary",
            "reason": "This retained file contains binary data.",
        }
    line_count = len(text.splitlines())
    if line_count > RUN_CANDIDATE_FILE_TEXT_MAX_LINES:
        return {
            "code": "candidate_file_text_too_many_lines",
            "reason": (
                "This file exceeds the bounded text-diff line limit; use the "
                "read-only content viewer instead."
            ),
        }
    return (
        {
            "present": True,
            "size_bytes": selected.total_size,
            "line_count": line_count,
            "ends_with_newline": text.endswith(("\n", "\r")),
        },
        text,
    )


def with_candidate_file_text_diff(
    projection: "RunCandidateComparisonProjection",
    *,
    selection_content: Any,
    baseline_selection: SelectionRef,
    comparison_selection: SelectionRef,
    relative_path: str,
) -> "RunCandidateComparisonProjection":
    """Attach one explicit, bounded no-copy text diff to a file comparison."""

    if not isinstance(projection, RunCandidateComparisonProjection):
        raise TypeError("projection must be a RunCandidateComparisonProjection.")
    if not isinstance(baseline_selection, SelectionRef) or not isinstance(
        comparison_selection, SelectionRef
    ):
        raise TypeError("candidate text diff selections must be SelectionRefs.")
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or relative_path != relative_path.strip()
        or relative_path.startswith(("/", "\\"))
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
    ):
        raise ValueError("candidate text diff path must be canonical and relative.")
    candidate_input = thaw_json(projection.candidate_input)
    files = candidate_input.get("files")
    if projection.mode != "files" or not isinstance(files, dict):
        raise ValueError("Candidate text diff requires an eligible file comparison.")
    row = next(
        (item for item in files.get("rows", ()) if item.get("path") == relative_path),
        None,
    )
    if row is None:
        raise ValueError("Candidate text diff path is absent from the comparison.")
    if row.get("change") == "same":
        files["text_diff"] = _file_text_diff_unavailable(
            relative_path=relative_path,
            code="candidate_file_text_unchanged",
            reason="The retained file contents are identical.",
        )
    elif selection_content is None or not callable(
        getattr(selection_content, "read_range", None)
    ):
        files["text_diff"] = _file_text_diff_unavailable(
            relative_path=relative_path,
            code="selection_content_provider_unavailable",
            reason="The retained-content reader is unavailable.",
        )
    else:
        baseline_read = _read_candidate_text(
            selection_content=selection_content,
            selection=baseline_selection,
            relative_path=relative_path,
            present=bool(row.get("baseline", {}).get("present")),
        )
        comparison_read = _read_candidate_text(
            selection_content=selection_content,
            selection=comparison_selection,
            relative_path=relative_path,
            present=bool(row.get("comparison", {}).get("present")),
        )
        failure = next(
            (
                value
                for value in (baseline_read, comparison_read)
                if isinstance(value, dict)
            ),
            None,
        )
        if failure is not None:
            files["text_diff"] = _file_text_diff_unavailable(
                relative_path=relative_path,
                code=str(failure.get("code") or "candidate_file_text_unavailable"),
                reason=str(
                    failure.get("reason")
                    or "The retained file text is unavailable."
                ),
            )
        else:
            baseline_facts, baseline_text = baseline_read
            comparison_facts, comparison_text = comparison_read
            diff_text = "\n".join(
                difflib.unified_diff(
                    baseline_text.splitlines(),
                    comparison_text.splitlines(),
                    fromfile=f"baseline/{relative_path}",
                    tofile=f"comparison/{relative_path}",
                    n=3,
                    lineterm="",
                )
            )
            diff_bytes = len(diff_text.encode("utf-8"))
            if diff_bytes > RUN_CANDIDATE_FILE_DIFF_MAX_BYTES:
                files["text_diff"] = _file_text_diff_unavailable(
                    relative_path=relative_path,
                    code="candidate_file_text_diff_too_large",
                    reason=(
                        "The unified diff exceeds the bounded response limit; use "
                        "the read-only content viewer instead."
                    ),
                )
            else:
                files["text_diff"] = {
                    "schema": RUN_CANDIDATE_FILE_TEXT_DIFF_SCHEMA,
                    "relative_path": relative_path,
                    "eligibility": SelectionEligibility.ready().to_dict(),
                    "baseline": baseline_facts,
                    "comparison": comparison_facts,
                    "diff": {
                        "format": "unified",
                        "context_lines": 3,
                        "text": diff_text,
                        "encoded_bytes": diff_bytes,
                        "line_count": len(diff_text.splitlines()),
                        "truncated": False,
                    },
                }
    result = replace(projection, candidate_input=candidate_input)
    if len(canonical_json_bytes(result.to_dict())) <= (
        RUN_CANDIDATE_COMPARISON_MAX_RESPONSE_BYTES
    ):
        return result
    files["text_diff"] = _file_text_diff_unavailable(
        relative_path=relative_path,
        code="candidate_file_text_diff_response_limit_exceeded",
        reason="The text diff would exceed the bounded comparison response.",
    )
    return replace(projection, candidate_input=candidate_input)


def _opaque_metadata_comparison(
    *,
    baseline_spec: Mapping[str, Any],
    comparison_spec: Mapping[str, Any],
) -> dict[str, Any]:
    names = sorted(
        set(baseline_spec) | set(comparison_spec),
        key=lambda value: value.encode("utf-8"),
    )
    if len(names) > RUN_CANDIDATE_COMPARISON_MAX_PARAMETERS:
        return _candidate_input_unavailable(
            candidate_format="opaque",
            eligibility=SelectionEligibility.unavailable(
                "candidate_metadata_limit_exceeded",
                "Candidate metadata comparison exceeds the bounded row limit.",
            ),
            rows=len(names),
        )
    remaining_budget = [RUN_CANDIDATE_COMPARISON_VALUE_BUDGET_BYTES]
    counts = _empty_summary(rows=len(names))
    rows = []
    for name in names:
        name_redacted = _is_private_parameter_name(name)
        baseline_present = name in baseline_spec
        comparison_present = name in comparison_spec
        baseline_value = baseline_spec.get(name)
        comparison_value = comparison_spec.get(name)
        if not baseline_present:
            change = "added"
        elif not comparison_present:
            change = "removed"
        elif baseline_value == comparison_value:
            change = "same"
        else:
            change = "changed"
        baseline_preview = _value_preview(
            parameter_name=name,
            present=baseline_present,
            value=baseline_value,
            remaining_budget=remaining_budget,
        )
        comparison_preview = _value_preview(
            parameter_name=name,
            present=comparison_present,
            value=comparison_value,
            remaining_budget=remaining_budget,
        )
        counts[change] += 1
        counts["hidden"] += sum(
            int(cell["present"] and not cell["included"])
            for cell in (baseline_preview, comparison_preview)
        )
        rows.append(
            {
                "name": None if name_redacted else name,
                "name_redacted": name_redacted,
                "baseline": baseline_preview,
                "comparison": comparison_preview,
                "change": change,
            }
        )
    return {
        "schema": RUN_CANDIDATE_INPUT_COMPARISON_SCHEMA,
        "format": "opaque",
        "eligibility": SelectionEligibility.ready().to_dict(),
        "summary": counts,
        "parameters": None,
        "files": None,
        "metadata": {"rows": rows},
    }


def _candidate_input_comparison(
    *,
    candidate_format: str,
    candidate_contract: Mapping[str, Any],
    baseline_spec: Mapping[str, Any],
    comparison_spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one independently eligible representation comparison."""

    if candidate_format == "files":
        return _file_manifest_comparison(
            candidate_contract=candidate_contract,
            baseline_spec=baseline_spec,
            comparison_spec=comparison_spec,
        )
    if candidate_format == "opaque":
        return _opaque_metadata_comparison(
            baseline_spec=baseline_spec,
            comparison_spec=comparison_spec,
        )
    if candidate_format != "parameters":
        return _candidate_input_unavailable(
            candidate_format=candidate_format,
            eligibility=SelectionEligibility.unsupported(
                "candidate_input_comparison_format_not_supported",
                "Core has no generic retained-input presenter for this "
                "candidate format yet; outcome comparison remains available.",
            ),
        )

    search_space = _search_space(candidate_contract)
    declared_names = sorted(search_space, key=lambda value: value.encode("utf-8"))
    undeclared_names = sorted(
        (set(baseline_spec) | set(comparison_spec)) - set(search_space),
        key=lambda value: value.encode("utf-8"),
    )
    parameter_names = (*declared_names, *undeclared_names)
    if len(parameter_names) > RUN_CANDIDATE_COMPARISON_MAX_PARAMETERS:
        return _candidate_input_unavailable(
            candidate_format=candidate_format,
            eligibility=SelectionEligibility.unavailable(
                "candidate_input_comparison_parameter_limit_exceeded",
                "Candidate input comparison exceeds the bounded parameter row limit.",
            ),
            rows=len(parameter_names),
        )

    remaining_budget = [RUN_CANDIDATE_COMPARISON_VALUE_BUDGET_BYTES]
    counts = _empty_summary(rows=len(parameter_names))
    rows = []
    for name in parameter_names:
        name_redacted = _is_private_parameter_name(name)
        baseline_present = name in baseline_spec
        comparison_present = name in comparison_spec
        baseline_value = baseline_spec.get(name)
        comparison_value = comparison_spec.get(name)
        if not baseline_present:
            change = "added"
        elif not comparison_present:
            change = "removed"
        elif baseline_value == comparison_value:
            change = "same"
        else:
            change = "changed"
        baseline_preview = _value_preview(
            parameter_name=name,
            present=baseline_present,
            value=baseline_value,
            remaining_budget=remaining_budget,
        )
        comparison_preview = _value_preview(
            parameter_name=name,
            present=comparison_present,
            value=comparison_value,
            remaining_budget=remaining_budget,
        )
        counts[change] += 1
        counts["hidden"] += sum(
            int(cell["present"] and not cell["included"])
            for cell in (baseline_preview, comparison_preview)
        )
        rows.append(
            {
                "name": None if name_redacted else name,
                "name_redacted": name_redacted,
                "declared": name in search_space,
                "definition": _parameter_definition(
                    None if name_redacted else search_space.get(name)
                ),
                "baseline": baseline_preview,
                "comparison": comparison_preview,
                "change": change,
            }
        )
    return {
        "schema": RUN_CANDIDATE_INPUT_COMPARISON_SCHEMA,
        "format": candidate_format,
        "eligibility": SelectionEligibility.ready().to_dict(),
        "summary": counts,
        "parameters": {"rows": rows},
        "files": None,
        "metadata": None,
    }


@dataclass(frozen=True)
class RunCandidateComparisonProjection:
    """Immutable, bounded comparison at one exact canonical run head."""

    run_id: str
    revision: int
    sequence: int
    mode: str
    eligibility: SelectionEligibility
    operands: tuple[Mapping[str, Any], Mapping[str, Any]]
    contract: Mapping[str, Any]
    outcomes: Mapping[str, Any]
    candidate_input: Mapping[str, Any]

    def __post_init__(self) -> None:
        required_text(self.run_id, "candidate comparison run id", max_bytes=512)
        nonnegative_int(self.revision, "candidate comparison revision")
        nonnegative_int(self.sequence, "candidate comparison sequence")
        if self.mode not in SUPPORTED_CANDIDATE_FORMATS:
            raise ValueError("candidate comparison mode is unsupported.")
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        operands = tuple(
            freeze_json(value, label="candidate comparison operand")
            for value in self.operands
        )
        if len(operands) != 2:
            raise ValueError("candidate comparison requires exactly two operands.")
        if tuple(item["role"] for item in operands) != (
            "baseline",
            "comparison",
        ):
            raise ValueError("candidate comparison operand roles are invalid.")
        contract = freeze_json(self.contract, label="candidate comparison contract")
        outcomes = freeze_json(self.outcomes, label="candidate comparison outcomes")
        candidate_input = freeze_json(
            self.candidate_input, label="candidate input comparison"
        )
        if (
            not isinstance(contract, Mapping)
            or not isinstance(outcomes, Mapping)
            or not isinstance(candidate_input, Mapping)
        ):
            raise TypeError("candidate comparison records must be mappings.")
        object.__setattr__(self, "operands", operands)
        object.__setattr__(self, "contract", contract)
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "candidate_input", candidate_input)

    @property
    def head(self) -> dict[str, int]:
        return {"revision": self.revision, "sequence": self.sequence}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUN_CANDIDATE_COMPARISON_SCHEMA,
            "run_id": self.run_id,
            "head": self.head,
            "mode": self.mode,
            "eligibility": self.eligibility.to_dict(),
            "operands": [thaw_json(value) for value in self.operands],
            "contract": thaw_json(self.contract),
            "outcomes": thaw_json(self.outcomes),
            "candidate_input": thaw_json(self.candidate_input),
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: RunLedgerSnapshot,
        *,
        baseline_presentation_selection: Mapping[str, Any],
        comparison_presentation_selection: Mapping[str, Any],
    ) -> "RunCandidateComparisonProjection":
        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")
        baseline = validate_run_workbench_selection(baseline_presentation_selection)
        comparison = validate_run_workbench_selection(comparison_presentation_selection)
        selections = (baseline, comparison)
        if baseline["selection_id"] == comparison["selection_id"]:
            raise ValueError("Candidate comparison selections must be distinct.")
        for presented in selections:
            if presented["run_id"] != snapshot.run.run_id:
                raise ValueError("Workbench selection belongs to a different run View.")
            if (
                presented["revision"] != snapshot.revision.revision
                or presented["sequence"] != snapshot.revision.last_sequence
            ):
                raise RealmConflict("Run presentation head changed.")
            if presented["kind"] != "candidate":
                raise ValueError("Candidate comparison requires candidate selections.")

        candidates = {item.candidate_id: item for item in snapshot.candidates}
        try:
            candidate_pair = tuple(
                candidates[presented["entity_id"]] for presented in selections
            )
        except KeyError as error:
            raise ValueError(
                "Workbench selection does not identify a candidate at this run head."
            ) from error
        if candidate_pair[0].candidate_id == candidate_pair[1].candidate_id:
            raise ValueError("Candidate comparison selections must be distinct.")

        candidate_contract = (
            snapshot.evaluation_closure.environment_revision.candidate_contract
        )
        contract_format = candidate_contract.get("format")
        if contract_format not in SUPPORTED_CANDIDATE_FORMATS:
            raise ValueError("Retained candidate contract format is unsupported.")
        formats = tuple(
            item.admission.envelope.candidate_format for item in candidate_pair
        )
        if formats != (contract_format, contract_format):
            raise ValueError(
                "Candidate comparison operands differ from the retained contract."
            )

        evidence = CandidateEvaluationEvidenceIndex.from_snapshot(snapshot)
        results = CandidateResultIndex.from_snapshot(snapshot, evidence_index=evidence)
        operands = tuple(
            {
                "role": role,
                "selection": presented,
                "candidate": {
                    "id": candidate.candidate_id,
                    "format": candidate.admission.envelope.candidate_format,
                },
                "result": thaw_json(results.for_candidate_key(candidate.candidate_key)),
            }
            for role, presented, candidate in zip(
                ("baseline", "comparison"),
                selections,
                candidate_pair,
                strict=True,
            )
        )
        contract = {
            "format": contract_format,
            "source": "retained_run_definition",
        }
        common = {
            "run_id": snapshot.run.run_id,
            "revision": snapshot.revision.revision,
            "sequence": snapshot.revision.last_sequence,
            "mode": contract_format,
            "operands": operands,
            "contract": contract,
            "outcomes": RunCandidateOutcomeComparison.from_snapshot(
                snapshot,
                baseline_candidate_key=candidate_pair[0].candidate_key,
                comparison_candidate_key=candidate_pair[1].candidate_key,
                evidence_index=evidence,
                result_index=results,
            ).to_dict(),
        }
        candidate_input = _candidate_input_comparison(
            candidate_format=contract_format,
            candidate_contract=candidate_contract,
            baseline_spec=candidate_pair[0].admission.envelope.spec,
            comparison_spec=candidate_pair[1].admission.envelope.spec,
        )
        ready = cls(
            **common,
            eligibility=SelectionEligibility.ready(),
            candidate_input=candidate_input,
        )
        if (
            len(canonical_json_bytes(ready.to_dict()))
            <= RUN_CANDIDATE_COMPARISON_MAX_RESPONSE_BYTES
        ):
            return ready
        bounded = cls(
            **common,
            eligibility=SelectionEligibility.ready(),
            candidate_input=_candidate_input_unavailable(
                candidate_format=contract_format,
                eligibility=SelectionEligibility.unavailable(
                    "candidate_input_comparison_response_limit_exceeded",
                    "Candidate input comparison exceeds the bounded response limit; "
                    "outcome comparison remains available.",
                ),
                rows=candidate_input["summary"]["rows"],
            ),
        )
        if (
            len(canonical_json_bytes(bounded.to_dict()))
            > RUN_CANDIDATE_COMPARISON_MAX_RESPONSE_BYTES
        ):
            raise ValueError("Bounded candidate comparison exceeds its hard limit.")
        return bounded


__all__ = [
    "RUN_CANDIDATE_COMPARISON_MAX_DEFINITION_TEXT_BYTES",
    "RUN_CANDIDATE_COMPARISON_MAX_PARAMETERS",
    "RUN_CANDIDATE_COMPARISON_MAX_RESPONSE_BYTES",
    "RUN_CANDIDATE_COMPARISON_MAX_VALUE_BYTES",
    "RUN_CANDIDATE_COMPARISON_SCHEMA",
    "RUN_CANDIDATE_COMPARISON_VALUE_BUDGET_BYTES",
    "RUN_CANDIDATE_FILE_DIFF_MAX_BYTES",
    "RUN_CANDIDATE_FILE_TEXT_DIFF_SCHEMA",
    "RUN_CANDIDATE_FILE_TEXT_MAX_BYTES",
    "RUN_CANDIDATE_FILE_TEXT_MAX_LINES",
    "RUN_CANDIDATE_INPUT_COMPARISON_SCHEMA",
    "RunCandidateComparisonProjection",
    "with_candidate_file_text_diff",
]
