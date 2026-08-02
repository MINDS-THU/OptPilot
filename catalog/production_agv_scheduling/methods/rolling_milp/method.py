"""Stage executable rolling-MILP policies as immutable file candidates.

The method does not import Gurobi or the simulator.  It deterministically emits
the two policy variants described in the paper; the environment imports and
executes the selected candidate inside a simulation trial.
"""

from __future__ import annotations

import fnmatch
import pprint
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from optpilot.candidate_staging import CandidateBundleStager, CandidateFileMapping


JsonDict = Dict[str, Any]
_SOURCE_ROOT = Path(__file__).resolve().parent / "candidate_source"
_GENERATED_SETTINGS_PATH = "policy/settings.py"
_SUPPORTED_VARIANTS = ("monolithic", "two_stage")


class RollingMILPMethod:
    """Emit each requested rolling-MILP variant exactly once."""

    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.study_spec = study_spec
        self.settings = dict(definition.get("config", {}))
        self.variants = _normalize_variants(self.settings.get("variants", _SUPPORTED_VARIANTS))
        self._source_files = _candidate_source_files()
        self._candidate_paths = sorted([*self._source_files, _GENERATED_SETTINGS_PATH])
        self._validate_environment_contract()
        self._index = 0

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if n_candidates <= 0 or self._index >= len(self.variants):
            return []

        runtime_context = dict(study_state.get("runtime_context", {}))
        staging_dir = runtime_context.get("candidate_staging_dir")
        if not staging_dir:
            raise ValueError(
                "RollingMILPMethod requires runtime_context.candidate_staging_dir "
                "to stage file candidates."
            )

        stager = CandidateBundleStager(staging_dir)
        selected = self.variants[self._index : self._index + int(n_candidates)]
        candidates: List[JsonDict] = []
        for variant in selected:
            candidates.append(self._stage_variant(stager, variant))
            self._index += 1
        return candidates

    def observe(self, observations: List[JsonDict]) -> None:
        """The baseline enumeration is deterministic and does not adapt."""

        return None

    def _stage_variant(self, stager: CandidateBundleStager, variant: str) -> JsonDict:
        policy_settings = _policy_settings(self.settings, variant)
        with tempfile.TemporaryDirectory(prefix="rolling-milp-settings-") as temp_dir:
            generated_settings = Path(temp_dir) / "settings.py"
            generated_settings.write_text(
                "# Generated deterministically by RollingMILPMethod.\n"
                "SETTINGS = "
                + pprint.pformat(policy_settings, sort_dicts=True, width=100)
                + "\n",
                encoding="utf-8",
            )
            mappings = [
                CandidateFileMapping(source=source, path=path)
                for path, source in sorted(self._source_files.items())
            ]
            mappings.append(
                CandidateFileMapping(source=generated_settings, path=_GENERATED_SETTINGS_PATH)
            )
            return stager.stage_files(
                mappings,
                candidate_id=f"rolling-milp-{variant.replace('_', '-')}",
                lineage={"parents": [], "source": "paper_baseline_extraction"},
                generator={
                    "method_id": self.definition["id"],
                    "strategy": "deterministic_rolling_milp_baseline",
                    "variant": variant,
                    "solver": "gurobi",
                    "solver_time_limit_seconds": policy_settings["solver_time_limit_sec"],
                    "min_replan_interval_minutes": policy_settings["min_replan_interval_sec"],
                    "accept_partial_solution": policy_settings["accept_partial_mip_solution"],
                    "fallback_mode": policy_settings["fallback_mode"],
                    "adaptive_task_cap": policy_settings["adaptive_task_cap"],
                    "summary": (
                        "Two-stage raw-line selection plus per-line MILP sequencing."
                        if variant == "two_stage"
                        else "Original monolithic rolling-horizon MILP."
                    ),
                },
            )

    def _validate_environment_contract(self) -> None:
        candidate = getattr(self.study_spec, "candidate", {}) or {}
        context = dict(candidate.get("context", {}) or {})
        files = context.get("files")
        if not isinstance(files, Mapping):
            nested_candidate = context.get("candidate", {})
            files = nested_candidate.get("files") if isinstance(nested_candidate, Mapping) else None
        if not isinstance(files, Mapping):
            return
        allow = [str(item) for item in files.get("allow", []) or []]
        if not allow:
            return
        rejected = [
            path
            for path in self._candidate_paths
            if not any(_path_matches(path, pattern) for pattern in allow)
        ]
        if rejected:
            raise ValueError(
                "The selected environment does not allow all rolling-MILP candidate files. "
                f"Rejected paths: {rejected}; allow patterns: {allow}."
            )


def _candidate_source_files() -> Dict[str, Path]:
    if not _SOURCE_ROOT.is_dir():
        raise FileNotFoundError(f"Rolling-MILP candidate source is missing: {_SOURCE_ROOT}")
    files = {
        path.relative_to(_SOURCE_ROOT).as_posix(): path
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
        if path.name != "settings.py" and "__pycache__" not in path.parts
    }
    required = {"scheduler.py", "param_estimator.py", "policy/controller.py"}
    missing = sorted(required.difference(files))
    if missing:
        raise FileNotFoundError(f"Rolling-MILP candidate source is incomplete: {missing}")
    return files


def _normalize_variants(raw: Any) -> List[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Iterable):
        raise TypeError("settings.variants must be a variant name or a list of variant names.")
    variants: List[str] = []
    for item in raw:
        variant = str(item).strip().lower().replace("-", "_")
        if variant not in _SUPPORTED_VARIANTS:
            raise ValueError(
                f"Unsupported rolling-MILP variant {item!r}; expected one of {_SUPPORTED_VARIANTS}."
            )
        if variant not in variants:
            variants.append(variant)
    if not variants:
        raise ValueError("settings.variants must contain at least one variant.")
    return variants


def _policy_settings(config: Mapping[str, Any], variant: str) -> JsonDict:
    fallback_mode = str(config.get("fallbackMode", "heuristic")).strip().lower()
    if fallback_mode not in {"error", "heuristic"}:
        raise ValueError("settings.fallbackMode must be 'error' or 'heuristic'.")

    settings: JsonDict = {
        "variant": variant,
        "solver_time_limit_sec": _positive_float(config, "solverTimeLimitSeconds", 2.0),
        "step_sec": _positive_float(config, "simulationStepMinutes", 0.5),
        "realtime_sleep_sec": 0.0,
        # The extracted implementation calls this a number of seconds, but the
        # paper and cleaned environment define one simulation unit as a minute.
        "min_replan_interval_sec": _nonnegative_float(
            config, "minReplanIntervalMinutes", 8.0
        ),
        "max_raw_products": int(config.get("maxRawProducts", 24)),
        "max_future_tasks": int(config.get("maxFutureTasks", 24)),
        "future_horizon_sec": float(config.get("futureHorizonMinutes", 120.0)),
        "max_mip_tasks": int(config.get("maxMipTasks", 120)),
        "accept_partial_mip_solution": bool(config.get("acceptPartialSolution", False)),
        "fallback_mode": fallback_mode,
        "use_two_stage_decomposition": variant == "two_stage",
        # The enhanced two-stage implementation introduced the adaptive cap;
        # the original monolithic comparison keeps the static upstream cap.
        "adaptive_task_cap": variant == "two_stage",
        "adaptive_min_mip_tasks": int(config.get("adaptiveMinMipTasks", 48)),
        "adaptive_max_mip_tasks": int(config.get("adaptiveMaxMipTasks", 200)),
    }
    overrides = config.get("variantOverrides", {})
    if isinstance(overrides, Mapping) and isinstance(overrides.get(variant), Mapping):
        for key, value in overrides[variant].items():
            if key not in settings:
                raise ValueError(f"Unknown variant override {variant}.{key}.")
            if key in {"variant", "use_two_stage_decomposition"}:
                raise ValueError(f"variantOverrides may not change baseline identity field {key!r}.")
            settings[key] = value
    if str(settings["fallback_mode"]).strip().lower() not in {"error", "heuristic"}:
        raise ValueError("The effective fallback_mode must be 'error' or 'heuristic'.")
    settings["fallback_mode"] = str(settings["fallback_mode"]).strip().lower()
    if float(settings["solver_time_limit_sec"]) <= 0.0:
        raise ValueError("The effective solver_time_limit_sec must be positive.")
    if float(settings["step_sec"]) <= 0.0:
        raise ValueError("The effective step_sec must be positive.")
    if float(settings["min_replan_interval_sec"]) < 0.0:
        raise ValueError("The effective min_replan_interval_sec must be nonnegative.")
    return settings


def _positive_float(config: Mapping[str, Any], key: str, default: float) -> float:
    value = float(config.get(key, default))
    if value <= 0.0:
        raise ValueError(f"settings.{key} must be positive.")
    return value


def _nonnegative_float(config: Mapping[str, Any], key: str, default: float) -> float:
    value = float(config.get(key, default))
    if value < 0.0:
        raise ValueError(f"settings.{key} must be nonnegative.")
    return value


def _path_matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
        return True
    return fnmatch.fnmatchcase(path, pattern)


__all__ = ["RollingMILPMethod"]
