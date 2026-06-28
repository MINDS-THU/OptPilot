"""Study runner for OptPilot."""

from __future__ import annotations

import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .candidate_materialization import normalize_candidate
from .environment import build_environment_snapshot
from .evidence import EvidenceView
from .execution import Evaluator
from .method_runtime import MethodRuntime
from .models import RunSummary, SandboxSpec, TrialSpec, ResourceProfile, utc_now_iso
from .registry import resolve_component
from .run_sources import prepare_run_sources
from .spec import StudySpec, load_study_spec
from .spec import load_expanded_study_spec
from .storage import LocalEvidenceStore


class StudyRunner:
    def __init__(
        self,
        study_spec: StudySpec,
        output_root: Optional[Path] = None,
        resume_run_dir: Optional[Path] = None,
        branch_from_run_dir: Optional[Path] = None,
    ):
        self.study_spec = study_spec
        evidence_output_dir = study_spec.evidence.get("outputDir")
        self.output_root = output_root or (Path(evidence_output_dir).resolve() if evidence_output_dir else (Path.cwd() / "runs").resolve())
        _reject_catalog_run_root(self.output_root, "run output root")
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.resume_run_dir = Path(resume_run_dir).resolve() if resume_run_dir else None
        self.branch_from_run_dir = Path(branch_from_run_dir).resolve() if branch_from_run_dir else None
        if self.resume_run_dir and self.branch_from_run_dir:
            raise ValueError("Use either resume_run_dir or branch_from_run_dir, not both.")

    def run(self) -> RunSummary:
        seed = int(self.study_spec.reproducibility.get("seedPolicy", {}).get("globalSeed", 0))
        rng = random.Random(seed)
        store = self._open_store()
        self.study_spec = prepare_run_sources(self.study_spec, store.run_dir)
        previous_summary = store.read_summary() if self.resume_run_dir else {}
        study_id = previous_summary.get("study_id") or f"study-{uuid.uuid4().hex[:12]}"
        store.write_spec(self.study_spec.raw)
        run_policy = _build_run_policy(self.study_spec)
        store.write_run_policy(run_policy)
        environment_snapshot = build_environment_snapshot(
            study_spec_path=self.study_spec.path,
            run_dir=store.run_dir,
            dependency_roots=_dependency_roots_for_snapshot(self.study_spec),
        )
        store.write_environment_snapshot(environment_snapshot)
        store.write_run_lineage(
            _build_run_lineage(
                store,
                mode="resume" if self.resume_run_dir else "branch" if self.branch_from_run_dir else "new",
                study_id=study_id,
                resume_run_dir=self.resume_run_dir,
                branch_from_run_dir=self.branch_from_run_dir,
            )
        )
        evidence_view = EvidenceView(store, self.study_spec)
        runtime_context = {
            "run_dir": str(store.run_dir),
            "candidate_store": str(store.run_dir / "candidates"),
            "candidate_store_dir": str(store.run_dir / "candidates"),
            "prompt_store_dir": str(store.run_dir / "prompts"),
            "candidate_content_ref_mode": "absolute",
            "prompt_content_ref_mode": "absolute",
            "candidate_context": dict(self.study_spec.candidate.get("context", {})),
            "environment_interfaces": list(
                self.study_spec.environment.get("adapter", {}).get("config", {}).get("interfaces", [])
            ),
        }

        _prepend_python_paths(self.study_spec.environment["adapter"].get("pythonPath", []) or [])
        environment_cls = resolve_component("adapter", self.study_spec.environment["adapter"]["implementation"])
        environment_adapter = environment_cls(self.study_spec.environment["adapter"], self.study_spec)

        materializer_def = _resolve_materialization_spec(self.study_spec)
        materializer_cls = resolve_component("materializer", materializer_def["implementation"])
        materializer = materializer_cls(materializer_def, self.study_spec)

        validator_def = _resolve_validation_spec(self.study_spec)
        validator_cls = resolve_component("validator", validator_def["implementation"])
        validator = validator_cls(validator_def, self.study_spec)

        method_def = self.study_spec.method
        method_impl = method_def.get("implementation", {})
        method_instance = None
        method_runtime = MethodRuntime(method_def, method_instance, store, self.study_spec)

        backend_def = self.study_spec.execution["backend"]
        evaluator = Evaluator(self.study_spec, environment_adapter, store, materializer, validator)
        backend_cls = resolve_component("backend", backend_def["implementation"])
        backend = backend_cls(backend_def, evaluator, max_workers=self.study_spec.candidate_parallelism)
        scheduler_def = _resolve_scheduler(self.study_spec)
        scheduler_cls = resolve_component("scheduler", scheduler_def["implementation"])
        scheduler = scheduler_cls(scheduler_def, backend, store)

        prior = _prior_run_state(evidence_view, previous_summary)
        best_metric: Optional[float] = prior["best_metric"]
        best_trial_id: Optional[str] = prior["best_trial_id"]
        best_candidate_id: Optional[str] = prior["best_candidate_id"]
        completed_trials = prior["completed_trials"]
        started_at = previous_summary.get("started_at") or utc_now_iso()
        start_monotonic = time.monotonic()
        max_trials = int(self.study_spec.stopping.get("maxTrials", 0) or 0)
        max_wall_clock = int(self.study_spec.stopping.get("maxWallClockSeconds", 0) or 0)
        patience_cfg = self.study_spec.stopping.get("convergenceRule", {}).get("config", {})
        patience_trials = int(patience_cfg.get("patienceTrials", 0) or 0)
        min_delta = float(patience_cfg.get("minDelta", 0.0) or 0.0)
        no_improvement_count = 0
        failure_count = int(prior["failure_count"])
        max_failures = int(self.study_spec.stopping.get("maxFailures", 0) or 0)

        try:
            while True:
                if max_trials and completed_trials >= max_trials:
                    break
                if max_failures and failure_count >= max_failures:
                    break
                elapsed = time.monotonic() - start_monotonic
                if max_wall_clock and elapsed >= max_wall_clock:
                    break

                study_state = {
                    "completed_trials": completed_trials,
                    "failure_count": failure_count,
                    "best_metric": best_metric,
                    "best_trial_id": best_trial_id,
                    "candidate_context": runtime_context["candidate_context"],
                    "runtime_context": runtime_context,
                }
                configured_batch_size = int(method_def.get("config", {}).get("batchSize", 1) or 1)
                remaining = max_trials - completed_trials if max_trials else configured_batch_size
                batch_size = min(configured_batch_size, remaining) if max_trials else configured_batch_size
                proposed_candidates = method_runtime.propose(batch_size, study_state, evidence_view)
                if not proposed_candidates:
                    break
                trial_specs = []
                for candidate in proposed_candidates:
                    normalized_candidate = normalize_candidate(candidate, self.study_spec, method_def["id"])
                    trial_spec = TrialSpec(
                        trial_id=f"trial-{uuid.uuid4().hex[:12]}",
                        study_id=study_id,
                        method_id=method_def["id"],
                        candidate=normalized_candidate,
                        objective=self.study_spec.objective,
                        resource_profile=_resolve_resource_profile(self.study_spec.execution, method_def),
                        sandbox_spec=_resolve_sandbox_spec(self.study_spec.execution, method_def),
                        metadata={
                            "seed": seed,
                            "backend_identity": _backend_identity(backend_def),
                            "scheduler_identity": _scheduler_identity(scheduler_def),
                        },
                    )
                    trial_specs.append(trial_spec)
                batch_observations = scheduler.run_batch(trial_specs)
                method_runtime.observe([observation.to_dict() for observation in batch_observations])
                for observation in batch_observations:
                    if _is_failure_status(observation.status):
                        failure_count += 1
                    if self.study_spec.primary_metric_name not in observation.metric_values:
                        no_improvement_count += 1
                        continue
                    metric = float(observation.metric_values[self.study_spec.primary_metric_name])
                    if _is_better(metric, best_metric, self.study_spec.primary_metric_direction, min_delta):
                        best_metric = metric
                        best_trial_id = observation.trial_id
                        best_candidate_id = observation.candidate_id
                        no_improvement_count = 0
                    else:
                        no_improvement_count += 1
                completed_trials += len(batch_observations)
                if patience_trials and no_improvement_count >= patience_trials:
                    break
        finally:
            method_runtime.close()

        summary = RunSummary(
            study_id=study_id,
            run_dir=str(store.run_dir),
            completed_trials=completed_trials,
            best_trial_id=best_trial_id,
            best_metric=best_metric,
            best_candidate_id=best_candidate_id,
            started_at=started_at,
            finished_at=utc_now_iso(),
            failure_count=failure_count,
            policy=run_policy,
        )
        store.write_summary(summary.to_dict())
        return summary

    def _open_store(self) -> LocalEvidenceStore:
        if self.resume_run_dir:
            if not self.resume_run_dir.exists():
                raise FileNotFoundError(f"Cannot resume missing run directory: {self.resume_run_dir}")
            return LocalEvidenceStore.open_run_dir(self.resume_run_dir)
        return LocalEvidenceStore(self.output_root, self.study_spec.name)



def run_study(
    spec_path: str,
    output_root: Optional[str] = None,
    resume_run_dir: Optional[str] = None,
    branch_from_run_dir: Optional[str] = None,
) -> RunSummary:
    study_spec = load_study_spec(spec_path)
    runner = StudyRunner(
        study_spec,
        output_root=Path(output_root).resolve() if output_root else None,
        resume_run_dir=Path(resume_run_dir).resolve() if resume_run_dir else None,
        branch_from_run_dir=Path(branch_from_run_dir).resolve() if branch_from_run_dir else None,
    )
    return runner.run()


def _prepend_python_paths(paths: List[str]) -> None:
    import sys

    for path in reversed([str(Path(item).resolve()) for item in paths if item]):
        if path not in sys.path:
            sys.path.insert(0, path)


def _reject_catalog_run_root(path: Path, location: str) -> None:
    if any(part == "catalog" for part in path.resolve().parts):
        raise ValueError(f"{location} must not be inside catalog source: {path}")


def _dependency_roots_for_snapshot(study_spec: StudySpec) -> List[Path]:
    roots = [Path.cwd().resolve(), study_spec.path.parent.resolve()]
    run_source = study_spec.raw.get("extensions", {}).get("runSource", {})
    if isinstance(run_source, dict):
        for item in run_source.values():
            if isinstance(item, dict) and item.get("copiedSourceRoot"):
                roots.append(Path(str(item["copiedSourceRoot"])).resolve())
    deduped: List[Path] = []
    seen = set()
    for root in roots:
        if root not in seen:
            seen.add(root)
            deduped.append(root)
    return deduped


def run_expanded_study_spec(
    spec_path: str,
    output_root: Optional[str] = None,
    resume_run_dir: Optional[str] = None,
    branch_from_run_dir: Optional[str] = None,
) -> RunSummary:
    """Run an already-expanded internal StudySpec.

    Public users should call `run_study` with a public study config. This helper exists
    for internal tests and worker-style paths that deliberately exercise the
    canonical execution representation.
    """

    study_spec = load_expanded_study_spec(spec_path)
    runner = StudyRunner(
        study_spec,
        output_root=Path(output_root).resolve() if output_root else None,
        resume_run_dir=Path(resume_run_dir).resolve() if resume_run_dir else None,
        branch_from_run_dir=Path(branch_from_run_dir).resolve() if branch_from_run_dir else None,
    )
    return runner.run()


def _resolve_resource_profile(execution: Dict[str, Any], method: Dict[str, Any]) -> ResourceProfile:
    defaults = execution.get("defaults", {}).get("resourceProfile", {})
    merged = dict(defaults)
    merged.update(method.get("resourceProfile", {}))
    return ResourceProfile.from_dict(merged)



def _resolve_sandbox_spec(execution: Dict[str, Any], method: Dict[str, Any]) -> SandboxSpec:
    defaults = execution.get("defaults", {}).get("sandboxSpec", {})
    merged = dict(defaults)
    merged.update(method.get("sandboxSpec", {}))
    return SandboxSpec.from_dict(merged)


def _resolve_materialization_spec(study_spec: StudySpec) -> Dict[str, Any]:
    return dict(
        study_spec.candidate.get(
            "materialization",
            {
                "implementation": "builtin.parameter_to_config",
                "config": {},
            },
        )
    )


def _resolve_validation_spec(study_spec: StudySpec) -> Dict[str, Any]:
    return dict(
        study_spec.candidate.get(
            "validation",
            {
                "implementation": "builtin.schema_validation",
                "config": {"enforceBounds": False},
            },
        )
    )


def _backend_identity(backend_def: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": backend_def.get("type"),
        "implementation": backend_def.get("implementation"),
        "config": dict(backend_def.get("config", {})),
    }


def _resolve_scheduler(study_spec: StudySpec) -> Dict[str, Any]:
    return dict(
        study_spec.execution.get(
            "scheduler",
            {
                "type": "local",
                "implementation": "builtin.local_scheduler",
                "config": {},
            },
        )
    )


def _scheduler_identity(scheduler_def: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": scheduler_def.get("type", "local"),
        "implementation": scheduler_def.get("implementation", "builtin.local_scheduler"),
        "config": dict(scheduler_def.get("config", {})),
    }


def _build_run_policy(study_spec: StudySpec) -> Dict[str, Any]:
    scheduler_def = _resolve_scheduler(study_spec)
    runtime_contract = dict(study_spec.environment.get("runtimeContract", {}))
    parallelism = dict(study_spec.execution.get("parallelism", {}))
    return {
        "environment": {
            "candidateAccess": _public_candidate_access(study_spec.environment.get("accessPolicy")),
            "candidateWriteScope": _public_candidate_write_scope(study_spec.environment.get("mutationPolicy")),
            "timeoutSeconds": runtime_contract.get("timeoutSeconds"),
        },
        "execution": {
            "backend": _backend_identity(study_spec.execution.get("backend", {})),
            "scheduler": _scheduler_identity(scheduler_def),
            "parallelism": {
                "candidateEvaluations": parallelism.get("candidateParallelism", 1),
                "methodCalls": parallelism.get("methodParallelism", 1),
            },
            "defaults": dict(study_spec.execution.get("defaults", {})),
        },
        "evidence": dict(study_spec.evidence),
        "reproducibility": dict(study_spec.reproducibility),
    }


def _public_candidate_access(access_policy: Any) -> str:
    return {
        "SchemaAware": "candidate_schema",
        "CodeAwareReadOnly": "candidate_files_read_only",
        "InvocationOnly": "evaluator_invocation_only",
    }.get(str(access_policy), str(access_policy))


def _public_candidate_write_scope(mutation_policy: Any) -> str:
    return {
        "NoMutation": "none",
        "TrialWorkspaceOnly": "trial_workspace_only",
    }.get(str(mutation_policy), str(mutation_policy))


def _prior_run_state(evidence_view: EvidenceView, previous_summary: Dict[str, Any]) -> Dict[str, Any]:
    summary = evidence_view.summary()
    completed_trials = int(previous_summary.get("completed_trials", summary.observation_count) or 0)
    failure_count = int(
        previous_summary.get(
            "failure_count",
            _failure_count_from_status_counts(summary.status_counts),
        )
        or 0
    )
    return {
        "completed_trials": completed_trials,
        "failure_count": failure_count,
        "best_metric": previous_summary.get("best_metric", summary.best_metric),
        "best_trial_id": previous_summary.get("best_trial_id", summary.best_trial_id),
        "best_candidate_id": previous_summary.get("best_candidate_id", summary.best_candidate_id),
    }


def _build_run_lineage(
    store: LocalEvidenceStore,
    *,
    mode: str,
    study_id: str,
    resume_run_dir: Optional[Path],
    branch_from_run_dir: Optional[Path],
) -> Dict[str, Any]:
    existing = store.read_run_lineage()
    lineage = dict(existing) if existing else {}
    lineage.setdefault("created_at", utc_now_iso())
    lineage["updated_at"] = utc_now_iso()
    lineage["study_id"] = study_id
    lineage["run_dir"] = str(store.run_dir)
    lineage["mode"] = mode
    lineage.setdefault("resume_events", [])
    if mode == "resume":
        lineage["resume_events"].append(
            {
                "resumed_at": utc_now_iso(),
                "run_dir": str(resume_run_dir),
            }
        )
    if mode == "branch":
        lineage["parent"] = {
            "run_dir": str(branch_from_run_dir),
            "branched_at": utc_now_iso(),
        }
    return lineage



def _is_better(candidate: float, current: Optional[float], direction: str, min_delta: float) -> bool:
    if current is None:
        return True
    if direction == "maximize":
        return candidate > current + min_delta
    if direction == "minimize":
        return candidate < current - min_delta
    raise ValueError(f"Unsupported optimization direction: {direction}")


FAILURE_STATUSES = {"failed", "invalid", "timeout", "partial"}


def _is_failure_status(status: str) -> bool:
    return status in FAILURE_STATUSES


def _failure_count_from_status_counts(status_counts: Dict[str, Any]) -> int:
    return sum(int(status_counts.get(status, 0) or 0) for status in FAILURE_STATUSES)
