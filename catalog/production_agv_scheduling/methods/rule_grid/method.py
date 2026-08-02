"""Deterministic exhaustive search over the paper's three rule layers."""

from __future__ import annotations

import fnmatch
import itertools
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from optpilot.candidate_staging import CandidateBundleStager, CandidateFileMapping


JsonDict = Dict[str, Any]
RuleTriple = Tuple[str, str, str]

LINE_RULES: Tuple[str, ...] = ("default", "sq", "lwt", "met", "random")
TASK_RULES: Tuple[str, ...] = (
    "default",
    "spt",
    "lwkr",
    "lopnr",
    "edd",
    "cr",
    "ms",
    "fifo",
    "random",
)
AGV_RULES: Tuple[str, ...] = ("default", "nvf", "random")

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_POLICY_INIT_TEMPLATE = _TEMPLATE_DIR / "policy_init.py"
_POLICY_TEMPLATE = _TEMPLATE_DIR / "rule_scheduler.py"
_ESTIMATOR_TEMPLATE = _TEMPLATE_DIR / "param_estimator.py"
_CANDIDATE_PATHS: Tuple[str, ...] = (
    "scheduler.py",
    "param_estimator.py",
    "policy/__init__.py",
    "policy/rule_scheduler.py",
)


class RuleGridMethod:
    """Stage one executable policy for each configured rule triple.

    The full configuration enumerates ``5 * 9 * 3 == 135`` candidates in a
    stable line-major, task-middle, AGV-minor order.  Narrower configurations,
    such as ``method_smoke.yaml``, use the same implementation.
    """

    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.study_spec = study_spec
        self.rng = rng
        self.settings = dict(definition.get("config", definition.get("settings", {})) or {})
        self._grid = self._build_grid()
        self._validate_environment_contract()
        self._cursor = 0
        self.observations: List[JsonDict] = []

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if n_candidates <= 0 or self._cursor >= len(self._grid):
            return []

        staging_dir = _candidate_staging_dir(study_state)
        stager = CandidateBundleStager(staging_dir)
        start = self._cursor
        triples = self._grid[start : start + n_candidates]
        candidates = [
            self._stage_candidate(stager, start + offset, triple)
            for offset, triple in enumerate(triples)
        ]
        self._cursor += len(candidates)
        return candidates

    def observe(self, observations: List[JsonDict]) -> None:
        self.observations.extend(dict(observation) for observation in observations)

    def _build_grid(self) -> List[RuleTriple]:
        line_rules = _configured_rules(self.settings, "lineRules", LINE_RULES)
        task_rules = _configured_rules(self.settings, "taskRules", TASK_RULES)
        agv_rules = _configured_rules(self.settings, "agvRules", AGV_RULES)
        return list(itertools.product(line_rules, task_rules, agv_rules))

    def _stage_candidate(
        self,
        stager: CandidateBundleStager,
        candidate_index: int,
        triple: RuleTriple,
    ) -> JsonDict:
        line_rule, task_rule, agv_rule = triple
        candidate_id = f"rule-grid-{line_rule}-{task_rule}-{agv_rule}"
        scheduler_source = _render_scheduler(line_rule, task_rule, agv_rule)

        with tempfile.TemporaryDirectory(prefix="optpilot-rule-grid-") as temp_dir:
            scheduler_path = Path(temp_dir) / "scheduler.py"
            scheduler_path.write_text(scheduler_source, encoding="utf-8")
            return stager.stage_files(
                [
                    CandidateFileMapping(scheduler_path, "scheduler.py"),
                    CandidateFileMapping(_ESTIMATOR_TEMPLATE, "param_estimator.py"),
                    CandidateFileMapping(_POLICY_INIT_TEMPLATE, "policy/__init__.py"),
                    CandidateFileMapping(_POLICY_TEMPLATE, "policy/rule_scheduler.py"),
                ],
                candidate_id=candidate_id,
                lineage={"parents": []},
                generator={
                    "method_id": self.definition["id"],
                    "strategy": "exhaustive_rule_grid",
                    "candidate_index": candidate_index,
                    "rule_triple": {
                        "line": line_rule,
                        "task": task_rule,
                        "agv": agv_rule,
                    },
                    "is_initial_policy": triple == ("default", "default", "default"),
                },
            )

    def _validate_environment_contract(self) -> None:
        allow = _candidate_file_allow_patterns(self.study_spec)
        if not allow:
            return
        rejected = [
            path
            for path in _CANDIDATE_PATHS
            if not any(_path_matches(path, pattern) for pattern in allow)
        ]
        if rejected:
            raise ValueError(
                "The selected environment does not allow all rule-grid candidate files. "
                f"Rejected paths: {rejected}; allow patterns: {allow}."
            )


def _configured_rules(
    settings: JsonDict,
    setting_name: str,
    supported: Sequence[str],
) -> Tuple[str, ...]:
    raw = settings.get(setting_name, list(supported))
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        raise TypeError(f"{setting_name} must be a list of rule names.")
    values = tuple(str(value).strip().lower() for value in raw)
    if not values:
        raise ValueError(f"{setting_name} must contain at least one rule.")
    if len(values) != len(set(values)):
        raise ValueError(f"{setting_name} must not contain duplicate rules.")
    unsupported = [value for value in values if value not in supported]
    if unsupported:
        raise ValueError(
            f"Unsupported {setting_name}: {unsupported!r}; expected values from {list(supported)!r}."
        )
    return values


def _candidate_staging_dir(study_state: JsonDict) -> str:
    runtime_context = dict(study_state.get("runtime_context", {}) or {})
    value = runtime_context.get("candidate_staging_dir")
    if not value:
        raise ValueError("RuleGridMethod requires runtime_context.candidate_staging_dir.")
    return str(value)


def _candidate_file_allow_patterns(study_spec: Any) -> List[str]:
    candidate = getattr(study_spec, "candidate", {}) or {}
    context = dict(candidate.get("context", {}) or {})
    files = context.get("files")
    if not isinstance(files, Mapping):
        nested_candidate = context.get("candidate", {})
        files = nested_candidate.get("files") if isinstance(nested_candidate, Mapping) else None
    if not isinstance(files, Mapping):
        return []
    return [str(item) for item in files.get("allow", []) or []]


def _path_matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
        return True
    return fnmatch.fnmatchcase(path, pattern)


def _render_scheduler(line_rule: str, task_rule: str, agv_rule: str) -> str:
    return f'''"""Frozen rule-grid candidate: {line_rule}/{task_rule}/{agv_rule}."""

from policy.rule_scheduler import Scheduler

LINE_RULE = {line_rule!r}
TASK_RULE = {task_rule!r}
AGV_RULE = {agv_rule!r}


def create_scheduler():
    return Scheduler(
        line_selection_method=LINE_RULE,
        task_priority_method=TASK_RULE,
        agv_dispatch_method=AGV_RULE,
    )
'''


__all__ = [
    "AGV_RULES",
    "LINE_RULES",
    "RuleGridMethod",
    "TASK_RULES",
]
