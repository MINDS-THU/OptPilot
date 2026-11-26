"""Process-aware LLM search over executable scheduling policies.

The method mirrors the paper's manager/editor cadence while respecting the
OptPilot file-candidate boundary:

* evaluate the environment-provided policy first;
* replay the incumbent's worst seed to obtain a queryable SQLite trace;
* ask one manager model to inspect the policy, metrics, and trace;
* ask independent editor calls to implement 3--4 distinct plans in parallel;
* keep an edit only when it strictly improves the incumbent mean score.

Only complete ``scheduler.py`` and ``param_estimator.py`` files form the
executable method/environment boundary. Generated candidates also carry one
non-executable, bounded provenance JSON sidecar. Simulation implementation
details remain owned by the environment.
"""

from __future__ import annotations

import ast
import concurrent.futures
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from optpilot.candidate_staging import CandidateBundleStager, CandidateFileMapping
from optpilot.policy_validation import (
    validate_policy_sources as validate_declared_policy_sources,
)
from optpilot.provenance import build_generator_record


_SQL_MUTATION_WORDS = re.compile(
    r"\b(attach|detach|insert|update|delete|drop|alter|create|replace|vacuum|reindex|analyze)\b",
    re.IGNORECASE,
)
_STATE_SCHEMA = "process-aware-llm-state.v1"
_PROMPT_SCHEMA = "process-aware-llm-prompt.v1"
_DEFAULT_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024
_DEFAULT_HTTP_ERROR_BYTES = 64 * 1024
_DEFAULT_QUERY_CELL_BYTES = 4096
_DEFAULT_QUERY_RESULT_BYTES = 64 * 1024
_DEFAULT_SQLITE_LENGTH_BYTES = 1024 * 1024
_DEFAULT_REPLAY_RESULT_BYTES = 1024 * 1024
_DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
_TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_TRANSIENT_API_ERROR_TYPES = frozenset(
    {
        "provider_overloaded",
        "provider_unavailable",
        "rate_limit_exceeded",
        "server",
        "timeout",
    }
)
_PROVENANCE_PATH = "provenance/llm_exchanges.json"
_PROVENANCE_SCHEMA = "process-aware-llm-exchanges.v1"
_PROVIDER_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)(\bbearer\s+)([^\s,;\"'}\]]+)"),
    re.compile(
        r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
        r"secret|password|credential|authorization|cookie)[\"']?\s*[:=]\s*[\"']?)"
        r"([^\"'\s,;}\]]+)"
    ),
)


class _LLMRequestError(RuntimeError):
    """A provider/configuration failure that editor prompting cannot repair."""


class _CorrectableLLMOutputError(_LLMRequestError):
    """The provider responded, but its output still needs semantic correction."""


class _RetryableLLMError(_LLMRequestError):
    """One bounded provider attempt failed for a reason safe to retry."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        correctable_output: bool = False,
    ):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.correctable_output = bool(correctable_output)


class ProcessAwareLLMHeuristicMethod:
    """Iteratively design schedulers using simulation metrics and trace data."""

    def __init__(self, definition: Dict[str, Any], study_spec, rng):
        self.definition = definition
        self.study_spec = study_spec
        self.rng = rng
        self.config = dict(definition.get("config", {}))
        self.candidate_context = dict(study_spec.candidate.get("context", {}))
        self.target_files = tuple(_editable_paths(self.candidate_context))
        if not self.target_files:
            raise ValueError(
                "ProcessAwareLLMHeuristicMethod requires a non-empty "
                "candidate.files.editable declaration."
            )
        self.policy_validation = self.candidate_context.get("policyValidation")
        if not isinstance(self.policy_validation, dict) or not self.policy_validation:
            raise ValueError(
                "ProcessAwareLLMHeuristicMethod requires the environment to "
                "declare a policyValidation block (entrypoint, forbidden "
                "imports/names, lints) in its candidate context."
            )
        self.environment_description = str(
            self.candidate_context.get("description") or "the target system"
        ).strip() or "the target system"
        self.allowed_paths = tuple(
            str(value)
            for value in self.candidate_context.get("files", {}).get("allow", [])
        )
        for emitted_path in (*self.target_files, _PROVENANCE_PATH):
            if not _path_allowed(emitted_path, self.allowed_paths):
                raise ValueError(
                    "ProcessAwareLLMHeuristicMethod cannot emit "
                    f"{emitted_path!r}; candidate.files.allow does not permit it."
                )

        self.source_files = _resolve_template_files(self.target_files, self.candidate_context)
        self.references = _method_references(self.candidate_context)
        self.policy_instructions = _load_policy_instructions(self.candidate_context)
        primary = study_spec.objective["primaryMetric"]
        self.primary_metric = str(primary["name"])
        self.primary_direction = str(primary["direction"])
        self.replay_seed_metric = str(
            self.config.get("replaySeedMetric", "worst_seed")
        )
        self.replay_score_metric = str(
            self.config.get("replayScoreMetric", "worst_total_score")
        )
        raw_score_keys = self.config.get(
            "replayScoreKeys",
            ["total_score", "mean_total_score", "total_score_mean"],
        )
        if (
            not isinstance(raw_score_keys, list)
            or not raw_score_keys
            or not all(isinstance(item, str) and item for item in raw_score_keys)
        ):
            raise ValueError("replayScoreKeys must be a non-empty list of names.")
        self.replay_score_keys = tuple(raw_score_keys)
        self.replay_callable = _capability_callable(
            self.candidate_context, "exact_seed_replay"
        )

        self.candidates_per_iteration = _bounded_int(
            self.config.get("candidatesPerIteration", 4), 1, 4, "candidatesPerIteration"
        )
        self.max_iterations = _bounded_int(
            self.config.get("maxIterations", 10), 1, 100, "maxIterations"
        )
        self.patience = _bounded_int(self.config.get("patience", 3), 1, 100, "patience")
        self.target_score = float(self.config.get("targetScore", 80.0))
        self.editor_retries = _bounded_int(
            self.config.get("editorRetries", 3), 1, 10, "editorRetries"
        )
        self.manager_correction_attempts = _bounded_int(
            self.config.get("managerCorrectionAttempts", 2),
            0,
            4,
            "managerCorrectionAttempts",
        )
        self.request_retries = _bounded_int(
            self.config.get("requestRetries", 2), 0, 5, "requestRetries"
        )
        self.retry_base_seconds = _bounded_float(
            self.config.get("retryBaseSeconds", 1.0),
            0.0,
            60.0,
            "retryBaseSeconds",
        )
        self.retry_max_seconds = _bounded_float(
            self.config.get("retryMaxSeconds", 8.0),
            0.0,
            300.0,
            "retryMaxSeconds",
        )
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retryMaxSeconds must be at least retryBaseSeconds.")
        self.request_timeout_seconds = _bounded_int(
            self.config.get("requestTimeoutSeconds", 180),
            1,
            1800,
            "requestTimeoutSeconds",
        )
        self.manager_query_rounds = _bounded_int(
            self.config.get("managerQueryRounds", 2), 0, 8, "managerQueryRounds"
        )
        self.max_query_rows = _bounded_int(
            self.config.get("maxQueryRows", 20), 1, 200, "maxQueryRows"
        )
        self.max_query_cell_bytes = _bounded_int(
            self.config.get("maxQueryCellBytes", _DEFAULT_QUERY_CELL_BYTES),
            64,
            64 * 1024,
            "maxQueryCellBytes",
        )
        self.max_query_result_bytes = _bounded_int(
            self.config.get("maxQueryResultBytes", _DEFAULT_QUERY_RESULT_BYTES),
            1024,
            1024 * 1024,
            "maxQueryResultBytes",
        )
        self.sqlite_length_limit_bytes = _bounded_int(
            self.config.get("sqliteLengthLimitBytes", _DEFAULT_SQLITE_LENGTH_BYTES),
            4096,
            16 * 1024 * 1024,
            "sqliteLengthLimitBytes",
        )
        self.max_http_response_bytes = _bounded_int(
            self.config.get("maxHttpResponseBytes", _DEFAULT_HTTP_RESPONSE_BYTES),
            1024,
            16 * 1024 * 1024,
            "maxHttpResponseBytes",
        )
        self.max_http_error_bytes = _bounded_int(
            self.config.get("maxHttpErrorBytes", _DEFAULT_HTTP_ERROR_BYTES),
            1024,
            1024 * 1024,
            "maxHttpErrorBytes",
        )
        self.replay_timeout_seconds = _bounded_int(
            self.config.get("replayTimeoutSeconds", 1800),
            1,
            10800,
            "replayTimeoutSeconds",
        )
        self.max_replay_result_bytes = _bounded_int(
            self.config.get("maxReplayResultBytes", _DEFAULT_REPLAY_RESULT_BYTES),
            4096,
            8 * 1024 * 1024,
            "maxReplayResultBytes",
        )
        self.max_state_cache_bytes = _bounded_int(
            self.config.get("maxStateCacheBytes", 8 * 1024 * 1024),
            64 * 1024,
            64 * 1024 * 1024,
            "maxStateCacheBytes",
        )
        self.max_provenance_bytes = _bounded_int(
            self.config.get("maxProvenanceBytes", 8 * 1024 * 1024),
            64 * 1024,
            64 * 1024 * 1024,
            "maxProvenanceBytes",
        )

        self._baseline_emitted = False
        self._iteration = 0
        self._no_improvement_rounds = 0
        self._stopped = False
        self._elite_id: str | None = None
        self._elite_score: float | None = None
        self._elite_sources: Dict[str, str] | None = None
        self._candidate_sources: Dict[str, Dict[str, str]] = {}
        self._observations: List[Dict[str, Any]] = []
        self._latest_trace_summary: Dict[str, Any] = {}
        self._proposal_cache: Dict[str, Dict[str, Any]] = {}
        self._llm_exchanges: Dict[str, Dict[str, Any]] = {}
        self._prompt_payloads: Dict[str, Dict[str, Any]] = {}
        self._trace_workspace = tempfile.TemporaryDirectory(prefix="optpilot-process-aware-")
        self._trace_database = Path(self._trace_workspace.name) / "worst_run.db"
        self._replay_settings = self._load_replay_settings()
        self._state_cache_path: Path | None = None
        self._state_cache_loaded = False
        self._prompt_store_dir: Path | None = None
        self._prompt_store_lock = threading.Lock()
        self._provenance_lock = threading.Lock()
        self._state_lock = threading.RLock()

    def _entrypoint_contract_line(self) -> str:
        """Render the preserved-interface instruction from the declaration."""

        entrypoint = self.policy_validation.get("entrypoint") or {}
        callable_name = str(entrypoint.get("callable") or "").strip()
        file_name = str(entrypoint.get("file") or "").strip()
        if not callable_name:
            return "Preserve the documented policy entrypoint."
        limit = entrypoint.get("maxArguments")
        arguments = (
            " taking no arguments"
            if limit == 0
            else f" taking at most {limit} arguments"
            if isinstance(limit, int)
            else ""
        )
        location = f" in {file_name}" if file_name else ""
        return (
            f"Plans must preserve the {callable_name}() entrypoint"
            f"{location}{arguments}."
        )

    def close(self) -> None:
        """Release the private replay workspace eagerly."""

        workspace = getattr(self, "_trace_workspace", None)
        if workspace is not None:
            workspace.cleanup()
            self._trace_workspace = None

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown timing varies
        self.close()

    def propose(self, n_candidates: int, study_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Stage the baseline or one parallel manager/editor generation."""

        if n_candidates <= 0:
            return []

        runtime_context = dict(study_state.get("runtime_context", {}))
        staging_dir = runtime_context.get("candidate_staging_dir")
        if not staging_dir:
            raise ValueError(
                "ProcessAwareLLMHeuristicMethod requires "
                "runtime_context.candidate_staging_dir."
            )
        stager = CandidateBundleStager(staging_dir)
        self._configure_runtime_services(runtime_context)
        request_digest = self._proposal_request_digest(n_candidates, study_state)
        phase_before = self._phase_digest()
        cached = self._proposal_cache.get(request_digest)
        if cached is not None and phase_before in {
            cached.get("phase_before"),
            cached.get("phase_after"),
        }:
            return self._restage_cached_proposal(stager, cached)
        if self._stopped:
            return []

        # The first trial is always the exact environment-provided policy.  It
        # supplies both the starting score and the first process trace.
        if not self._baseline_emitted:
            candidate, sources = self._stage_baseline(stager)
            self._baseline_emitted = True
            self._candidate_sources[candidate["candidate_id"]] = dict(sources)
            self._remember_proposal(
                request_digest,
                phase_before,
                [self._proposal_descriptor(candidate, sources)],
            )
            self._persist_state()
            return [candidate]

        # A manager round must be grounded in an evaluated incumbent.
        if self._elite_sources is None or self._elite_id is None:
            return []
        if self._should_stop():
            self._stopped = True
            return []

        count = min(int(n_candidates), self.candidates_per_iteration)
        next_iteration = self._iteration + 1
        manager_messages = self._manager_messages(count, study_state)
        manager_record = self._store_prompt(
            manager_messages,
            {"role": "manager", "iteration": next_iteration},
        )
        manager_result = self._run_manager_query_loop(manager_messages, count)
        plans = _extract_plans(manager_result, count)
        if not plans:
            raise RuntimeError("The manager did not return any executable improvement plans.")

        editor_jobs: List[Tuple[int, Dict[str, str], List[Dict[str, str]], Dict[str, Any] | None]] = []
        for plan_index, plan in enumerate(plans, start=1):
            messages = self._editor_messages(plan, plan_index, next_iteration)
            prompt_record = self._store_prompt(
                messages,
                {
                    "role": "editor",
                    "iteration": next_iteration,
                    "plan_index": plan_index,
                    "manager_prompt_record_id": (manager_record or {}).get("prompt_record_id"),
                },
            )
            editor_jobs.append((plan_index, plan, messages, prompt_record))

        results: List[Tuple[int, Dict[str, str], str, Dict[str, Any] | None]] = []
        failures: List[str] = []
        failure_diagnostics: List[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(editor_jobs)) as executor:
            futures = {
                executor.submit(self._run_editor, plan, messages):
                (plan_index, plan, prompt_record)
                for plan_index, plan, messages, prompt_record in editor_jobs
            }
            for future in concurrent.futures.as_completed(futures):
                plan_index, plan, prompt_record = futures[future]
                try:
                    summary, sources = future.result()
                except Exception as exc:  # one editor must not cancel its siblings
                    failures.append(f"plan-{plan_index:02d}-{type(exc).__name__}")
                    failure_diagnostics.append(
                        f"plan {plan_index}: {type(exc).__name__}: {exc}"
                    )
                    continue
                results.append((plan_index, sources, summary, prompt_record))

        if not results:
            joined = "; ".join(failure_diagnostics) if failures else "no editor result"
            raise RuntimeError(f"All editor plans failed: {joined}")

        candidates = []
        descriptors = []
        pending_sources: Dict[str, Dict[str, str]] = {}
        stable_failures = sorted(failures)
        for plan_index, sources, summary, prompt_record in sorted(results):
            candidate_id = (
                f"{self.definition['id']}-iteration-{next_iteration:02d}-plan-{plan_index:02d}"
            )
            candidate = self._stage_sources(
                stager,
                candidate_id=candidate_id,
                sources=sources,
                lineage={"parents": [self._elite_id]},
                strategy="process_aware_parallel_edit",
                summary=summary,
                prompt_record=prompt_record,
                extra={
                    "iteration": next_iteration,
                    "plan_index": plan_index,
                    "manager_prompt_record_id": (manager_record or {}).get("prompt_record_id"),
                    "failed_sibling_plans": stable_failures,
                },
            )
            pending_sources[candidate_id] = dict(sources)
            descriptors.append(
                self._proposal_descriptor(
                    candidate,
                    sources,
                    support_files=self._provenance_files(),
                )
            )
            candidates.append(candidate)

        self._candidate_sources.update(pending_sources)
        self._iteration = next_iteration
        self._remember_proposal(request_digest, phase_before, descriptors)
        self._persist_state()
        return candidates

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        """Select a strict incumbent improvement and replay its worst seed."""

        copied = [dict(item) for item in observations]
        self._observations.extend(copied)
        successful = []
        for observation in copied:
            candidate_id = _candidate_id(observation)
            if candidate_id not in self._candidate_sources:
                continue
            if str(observation.get("status", "success")) != "success":
                continue
            metric_values = dict(observation.get("metric_values", {}))
            value = metric_values.get(self.primary_metric)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                continue
            successful.append((float(value), candidate_id, observation))

        if not successful:
            if self._elite_id is not None:
                self._no_improvement_rounds += 1
            self._stopped = self._should_stop()
            self._persist_state()
            return

        best_value, best_id, best_observation = successful[0]
        for value, candidate_id, observation in successful[1:]:
            if _is_better(value, best_value, self.primary_direction):
                best_value, best_id, best_observation = value, candidate_id, observation

        improved = self._elite_score is None or _is_better(
            best_value, self._elite_score, self.primary_direction
        )
        if improved:
            self._elite_id = best_id
            self._elite_score = best_value
            self._elite_sources = dict(self._candidate_sources[best_id])
            self._no_improvement_rounds = 0
            self._replay_incumbent(best_observation)
        elif self._iteration > 0:
            self._no_improvement_rounds += 1

        self._stopped = self._should_stop()
        self._persist_state()

    def _stage_baseline(
        self, stager: CandidateBundleStager
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        sources = {
            path: self.source_files[path].read_text(encoding="utf-8")
            for path in self.target_files
        }
        _validate_policy_sources(
            sources,
            editable_files=self.target_files,
            policy_validation=self.policy_validation,
        )
        candidate_id = f"{self.definition['id']}-baseline"
        candidate = stager.stage_files(
            [
                CandidateFileMapping(source=self.source_files[path], path=path)
                for path in self.target_files
            ],
            candidate_id=candidate_id,
            lineage={"parents": [], "source": "environment_candidate_template"},
            generator={
                "method_id": self.definition["id"],
                "strategy": "initial_policy",
                "owned_by": "package",
                "summary": "Unmodified initial scheduler and parameter estimator.",
            },
        )
        return candidate, sources

    def _stage_sources(
        self,
        stager: CandidateBundleStager,
        *,
        candidate_id: str,
        sources: Mapping[str, str],
        lineage: Dict[str, Any],
        strategy: str,
        summary: str,
        prompt_record: Dict[str, Any] | None,
        extra: Dict[str, Any],
    ) -> Dict[str, Any]:
        support_files = self._provenance_files()
        provenance_bytes = support_files[_PROVENANCE_PATH].encode("utf-8")
        generator_extra = {
            "owned_by": "package",
            "summary": _portable_summary(summary),
            "edited_paths": list(self.target_files),
            "provenance_file": _PROVENANCE_PATH,
            "provenance_sha256": hashlib.sha256(provenance_bytes).hexdigest(),
            **extra,
        }
        generator = build_generator_record(
            method_id=self.definition["id"],
            strategy=strategy,
            prompt_record=prompt_record,
            model_record=self._model_record(),
            extra=generator_extra,
        )
        return self._stage_candidate_sources(
            stager,
            candidate_id=candidate_id,
            sources=sources,
            lineage=lineage,
            generator=generator,
            support_files=support_files,
        )

    def _stage_candidate_sources(
        self,
        stager: CandidateBundleStager,
        *,
        candidate_id: str,
        sources: Mapping[str, str],
        lineage: Mapping[str, Any],
        generator: Mapping[str, Any],
        support_files: Mapping[str, str] | None = None,
    ) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="optpilot-policy-stage-") as tmp_dir:
            root = Path(tmp_dir)
            mappings = []
            for relative_path in self.target_files:
                if relative_path not in sources:
                    raise ValueError(f"Cached proposal is missing {relative_path!r}.")
                destination = root / relative_path
                destination.write_text(sources[relative_path], encoding="utf-8")
                mappings.append(CandidateFileMapping(source=destination, path=relative_path))
            for relative_path, content in sorted((support_files or {}).items()):
                if not _path_allowed(relative_path, self.allowed_paths):
                    raise ValueError(
                        f"Candidate support path {relative_path!r} is not allowed."
                    )
                destination = root.joinpath(*PurePosixPath(relative_path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
                mappings.append(CandidateFileMapping(source=destination, path=relative_path))
            return stager.stage_files(
                mappings,
                candidate_id=candidate_id,
                lineage=dict(lineage),
                generator=dict(generator),
            )

    @staticmethod
    def _proposal_descriptor(
        candidate: Mapping[str, Any],
        sources: Mapping[str, str],
        *,
        support_files: Mapping[str, str] | None = None,
    ) -> Dict[str, Any]:
        return {
            "candidate_id": str(candidate["candidate_id"]),
            "sources": dict(sources),
            "lineage": _json_round_trip(candidate.get("lineage", {})),
            "generator": _json_round_trip(candidate.get("generator", {})),
            "support_files": dict(support_files or {}),
        }

    def _restage_cached_proposal(
        self, stager: CandidateBundleStager, cached: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        descriptors = cached.get("candidates", [])
        if not isinstance(descriptors, list):
            raise ValueError("The durable proposal cache is malformed.")
        candidates = []
        for descriptor in descriptors:
            if not isinstance(descriptor, Mapping):
                raise ValueError("The durable proposal cache contains an invalid candidate.")
            candidate_id = str(descriptor.get("candidate_id", ""))
            sources = descriptor.get("sources", {})
            lineage = descriptor.get("lineage", {})
            generator = descriptor.get("generator", {})
            support_files = descriptor.get("support_files", {})
            if not candidate_id or not isinstance(sources, Mapping):
                raise ValueError("The durable proposal cache contains an invalid candidate.")
            candidate = self._stage_candidate_sources(
                stager,
                candidate_id=candidate_id,
                sources={str(key): str(value) for key, value in sources.items()},
                lineage=lineage if isinstance(lineage, Mapping) else {},
                generator=generator if isinstance(generator, Mapping) else {},
                support_files=(
                    {str(key): str(value) for key, value in support_files.items()}
                    if isinstance(support_files, Mapping)
                    else {}
                ),
            )
            self._candidate_sources[candidate_id] = {
                str(key): str(value) for key, value in sources.items()
            }
            candidates.append(candidate)
        return candidates

    def _manager_messages(
        self, requested_plans: int, study_state: Dict[str, Any]
    ) -> List[Dict[str, str]]:
        system = _read_local_prompt(
            "manager.md",
            "You are an operations-research manager. Return JSON only.",
        )
        user_sections = [
            "Design distinct improvements to the decision policy for "
            f"{self.environment_description}",
            f"Return exactly {requested_plans} plans when possible.",
            f"Primary objective: {self.primary_metric} ({self.primary_direction}).",
            f"Current incumbent: {self._elite_id} = {self._elite_score}.",
            f"Completed optimization iterations: {self._iteration}.",
            "Current executable policy:\n"
            + _render_sources(self._elite_sources or {}, self.target_files),
            "Policy interface and snapshot contract:\n" + self.policy_instructions,
            "Recent trial results:\n" + self._render_observations(),
            "Worst-seed trace summary:\n"
            + json.dumps(self._latest_trace_summary, indent=2, sort_keys=True),
            "Environment references:\n" + self._render_references(),
            (
                "You may first return read-only SQLite queries to inspect the replay trace. "
                "Return JSON as {\"queries\":[{\"purpose\":\"...\",\"sql\":\"SELECT ...\"}],"
                "\"plans\":[]}. After query results are supplied, return "
                "{\"queries\":[],\"plans\":[{\"title\":\"...\",\"rationale\":\"...\","
                "\"changes\":[\"...\"],\"expected_effect\":\"...\"}]}."
            ),
            self._entrypoint_contract_line(),
        ]
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(user_sections)},
        ]

    def _run_manager_query_loop(
        self, messages: List[Dict[str, str]], requested_plans: int
    ) -> Dict[str, Any]:
        conversation = [dict(item) for item in messages]
        result = self._chat_json(conversation)
        for query_round in range(self.manager_query_rounds):
            queries = result.get("queries", [])
            if not isinstance(queries, list) or not queries:
                break
            query_results = []
            for query_index, query in enumerate(queries[:4], start=1):
                if not isinstance(query, dict):
                    query_results.append({"index": query_index, "error": "query must be an object"})
                    continue
                sql = str(query.get("sql", ""))
                try:
                    output = _query_trace_database(
                        self._trace_database,
                        sql,
                        max_rows=self.max_query_rows,
                        max_cell_bytes=self.max_query_cell_bytes,
                        max_result_bytes=self.max_query_result_bytes,
                        sqlite_length_limit_bytes=self.sqlite_length_limit_bytes,
                    )
                except Exception as exc:
                    output = {"error": f"{type(exc).__name__}: {exc}"}
                query_results.append(
                    {
                        "index": query_index,
                        "purpose": str(query.get("purpose", "")),
                        "sql": sql,
                        "result": output,
                    }
                )
            conversation.append({"role": "assistant", "content": json.dumps(result)})
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        "Read-only trace query results:\n"
                        + json.dumps(query_results, indent=2, sort_keys=True)
                        + f"\nNow return up to {requested_plans} concrete, distinct plans in the required JSON shape."
                    ),
                }
            )
            result = self._chat_json(conversation)

        # JSON mode guarantees syntax, not the manager schema. Preserve the
        # best valid result while giving a malformed or undersized plan set a
        # bounded chance to self-correct. This is separate from transport
        # retries: no SQL queries are executed during correction.
        best_result = result
        best_plan_count = len(_extract_plans(result, requested_plans))
        for correction_index in range(self.manager_correction_attempts):
            if best_plan_count >= requested_plans:
                break
            conversation.append(
                {"role": "assistant", "content": json.dumps(result, sort_keys=True)}
            )
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        "The manager response did not contain enough valid plans. "
                        "The trace-query budget is now closed. Return JSON with "
                        '"queries":[] and exactly '
                        f"{requested_plans} distinct plans using the required plan fields. "
                        f"This is correction attempt {correction_index + 1}."
                    ),
                }
            )
            result = self._chat_json(conversation)
            plan_count = len(_extract_plans(result, requested_plans))
            if plan_count > best_plan_count:
                best_result = result
                best_plan_count = plan_count
        return best_result

    def _editor_messages(
        self, plan: Dict[str, str], plan_index: int, iteration: int
    ) -> List[Dict[str, str]]:
        system = _read_local_prompt(
            "editor.md",
            "You are an expert Python scheduling-policy editor. Return JSON only.",
        )
        response_shape = {
            "summary": "Concise implementation summary.",
            "files": [
                {"path": path, "content": "Complete replacement Python source."}
                for path in self.target_files
            ],
        }
        user = "\n\n".join(
            [
                f"Implement manager plan {plan_index} for optimization iteration {iteration}.",
                "Plan:\n" + json.dumps(plan, indent=2, sort_keys=True),
                f"Primary objective: {self.primary_metric} ({self.primary_direction}).",
                "Incumbent policy:\n"
                + _render_sources(self._elite_sources or {}, self.target_files),
                "Policy interface and snapshot contract:\n" + self.policy_instructions,
                "Environment references:\n" + self._render_references(),
                "Return JSON only with this shape:\n"
                + json.dumps(response_shape, indent=2),
                (
                    "Each content value is a complete file, not a patch. "
                    + self._entrypoint_contract_line()
                    + " Do not import simulator internals; use only the "
                    "documented policy contract."
                ),
            ]
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _run_editor(
        self, plan: Dict[str, str], messages: List[Dict[str, str]]
    ) -> Tuple[str, Dict[str, str]]:
        errors = []
        conversation = [dict(item) for item in messages]
        for attempt in range(1, self.editor_retries + 1):
            try:
                result = self._chat_json(conversation)
                summary = str(result.get("summary") or plan.get("title") or "Policy edit")
                edited = _extract_files(result, self.target_files)
                sources = dict(self._elite_sources or {})
                sources.update(edited)
                _validate_policy_sources(
                    sources,
                    editable_files=self.target_files,
                    policy_validation=self.policy_validation,
                )
                return summary, sources
            except Exception as exc:
                if isinstance(exc, _LLMRequestError) and not isinstance(
                    exc, _CorrectableLLMOutputError
                ):
                    raise
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                if attempt < self.editor_retries:
                    conversation.append(
                        {
                            "role": "user",
                            "content": (
                                "The previous response failed validation: "
                                f"{type(exc).__name__}: {exc}. Return corrected complete files as JSON."
                            ),
                        }
                    )
        raise RuntimeError("; ".join(errors))

    def _chat_json(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        api_key_env = str(self.config.get("apiKeyEnvVar", "OPENROUTER_API_KEY"))
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise _LLMRequestError(
                f"ProcessAwareLLMHeuristicMethod requires {api_key_env}. Declare it in "
                "runtime.envFromHost and configure it for the Run."
            )
        payload: Dict[str, Any] = {
            "model": str(self.config.get("model", _DEFAULT_MODEL)),
            "temperature": float(self.config.get("temperature", 0.2)),
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        if self.config.get("maxTokens") is not None:
            payload["max_tokens"] = int(self.config["maxTokens"])
        if str(self.config.get("provider", "openrouter")).lower() == "openrouter":
            payload["provider"] = {
                "allow_fallbacks": bool(
                    self.config.get("allowProviderFallbacks", True)
                ),
                "require_parameters": bool(
                    self.config.get("requireProviderParameters", True)
                ),
            }

        attempts = self.request_retries + 1
        last_error: _RetryableLLMError | None = None
        for attempt_index in range(attempts):
            try:
                return self._chat_json_once(payload, api_key)
            except _RetryableLLMError as exc:
                last_error = exc
                if attempt_index + 1 >= attempts:
                    break
                retry_delay = min(
                    self.retry_max_seconds,
                    self.retry_base_seconds * (2**attempt_index),
                )
                if exc.retry_after_seconds is not None:
                    retry_delay = min(
                        self.retry_max_seconds,
                        max(retry_delay, exc.retry_after_seconds),
                    )
                if retry_delay > 0.0:
                    time.sleep(retry_delay)
        assert last_error is not None
        failure_type = (
            _CorrectableLLMOutputError
            if last_error.correctable_output
            else _LLMRequestError
        )
        raise failure_type(
            f"LLM request failed after {attempts} bounded attempts: {last_error}"
        ) from last_error

    def _chat_json_once(
        self, payload: Mapping[str, Any], api_key: str
    ) -> Dict[str, Any]:
        request = urllib.request.Request(
            str(self.config.get("apiBase", "https://openrouter.ai/api/v1/chat/completions")),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        raw: str | None = None
        transport_failure: _LLMRequestError | None = None
        try:
            with urllib.request.urlopen(
                request, timeout=self.request_timeout_seconds
            ) as response:
                raw = _read_bounded_http_body(
                    response,
                    max_bytes=self.max_http_response_bytes,
                    label="LLM response",
                ).decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                numeric_status = int(exc.code)
            except (TypeError, ValueError):
                numeric_status = None
            status = str(numeric_status) if numeric_status is not None else "unknown"
            try:
                detail = _read_bounded_http_body(
                    exc,
                    max_bytes=self.max_http_error_bytes,
                    label="LLM HTTP error response",
                ).decode("utf-8", errors="replace")
                detail = _redact_provider_text(detail, secrets=(api_key,))
            except Exception:
                detail = "Provider error body was unavailable or exceeded its limit."
            message = (
                f"LLM request failed with HTTP {status}: "
                + _bounded_utf8_text(detail, 4096)
            )
            try:
                retry_after = _numeric_retry_after(exc.headers)
            except Exception:
                retry_after = None
            if numeric_status in _TRANSIENT_HTTP_STATUS_CODES:
                transport_failure = _RetryableLLMError(
                    message,
                    retry_after_seconds=retry_after,
                )
            else:
                transport_failure = _LLMRequestError(message)
        except urllib.error.URLError as exc:
            try:
                reason_text = str(exc.reason)
            except Exception:
                reason_text = "provider transport error"
            reason = _bounded_utf8_text(
                _redact_provider_text(reason_text, secrets=(api_key,)), 1000
            )
            transport_failure = _RetryableLLMError(
                f"LLM request failed: {reason}"
            )
        except TimeoutError:
            transport_failure = _RetryableLLMError("LLM request timed out.")
        except Exception:
            transport_failure = _RetryableLLMError(
                "LLM request failed while opening or reading the provider response."
            )

        # Raise only after leaving the provider-exception handler. This keeps
        # the original exception out of both __cause__ and __context__, which
        # retained workers serialize as a full traceback chain.
        if transport_failure is not None:
            raise transport_failure
        if raw is None:  # defensive: urlopen returned without data or failure
            raise _RetryableLLMError("LLM endpoint returned no response body.")

        envelope_failure: _RetryableLLMError | None = None
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            envelope = None
            envelope_failure = _RetryableLLMError(
                "LLM endpoint returned a non-JSON response envelope."
            )
        if envelope_failure is not None:
            raise envelope_failure
        wire_content, content, response_metadata = _chat_completion_content(
            envelope,
            secrets=(api_key,),
        )
        parse_failure: _RetryableLLMError | None = None
        try:
            parsed_unredacted = _parse_json_object(content)
        except (json.JSONDecodeError, ValueError):
            parsed_unredacted = {}
            parse_failure = _RetryableLLMError(
                "LLM completion did not contain a complete JSON object.",
                correctable_output=True,
            )
        if parse_failure is not None:
            raise parse_failure
        parsed = _redact_provider_value(parsed_unredacted, secrets=(api_key,))
        if parsed != parsed_unredacted:
            # The raw completion is a JSON container, so applying credential
            # regexes to it would corrupt ordinary source code. When
            # field-aware redaction changed parsed data, retain a canonical
            # sanitized rendering instead of the unsafe wire spelling.
            wire_content = _canonical_json_bytes(parsed).decode("utf-8")
        self._record_llm_exchange(
            payload,
            wire_content,
            parsed,
            response_metadata=response_metadata,
        )
        return parsed

    def _replay_incumbent(self, observation: Dict[str, Any]) -> None:
        metrics = dict(observation.get("metric_values", {}))
        worst_seed = metrics.get(self.replay_seed_metric)
        worst_score = metrics.get(self.replay_score_metric)
        if not isinstance(worst_seed, (int, float)):
            raise RuntimeError(
                "Successful evaluation did not report numeric "
                f"{self.replay_seed_metric}."
            )
        if not isinstance(worst_score, (int, float)):
            raise RuntimeError(
                "Successful evaluation did not report numeric "
                f"{self.replay_score_metric}."
            )

        candidate_dir = Path(self._trace_workspace.name) / "candidate"
        if candidate_dir.exists():
            shutil.rmtree(candidate_dir)
        candidate_dir.mkdir(parents=True)
        assert self._elite_sources is not None
        for relative_path, source in self._elite_sources.items():
            (candidate_dir / relative_path).write_text(source, encoding="utf-8")
        if self._trace_database.exists():
            self._trace_database.unlink()

        result = self._run_replay_subprocess(
            candidate_dir=candidate_dir,
            settings=dict(self._replay_settings),
            seed=int(worst_seed),
            database_path=self._trace_database,
        )
        if not self._trace_database.is_file():
            raise RuntimeError("Worst-seed replay did not create the requested SQLite trace.")
        replay_score = _extract_total_score(result, self.replay_score_keys)
        tolerance = max(1e-6, abs(float(worst_score)) * 1e-6)
        if not math.isclose(replay_score, float(worst_score), rel_tol=0.0, abs_tol=tolerance):
            raise RuntimeError(
                "Worst-seed replay did not reproduce its evaluation: "
                f"observed={float(worst_score):.12g}, replayed={replay_score:.12g}."
            )
        self._latest_trace_summary = _summarize_trace_database(
            self._trace_database,
            max_tables=40,
            max_cell_bytes=self.max_query_cell_bytes,
            max_result_bytes=self.max_query_result_bytes,
            sqlite_length_limit_bytes=self.sqlite_length_limit_bytes,
        )
        self._latest_trace_summary.update(
            {
                "candidate_id": self._elite_id,
                "seed": int(worst_seed),
                "total_score": replay_score,
            }
        )

    def _run_replay_subprocess(
        self,
        *,
        candidate_dir: Path,
        settings: Mapping[str, Any],
        seed: int,
        database_path: Path,
    ) -> Dict[str, Any]:
        """Run exact-seed replay outside the API-secret-bearing method process."""

        environment_root = _find_callable_import_root(self.replay_callable)
        worker = Path(__file__).with_name("replay_worker.py").resolve()
        if not worker.is_file():
            raise RuntimeError("The isolated replay worker is unavailable.")
        request_path = Path(self._trace_workspace.name) / "replay_request.json"
        result_path = Path(self._trace_workspace.name) / "replay_result.json"
        request_payload = {
            "candidate_dir": str(candidate_dir.resolve()),
            "database_path": str(database_path.resolve()),
            "seed": int(seed),
            "settings": _json_round_trip(settings),
        }
        _write_bounded_json(
            request_path,
            request_payload,
            max_bytes=self.max_replay_result_bytes,
        )
        if result_path.exists():
            result_path.unlink()
        command = [
            sys.executable,
            "-I",
            "-B",
            str(worker),
            "--environment-root",
            str(environment_root),
            "--environment-callable",
            self.replay_callable,
            "--request",
            str(request_path),
            "--result",
            str(result_path),
            "--max-result-bytes",
            str(self.max_replay_result_bytes),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self._trace_workspace.name,
                env=_sanitized_replay_environment(
                    str(self.config.get("apiKeyEnvVar", "OPENROUTER_API_KEY"))
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.replay_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Worst-seed replay exceeded {self.replay_timeout_seconds} seconds."
            ) from exc
        payload = _read_bounded_json_file(
            result_path,
            max_bytes=self.max_replay_result_bytes,
            label="isolated replay result",
        )
        if completed.returncode != 0 or payload.get("ok") is not True:
            error_type = str(payload.get("error_type", "ReplayWorkerError"))
            message = str(payload.get("message", "isolated replay failed"))
            raise RuntimeError(f"{error_type}: {message}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Isolated replay returned a malformed result.")
        return result

    def _load_replay_settings(self) -> Dict[str, Any]:
        matches = [
            item for item in self.references
            if str(item.get("name", "")) == "replay_settings" and item.get("path")
        ]
        if len(matches) != 1:
            raise ValueError(
                "ProcessAwareLLMHeuristicMethod requires exactly one replay_settings reference."
            )
        path = Path(str(matches[0]["path"]))
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("The replay_settings reference must contain a JSON object.")
        return dict(payload)

    def _render_observations(self) -> str:
        rows = []
        for item in self._observations[-12:]:
            rows.append(
                {
                    "candidate_id": _candidate_id(item),
                    "status": item.get("status"),
                    "metrics": item.get("metric_values", {}),
                }
            )
        return json.dumps(rows, indent=2, sort_keys=True)

    def _render_references(self) -> str:
        excluded_types = {"candidate_template", "replay_settings"}
        remaining = int(self.config.get("maxReferenceBytes", 32000))
        per_file = int(self.config.get("maxReferenceFileBytes", 16000))
        blocks = []
        for reference in self.references:
            if (
                str(reference.get("type", "")) in excluded_types
                or str(reference.get("name", "")) == "replay_settings"
                or not reference.get("path")
            ):
                continue
            if remaining <= 0:
                blocks.append("[additional references omitted]")
                break
            path = Path(str(reference["path"]))
            header = {
                "name": reference.get("name", path.name),
                "type": reference.get("type"),
                "description": reference.get("description"),
            }
            if not path.is_file():
                blocks.append(f"REFERENCE {json.dumps(header)}\n[unavailable]")
                continue
            limit = min(per_file, remaining)
            data = path.read_bytes()[: limit + 1]
            truncated = len(data) > limit
            sample = data[:limit]
            remaining -= len(sample)
            text = sample.decode("utf-8", errors="replace")
            blocks.append(
                f"REFERENCE {json.dumps(header, sort_keys=True)}\n{text}"
                + ("\n[truncated]" if truncated else "")
            )
        return "\n\n".join(blocks) if blocks else "No additional readable references."

    def _configure_runtime_services(self, runtime_context: Dict[str, Any]) -> None:
        """Bind only runtime-projected writable locations; never invent host paths."""

        state_value = runtime_context.get("method_state_dir")
        if state_value:
            state_root = _prepare_runtime_directory(state_value, "method_state_dir")
            state_path = state_root / "process-aware-llm-state.json"
            if self._state_cache_path is not None and self._state_cache_path != state_path:
                raise ValueError("runtime_context.method_state_dir changed during the Run.")
            if self._state_cache_path is None:
                self._state_cache_path = state_path
                self._trace_database = state_root / "worst_run.db"
            if not self._state_cache_loaded:
                self._load_state()
                self._state_cache_loaded = True

        prompt_value = runtime_context.get("prompt_store_dir")
        if prompt_value:
            prompt_root = _prepare_runtime_directory(prompt_value, "prompt_store_dir")
        elif self._state_cache_path is not None:
            prompt_root = _prepare_runtime_directory(
                self._state_cache_path.parent / "prompts", "method-state prompt directory"
            )
        else:
            prompt_root = None
        if (
            self._prompt_store_dir is not None
            and prompt_root is not None
            and self._prompt_store_dir != prompt_root
        ):
            raise ValueError("runtime_context.prompt_store_dir changed during the Run.")
        if prompt_root is not None:
            self._prompt_store_dir = prompt_root

    def _store_prompt(
        self, messages: List[Dict[str, str]], metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        payload = {
            "schema": _PROMPT_SCHEMA,
            "messages": [dict(item) for item in messages],
            "metadata": dict(metadata),
        }
        encoded = _canonical_json_bytes(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        record = {
            "schema": _PROMPT_SCHEMA,
            "prompt_record_id": f"prompt-{digest[:24]}",
            "sha256": digest,
            "sizeBytes": len(encoded),
            "message_count": len(messages),
            "metadata": dict(metadata),
        }
        self._prompt_payloads[digest] = {
            "prompt_record_id": record["prompt_record_id"],
            "sha256": digest,
            "payload": payload,
        }
        if self._prompt_store_dir is None:
            return record
        with self._prompt_store_lock:
            path = self._prompt_store_dir / f"prompt-{digest}.json"
            if path.exists():
                existing = path.read_bytes()
                if existing != encoded:
                    raise RuntimeError("A content-addressed prompt record was corrupted.")
            else:
                _atomic_write_bytes(path, encoded)
        # Physical paths are intentionally absent from candidate metadata:
        # retained file-candidate provenance must remain portable.
        return record

    def _model_record(self) -> Dict[str, Any]:
        # Keep generator metadata deterministic.  The generic provenance helper
        # adds a wall-clock timestamp, which is inappropriate for replay keys.
        return {
            "provider": str(self.config.get("provider", "openrouter")),
            "model": str(self.config.get("model", _DEFAULT_MODEL)),
            "parameters": {
                "temperature": float(self.config.get("temperature", 0.2)),
                "max_tokens": int(self.config.get("maxTokens", 0) or 0),
                "request_retries": self.request_retries,
                "require_provider_parameters": bool(
                    self.config.get("requireProviderParameters", True)
                ),
            },
        }

    def _record_llm_exchange(
        self,
        request_payload: Mapping[str, Any],
        response_content: Any,
        parsed_response: Mapping[str, Any],
        *,
        response_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        request_value = _json_round_trip(request_payload)
        response_value = _json_round_trip(response_content)
        record = {
            "provider": str(self.config.get("provider", "openrouter")),
            "request": request_value,
            "request_sha256": _canonical_digest(request_value),
            "response_content": response_value,
            "response_sha256": _canonical_digest(response_value),
            "parsed_response": _json_round_trip(parsed_response),
        }
        if response_metadata:
            record["response_metadata"] = _json_round_trip(response_metadata)
        exchange_id = _canonical_digest(record)
        with self._provenance_lock:
            updated = dict(self._llm_exchanges)
            updated[exchange_id] = record
            self._encode_provenance(updated)
            self._llm_exchanges = updated

    def _encode_provenance(
        self, exchanges: Mapping[str, Mapping[str, Any]] | None = None
    ) -> bytes:
        selected_exchanges = self._llm_exchanges if exchanges is None else exchanges
        payload = {
            "schema": _PROVENANCE_SCHEMA,
            "method_id": self.definition["id"],
            "model": self._model_record(),
            "prompts": [
                self._prompt_payloads[key] for key in sorted(self._prompt_payloads)
            ],
            "exchanges": [
                {"exchange_id": key, **selected_exchanges[key]}
                for key in sorted(selected_exchanges)
            ],
        }
        encoded = _canonical_json_bytes(payload)
        if len(encoded) > self.max_provenance_bytes:
            raise RuntimeError("LLM provenance exceeds maxProvenanceBytes.")
        return encoded

    def _provenance_files(self) -> Dict[str, str]:
        return {_PROVENANCE_PATH: self._encode_provenance().decode("utf-8")}

    def _proposal_request_digest(
        self, n_candidates: int, study_state: Mapping[str, Any]
    ) -> str:
        semantic_state = dict(study_state)
        semantic_state.pop("runtime_context", None)
        return _canonical_digest(
            {
                "method_id": self.definition["id"],
                "n_candidates": int(n_candidates),
                "study_state": _json_round_trip(semantic_state),
            }
        )

    def _phase_digest(self) -> str:
        return _canonical_digest(
            {
                "baseline_emitted": self._baseline_emitted,
                "iteration": self._iteration,
                "no_improvement_rounds": self._no_improvement_rounds,
                "stopped": self._stopped,
                "elite_id": self._elite_id,
                "elite_score": self._elite_score,
                "candidate_sources": self._candidate_sources,
                "observations": self._observations,
                "llm_exchanges": self._llm_exchanges,
                "prompt_payloads": self._prompt_payloads,
            }
        )

    def _remember_proposal(
        self,
        request_digest: str,
        phase_before: str,
        descriptors: List[Dict[str, Any]],
    ) -> None:
        # Only the most recent proposal can be awaiting response recovery.
        # Replacing the older entry keeps durable state bounded even though
        # each generated candidate carries the generation's full provenance.
        self._proposal_cache = {
            request_digest: {
                "phase_before": phase_before,
                "phase_after": self._phase_digest(),
                "candidates": _json_round_trip(descriptors),
            }
        }

    def _state_payload(self) -> Dict[str, Any]:
        return {
            "baseline_emitted": self._baseline_emitted,
            "iteration": self._iteration,
            "no_improvement_rounds": self._no_improvement_rounds,
            "stopped": self._stopped,
            "elite_id": self._elite_id,
            "elite_score": self._elite_score,
            "elite_sources": self._elite_sources,
            "candidate_sources": self._candidate_sources,
            "observations": self._observations,
            "latest_trace_summary": self._latest_trace_summary,
            "proposal_cache": self._proposal_cache,
            "llm_exchanges": self._llm_exchanges,
            "prompt_payloads": self._prompt_payloads,
        }

    def _persist_state(self) -> None:
        if self._state_cache_path is None:
            return
        envelope = {
            "schema": _STATE_SCHEMA,
            "method_id": self.definition["id"],
            "state": self._state_payload(),
        }
        encoded = _canonical_json_bytes(envelope)
        if len(encoded) > self.max_state_cache_bytes:
            raise RuntimeError("The method state exceeds maxStateCacheBytes.")
        with self._state_lock:
            _atomic_write_bytes(self._state_cache_path, encoded)

    def _load_state(self) -> None:
        assert self._state_cache_path is not None
        if not self._state_cache_path.exists():
            return
        envelope = _read_bounded_json_file(
            self._state_cache_path,
            max_bytes=self.max_state_cache_bytes,
            label="method state cache",
        )
        if (
            envelope.get("schema") != _STATE_SCHEMA
            or envelope.get("method_id") != self.definition["id"]
            or not isinstance(envelope.get("state"), dict)
        ):
            raise ValueError("The method state cache is incompatible.")
        state = envelope["state"]
        self._baseline_emitted = bool(state.get("baseline_emitted", False))
        self._iteration = int(state.get("iteration", 0))
        self._no_improvement_rounds = int(state.get("no_improvement_rounds", 0))
        self._stopped = bool(state.get("stopped", False))
        self._elite_id = (
            None if state.get("elite_id") is None else str(state["elite_id"])
        )
        self._elite_score = (
            None if state.get("elite_score") is None else float(state["elite_score"])
        )
        elite_sources = state.get("elite_sources")
        self._elite_sources = (
            None
            if elite_sources is None
            else {str(key): str(value) for key, value in dict(elite_sources).items()}
        )
        self._candidate_sources = {
            str(candidate_id): {
                str(path): str(source) for path, source in dict(sources).items()
            }
            for candidate_id, sources in dict(state.get("candidate_sources", {})).items()
        }
        observations = state.get("observations", [])
        self._observations = [dict(item) for item in observations]
        self._latest_trace_summary = dict(state.get("latest_trace_summary", {}))
        self._proposal_cache = {
            str(key): dict(value)
            for key, value in dict(state.get("proposal_cache", {})).items()
        }
        self._llm_exchanges = {
            str(key): dict(value)
            for key, value in dict(state.get("llm_exchanges", {})).items()
        }
        self._prompt_payloads = {
            str(key): dict(value)
            for key, value in dict(state.get("prompt_payloads", {})).items()
        }
        self._encode_provenance()

    def _should_stop(self) -> bool:
        if self._elite_score is not None:
            if self.primary_direction == "maximize" and self._elite_score >= self.target_score:
                return True
            if self.primary_direction == "minimize" and self._elite_score <= self.target_score:
                return True
        return (
            self._iteration >= self.max_iterations
            or self._no_improvement_rounds >= self.patience
        )


def _method_references(candidate_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    method_context = candidate_context.get("methodContext", {})
    if not isinstance(method_context, dict):
        return []
    return [dict(item) for item in method_context.get("references", []) if isinstance(item, dict)]


def _load_policy_instructions(candidate_context: Dict[str, Any]) -> str:
    method_context = candidate_context.get("methodContext", {})
    if not isinstance(method_context, dict):
        raise ValueError("methodContext must be an object.")
    paths = method_context.get("instructions", []) or []
    if not paths:
        raise ValueError("At least one methodContext instruction file is required.")
    blocks = []
    for value in paths:
        path = Path(str(value))
        if not path.is_file():
            raise FileNotFoundError(f"Method instruction file is unavailable: {path}")
        blocks.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(blocks)


def _editable_paths(candidate_context: Dict[str, Any]) -> List[str]:
    files = candidate_context.get("files", {})
    editable = files.get("editable", []) or []
    paths = [str(item["path"]) for item in editable if isinstance(item, dict) and item.get("path")]
    if paths:
        return paths
    return [str(path) for path in files.get("required", []) or []]


def _path_allowed(path: str, patterns: Sequence[str]) -> bool:
    candidate = PurePosixPath(path)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or str(candidate) != path
    ):
        return False
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _resolve_template_files(
    target_files: Sequence[str], candidate_context: Dict[str, Any]
) -> Dict[str, Path]:
    templates = {
        str(item.get("name")): Path(str(item.get("path")))
        for item in _method_references(candidate_context)
        if item.get("type") == "candidate_template" and item.get("name") and item.get("path")
    }
    resolved = {}
    for relative_path in target_files:
        source = templates.get(relative_path)
        if source is None or not source.is_file():
            raise FileNotFoundError(
                "A candidate_template reference named "
                f"{relative_path!r} is required and must be a regular file."
            )
        resolved[relative_path] = source
    return resolved


def _extract_files(payload: Dict[str, Any], allowed_paths: Iterable[str]) -> Dict[str, str]:
    allowed = set(allowed_paths)
    items = payload.get("files")
    if not isinstance(items, list) or not items:
        raise ValueError("Response must contain a non-empty files array.")
    result = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"files[{index}] must be an object.")
        path = str(item.get("path", ""))
        content = item.get("content")
        if path not in allowed:
            raise ValueError(f"files[{index}].path {path!r} is not editable.")
        if not isinstance(content, str):
            raise ValueError(f"files[{index}].content must be a string.")
        if path in result:
            raise ValueError(f"Duplicate edited path: {path!r}.")
        result[path] = content
    return result


def _validate_policy_sources(
    sources: Mapping[str, str],
    *,
    editable_files: Sequence[str],
    policy_validation: Mapping[str, Any],
) -> None:
    """Apply the environment-declared policy contract to candidate sources.

    The rules (entrypoint shape, forbidden imports/names, string lints) come
    entirely from the environment's ``policyValidation`` declaration (F5) and
    are checked by the shared core checker; this method carries no
    domain-specific validation of its own.
    """

    for path in editable_files:
        if path not in sources:
            raise ValueError(f"Missing required policy file {path!r}.")
        ast.parse(sources[path], filename=path)
    errors = validate_declared_policy_sources(dict(sources), policy_validation)
    if errors:
        raise ValueError("; ".join(errors[:5]))


def _extract_plans(payload: Dict[str, Any], limit: int) -> List[Dict[str, str]]:
    plans = payload.get("plans")
    if not isinstance(plans, list):
        return []
    result = []
    for index, plan in enumerate(plans[:limit], start=1):
        if not isinstance(plan, dict):
            continue
        title = str(plan.get("title") or f"Plan {index}").strip()
        rationale = str(plan.get("rationale") or "").strip()
        changes_value = plan.get("changes", [])
        if isinstance(changes_value, list):
            changes = "\n".join(f"- {str(value)}" for value in changes_value)
        else:
            changes = str(changes_value)
        expected = str(plan.get("expected_effect") or "").strip()
        if not rationale and not changes:
            continue
        result.append(
            {
                "title": title,
                "rationale": rationale,
                "changes": changes,
                "expected_effect": expected,
            }
        )
    return result


def _render_sources(sources: Mapping[str, str], ordering: Sequence[str] = ()) -> str:
    ordered = [path for path in ordering if path in sources]
    ordered += [path for path in sorted(sources) if path not in ordered]
    return "\n\n".join(
        f"FILE: {path}\n```python\n{sources[path]}\n```" for path in ordered
    )


def _candidate_id(observation: Mapping[str, Any]) -> str | None:
    direct = observation.get("candidate_id")
    if direct:
        return str(direct)
    candidate = observation.get("candidate")
    if isinstance(candidate, Mapping) and candidate.get("candidate_id"):
        return str(candidate["candidate_id"])
    return None


def _is_better(candidate: float, incumbent: float, direction: str) -> bool:
    return candidate < incumbent if direction == "minimize" else candidate > incumbent


def _extract_total_score(
    payload: Any,
    score_keys: Sequence[str] = ("total_score", "mean_total_score", "total_score_mean"),
) -> float:
    if not isinstance(payload, Mapping):
        raise RuntimeError("The replay callable must return a mapping.")
    containers = [payload]
    for key in ("metric_values", "metrics", "kpi", "raw_kpis"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    for container in containers:
        for key in score_keys:
            value = container.get(key)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                return float(value)
    raise RuntimeError("Worst-seed replay result did not contain a numeric total score.")


def _chat_completion_content(
    envelope: Any,
    *,
    secrets: Sequence[str] = (),
) -> Tuple[Any, str, Dict[str, Any]]:
    """Validate one OpenAI-compatible non-streaming completion envelope."""

    if not isinstance(envelope, Mapping):
        raise _RetryableLLMError("LLM response envelope must be a JSON object.")
    if envelope.get("error") not in (None, {}):
        raise _api_envelope_error(
            "LLM endpoint error", envelope["error"], secrets=secrets
        )
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _RetryableLLMError("LLM response did not contain any choices.")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise _RetryableLLMError("LLM response choice must be a JSON object.")
    if choice.get("error") not in (None, {}):
        raise _api_envelope_error(
            "LLM provider error", choice["error"], secrets=secrets
        )

    finish_reason = choice.get("finish_reason")
    if finish_reason in {"content_filter", "error", "length"}:
        raise _RetryableLLMError(
            f"LLM completion ended before valid output: finish_reason={finish_reason}.",
            correctable_output=True,
        )
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise _RetryableLLMError("LLM response choice did not contain a message object.")
    wire_content = _redact_provider_value(
        message.get("content"),
        secrets=secrets,
    )
    if isinstance(wire_content, str):
        content = wire_content
    elif isinstance(wire_content, list):
        pieces = []
        for item in wire_content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                pieces.append(str(item["text"]))
        content = "".join(pieces)
    else:
        raise _RetryableLLMError(
            "LLM response message content must be text.", correctable_output=True
        )
    if not content.strip():
        raise _RetryableLLMError(
            "LLM response message content was empty.", correctable_output=True
        )

    metadata: Dict[str, Any] = {}
    for source, target in (
        ("model", "model"),
        ("provider", "provider"),
    ):
        value = envelope.get(source)
        if isinstance(value, str) and value:
            metadata[target] = _bounded_utf8_text(
                _redact_provider_text(value, secrets=secrets), 512
            )
    for source, target in (
        ("finish_reason", "finish_reason"),
        ("native_finish_reason", "native_finish_reason"),
    ):
        value = choice.get(source)
        if isinstance(value, str) and value:
            metadata[target] = _bounded_utf8_text(
                _redact_provider_text(value, secrets=secrets), 128
            )
    usage = envelope.get("usage")
    if isinstance(usage, Mapping):
        safe_usage: Dict[str, int | float] = {}
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cost",
            "reasoning_tokens",
            "cached_tokens",
        ):
            value = usage.get(key)
            if (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
            ):
                safe_usage[key] = value
        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, Mapping):
            reasoning_tokens = completion_details.get("reasoning_tokens")
            if (
                "reasoning_tokens" not in safe_usage
                and not isinstance(reasoning_tokens, bool)
                and isinstance(reasoning_tokens, (int, float))
                and math.isfinite(float(reasoning_tokens))
            ):
                safe_usage["reasoning_tokens"] = reasoning_tokens
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, Mapping):
            cached_tokens = prompt_details.get("cached_tokens")
            if (
                "cached_tokens" not in safe_usage
                and not isinstance(cached_tokens, bool)
                and isinstance(cached_tokens, (int, float))
                and math.isfinite(float(cached_tokens))
            ):
                safe_usage["cached_tokens"] = cached_tokens
        if safe_usage:
            metadata["usage"] = safe_usage
    return wire_content, content, metadata


def _api_envelope_error(
    label: str,
    error: Any,
    *,
    secrets: Sequence[str] = (),
) -> _LLMRequestError:
    """Return a bounded typed error without retaining provider-owned payloads."""

    code: Any = None
    error_type: Any = None
    message: Any = None
    if isinstance(error, Mapping):
        code = error.get("code")
        error_type = error.get("type")
        message = error.get("message")
        metadata = error.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("error_type") is not None:
            error_type = metadata.get("error_type")
    else:
        message = error
    parts = []
    if code is not None:
        parts.append(
            "code="
            + _bounded_utf8_text(
                _redact_provider_text(str(code), secrets=secrets), 128
            )
        )
    if error_type is not None:
        parts.append(
            "type="
            + _bounded_utf8_text(
                _redact_provider_text(str(error_type), secrets=secrets), 128
            )
        )
    if message is not None:
        parts.append(
            _bounded_utf8_text(
                _redact_provider_text(str(message), secrets=secrets), 1000
            )
        )
    description = f"{label}: " + ("; ".join(parts) or "unspecified provider error")

    numeric_code: int | None = None
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        pass
    normalized_type = str(error_type or code or "").strip().lower()
    if (
        numeric_code in _TRANSIENT_HTTP_STATUS_CODES
        or normalized_type in _TRANSIENT_API_ERROR_TYPES
    ):
        return _RetryableLLMError(description)
    return _LLMRequestError(description)


def _redact_provider_value(value: Any, *, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        # Successful completion strings may be source code. Replace only
        # exact configured secrets here; broad credential regexes belong on
        # diagnostic text or structurally identified credential fields.
        return _redact_exact_provider_text(value, secrets=secrets)
    if isinstance(value, list):
        return [
            _redact_provider_value(item, secrets=secrets)
            for item in value
        ]
    if isinstance(value, Mapping):
        redacted: Dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = (
                _redact_provider_text(key, secrets=secrets)
                if isinstance(key, str)
                else key
            )
            # Two provider-controlled keys can collapse to the same redacted
            # spelling. Preserve both values without restoring either secret.
            if safe_key in redacted:
                base_key = str(safe_key)
                suffix = 2
                while f"{base_key}#{suffix}" in redacted:
                    suffix += 1
                safe_key = f"{base_key}#{suffix}"
            if isinstance(key, str) and _is_provider_credential_field(key):
                safe_item = "[REDACTED]"
            else:
                safe_item = _redact_provider_value(item, secrets=secrets)
            redacted[safe_key] = safe_item
        return redacted
    return value


def _redact_exact_provider_text(value: str, *, secrets: Sequence[str]) -> str:
    redacted = str(value)
    for secret in sorted(
        {str(item) for item in secrets if str(item)},
        key=len,
        reverse=True,
    ):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact_provider_text(value: str, *, secrets: Sequence[str]) -> str:
    redacted = _redact_exact_provider_text(value, secrets=secrets)
    for pattern in _PROVIDER_CREDENTIAL_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def _is_provider_credential_field(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return normalized in {
        "apikey",
        "accesstoken",
        "refreshtoken",
        "token",
        "secret",
        "password",
        "credential",
        "authorization",
        "cookie",
    }


def _parse_json_object(content: str) -> Dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def _query_trace_database(
    database_path: Path,
    sql: str,
    *,
    max_rows: int,
    max_cell_bytes: int = _DEFAULT_QUERY_CELL_BYTES,
    max_result_bytes: int = _DEFAULT_QUERY_RESULT_BYTES,
    sqlite_length_limit_bytes: int = _DEFAULT_SQLITE_LENGTH_BYTES,
) -> Dict[str, Any]:
    if not database_path.is_file():
        raise FileNotFoundError("No incumbent replay trace is available.")
    statement = sql.strip()
    if not statement or len(statement) > 4000:
        raise ValueError("SQL query must contain between 1 and 4000 characters.")
    if "\x00" in statement or _SQL_MUTATION_WORDS.search(statement):
        raise ValueError("Only read-only SELECT/WITH/PRAGMA table_info queries are allowed.")
    normalized = re.sub(r"^(?:\s|--[^\n]*\n|/\*.*?\*/)*", "", statement, flags=re.DOTALL)
    first_word = normalized.split(None, 1)[0].rstrip(";").upper() if normalized else ""
    if first_word not in {"SELECT", "WITH", "PRAGMA"}:
        raise ValueError("Query must begin with SELECT, WITH, or PRAGMA.")
    if first_word == "PRAGMA" and not re.match(
        r"(?is)^PRAGMA\s+(?:main\.)?(?:table_info|table_xinfo|table_list)\s*(?:\(|;|$)",
        normalized,
    ):
        raise ValueError("Only schema-inspection PRAGMA queries are allowed.")

    uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, sqlite_length_limit_bytes)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 8192)
            connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 128)
            connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 16)
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 128)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        steps = 0

        def progress() -> int:
            nonlocal steps
            steps += 1
            return 1 if steps > 2000 else 0

        connection.set_progress_handler(progress, 1000)
        cursor = connection.execute(statement)
        columns = [
            _bounded_utf8_text(str(item[0]), min(max_cell_bytes, 512))
            for item in (cursor.description or [])
        ]
        result: Dict[str, Any] = {
            "columns": columns,
            "rows": [],
            "truncated": False,
        }
        if len(_canonical_json_bytes(result)) > max_result_bytes:
            raise ValueError("SQL column metadata exceeds maxQueryResultBytes.")
        for index in range(max_rows + 1):
            row = cursor.fetchone()
            if row is None:
                break
            if index >= max_rows:
                result["truncated"] = True
                break
            safe_row = [_json_safe(value, max_cell_bytes=max_cell_bytes) for value in row]
            trial = {
                "columns": columns,
                "rows": [*result["rows"], safe_row],
                # Reserve the larger spelling so marking truncation cannot
                # push the final payload over the configured byte ceiling.
                "truncated": True,
            }
            if len(_canonical_json_bytes(trial)) > max_result_bytes:
                result["truncated"] = True
                break
            result["rows"].append(safe_row)
        if len(_canonical_json_bytes(result)) > max_result_bytes:
            raise RuntimeError("SQL result bounding failed.")
        return result
    finally:
        connection.close()


def _summarize_trace_database(
    database_path: Path,
    *,
    max_tables: int,
    max_cell_bytes: int = _DEFAULT_QUERY_CELL_BYTES,
    max_result_bytes: int = _DEFAULT_QUERY_RESULT_BYTES,
    sqlite_length_limit_bytes: int = _DEFAULT_SQLITE_LENGTH_BYTES,
) -> Dict[str, Any]:
    query_limits = {
        "max_cell_bytes": max_cell_bytes,
        "max_result_bytes": max_result_bytes,
        "sqlite_length_limit_bytes": sqlite_length_limit_bytes,
    }
    tables_payload = _query_trace_database(
        database_path,
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        max_rows=max_tables,
        **query_limits,
    )
    tables = [str(row[0]) for row in tables_payload["rows"] if row]
    row_counts = {}
    schemas = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        try:
            count_payload = _query_trace_database(
                database_path,
                f"SELECT COUNT(*) AS n FROM {quoted}",
                max_rows=1,
                **query_limits,
            )
            row_counts[table] = int(count_payload["rows"][0][0])
            schema_payload = _query_trace_database(
                database_path,
                f"PRAGMA table_info({quoted})",
                max_rows=100,
                **query_limits,
            )
            schemas[table] = [row[1] for row in schema_payload["rows"] if len(row) > 1]
        except Exception as exc:
            row_counts[table] = f"unavailable: {type(exc).__name__}"
    return {
        "tables": tables,
        "table_list_truncated": bool(tables_payload.get("truncated")),
        "row_counts": row_counts,
        "columns": schemas,
    }


def _json_safe(value: Any, *, max_cell_bytes: int) -> Any:
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _bounded_utf8_text(value, max_cell_bytes)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return _bounded_utf8_text(str(value), max_cell_bytes)


def _bounded_utf8_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = "...[truncated]"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return prefix + marker


def _read_bounded_http_body(response: Any, *, max_bytes: int, label: str) -> bytes:
    headers = getattr(response, "headers", None)
    if headers is not None:
        raw_length = headers.get("Content-Length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except (TypeError, ValueError):
                # Content-Length is provider-controlled. Ignore malformed
                # values and enforce the bound against bytes actually read;
                # never propagate the parser's diagnostic because it embeds
                # the untrusted header verbatim.
                declared_length = None
            if declared_length is not None and declared_length > max_bytes:
                raise ValueError(f"{label} exceeds its {max_bytes}-byte limit.")
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"{label} exceeds its {max_bytes}-byte limit.")
    return payload


def _numeric_retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
        parsed = float(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


def _capability_callable(context: Mapping[str, Any], capability_id: str) -> str:
    """Return the ``module:function`` a declared capability binds to."""

    for entry in context.get("capabilities", []) or []:
        if isinstance(entry, Mapping) and entry.get("id") == capability_id:
            spec = str(entry.get("callable") or "").strip()
            if spec.count(":") == 1 and all(part for part in spec.split(":")):
                return spec
            raise ValueError(
                f"Capability {capability_id!r} does not declare a usable "
                "module:function callable."
            )
    raise ValueError(
        f"The environment does not declare the {capability_id!r} capability "
        "this method requires."
    )


def _find_callable_import_root(callable_spec: str) -> Path:
    """Locate the capability module on the retained method import path."""

    module_name = callable_spec.split(":", 1)[0]
    module_file = module_name.replace(".", "/") + ".py"
    for raw_root in sys.path:
        try:
            root = Path(raw_root or os.getcwd()).resolve(strict=True)
            candidate = root / module_file
            metadata = candidate.lstat()
        except (OSError, RuntimeError):
            continue
        if candidate.is_symlink() or not candidate.is_file() or metadata.st_size <= 0:
            continue
        return root
    raise RuntimeError(
        f"The capability module {module_name!r} is not available on the "
        "retained method import path."
    )


def _sanitized_replay_environment(api_key_env: str) -> Dict[str, str]:
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SYSTEMROOT",
        "WINDIR",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    for key in list(environment):
        folded = key.upper()
        if any(
            token in folded
            for token in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH", "COOKIE")
        ):
            environment.pop(key, None)
    environment.pop(api_key_env, None)
    environment.pop("OPENROUTER_API_KEY", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    return environment


def _prepare_runtime_directory(value: Any, label: str) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        raise ValueError(f"runtime_context.{label} must be an absolute path.")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"runtime_context.{label} must be a real directory.")
    return path.resolve(strict=True)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _json_round_trip(value: Any) -> Any:
    return json.loads(_canonical_json_bytes(value).decode("utf-8"))


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(handle.name, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _write_bounded_json(path: Path, payload: Any, *, max_bytes: int) -> None:
    encoded = _canonical_json_bytes(payload)
    if len(encoded) > max_bytes:
        raise ValueError(f"JSON payload exceeds its {max_bytes}-byte limit.")
    _atomic_write_bytes(path, encoded)


def _read_bounded_json_file(
    path: Path, *, max_bytes: int, label: str
) -> Dict[str, Any]:
    with path.open("rb") as handle:
        encoded = handle.read(max_bytes + 1)
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds its {max_bytes}-byte limit.")
    payload = json.loads(encoded.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object.")
    return payload


def _portable_summary(value: Any, *, max_characters: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    # Retained candidate metadata rejects physical paths and URLs.  Summaries
    # are descriptive only, so restrict them to a portable display alphabet.
    text = re.sub(r"[^A-Za-z0-9 .,_+@()\-]", "_", text)
    return text[:max_characters] or "Policy edit"


def _read_local_prompt(name: str, fallback: str) -> str:
    path = Path(__file__).with_name("prompts") / name
    return path.read_text(encoding="utf-8") if path.is_file() else fallback


def _bounded_int(value: Any, minimum: int, maximum: int, name: str) -> int:
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}].")
    return parsed


def _bounded_float(value: Any, minimum: float, maximum: float, name: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be finite and in [{minimum}, {maximum}].")
    return parsed
