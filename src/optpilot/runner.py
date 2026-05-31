"""Study runner for OptPilot."""

from __future__ import annotations

import random
import inspect
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifacts import normalize_optimizable_artifact
from .engine_runtime import EngineRuntime
from .environment import build_environment_snapshot
from .evidence import EvidenceView
from .execution import Evaluator
from .models import RunSummary, SandboxSpec, TrialSpec, ResourceProfile, utc_now_iso
from .registry import resolve_component
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
        self.output_root = output_root or (Path(evidence_output_dir).resolve() if evidence_output_dir else study_spec.base_dir.parent / "runs")
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.resume_run_dir = Path(resume_run_dir).resolve() if resume_run_dir else None
        self.branch_from_run_dir = Path(branch_from_run_dir).resolve() if branch_from_run_dir else None
        if self.resume_run_dir and self.branch_from_run_dir:
            raise ValueError("Use either resume_run_dir or branch_from_run_dir, not both.")

    def run(self) -> RunSummary:
        seed = int(self.study_spec.reproducibility.get("seedPolicy", {}).get("globalSeed", 0))
        rng = random.Random(seed)
        store = self._open_store()
        previous_summary = store.read_summary() if self.resume_run_dir else {}
        study_id = previous_summary.get("study_id") or f"study-{uuid.uuid4().hex[:12]}"
        store.write_spec(self.study_spec.raw)
        run_policy = _build_run_policy(self.study_spec)
        store.write_run_policy(run_policy)
        environment_snapshot = build_environment_snapshot(study_spec_path=self.study_spec.path, run_dir=store.run_dir)
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
            "artifact_store_dir": str(store.run_dir / "artifacts"),
            "prompt_store_dir": str(store.run_dir / "prompts"),
            "artifact_content_ref_mode": "absolute",
            "prompt_content_ref_mode": "absolute",
        }

        controller_def = self.study_spec.controllers[0]
        controller_cls = resolve_component("controller", controller_def["implementation"])
        controller = controller_cls(controller_def, self.study_spec)

        target_cls = resolve_component("adapter", self.study_spec.target["adapter"]["implementation"])
        target_adapter = target_cls(self.study_spec.target["adapter"], self.study_spec)

        materializer_def = _resolve_materialization_plan(self.study_spec)
        materializer_cls = resolve_component("materializer", materializer_def["implementation"])
        materializer = materializer_cls(materializer_def, self.study_spec)

        validator_def = _resolve_validation_rules(self.study_spec)
        validator_cls = resolve_component("validator", validator_def["implementation"])
        validator = validator_cls(validator_def, self.study_spec)

        engine_instances = {}
        for engine_def in self.study_spec.engines:
            engine_cls = resolve_component("engine", engine_def["implementation"])
            engine = engine_cls(engine_def, self.study_spec, rng)
            engine_instances[engine_def["id"]] = EngineRuntime(engine_def, engine, store, self.study_spec)

        backend_def = self.study_spec.execution["backend"]
        evaluator = Evaluator(self.study_spec, target_adapter, store, materializer, validator)
        backend_cls = resolve_component("backend", backend_def["implementation"])
        backend = backend_cls(backend_def, evaluator, max_workers=self.study_spec.candidate_parallelism)
        scheduler_def = _resolve_scheduler(self.study_spec)
        scheduler_cls = resolve_component("scheduler", scheduler_def["implementation"])
        scheduler = scheduler_cls(scheduler_def, backend, store)

        prior = _prior_run_state(evidence_view, previous_summary)
        best_metric: Optional[float] = prior["best_metric"]
        best_trial_id: Optional[str] = prior["best_trial_id"]
        best_artifact_id: Optional[str] = prior["best_artifact_id"]
        completed_trials = prior["completed_trials"]
        started_at = previous_summary.get("started_at") or utc_now_iso()
        start_monotonic = time.monotonic()
        max_trials = int(self.study_spec.stopping.get("maxTrials", 0) or 0)
        max_wall_clock = int(self.study_spec.stopping.get("maxWallClockSeconds", 0) or 0)
        patience_cfg = self.study_spec.stopping.get("convergenceRule", {}).get("config", {})
        patience_trials = int(patience_cfg.get("patienceTrials", 0) or 0)
        min_delta = float(patience_cfg.get("minDelta", 0.0) or 0.0)
        no_improvement_count = 0

        while True:
            if max_trials and completed_trials >= max_trials:
                break
            elapsed = time.monotonic() - start_monotonic
            if max_wall_clock and elapsed >= max_wall_clock:
                break

            study_state = {
                "completed_trials": completed_trials,
                "best_metric": best_metric,
                "best_trial_id": best_trial_id,
                "runtime_context": runtime_context,
            }
            decision_context = evidence_view.decision_context()
            decision = _call_controller_decide(controller, study_state, self.study_spec.engines, evidence_view)
            decision_record = decision.to_dict()
            decision_record.setdefault("metadata", {})
            decision_record["metadata"].setdefault("evidence_context", decision_context)
            store.record_controller_decision(decision_record)
            engine = engine_instances[decision.engine_id]
            remaining = max_trials - completed_trials if max_trials else decision.batch_size
            batch_size = min(decision.batch_size, remaining) if max_trials else decision.batch_size
            candidate_artifacts = engine.propose(batch_size, study_state, evidence_view)
            trial_specs = []
            for artifact in candidate_artifacts:
                normalized_artifact = normalize_optimizable_artifact(artifact, self.study_spec, decision.engine_id)
                trial_spec = TrialSpec(
                    trial_id=f"trial-{uuid.uuid4().hex[:12]}",
                    study_id=study_id,
                    engine_id=decision.engine_id,
                    artifact=normalized_artifact,
                    instances=self.study_spec.build_instance_batch(rng),
                    objective=self.study_spec.objective,
                    resource_profile=_resolve_resource_profile(self.study_spec.execution, self.study_spec.engines, decision.engine_id),
                    sandbox_spec=_resolve_sandbox_spec(self.study_spec.execution, self.study_spec.engines, decision.engine_id),
                    metadata={
                        "seed": seed,
                        "controller_id": controller_def["id"],
                        "backend_identity": _backend_identity(backend_def),
                        "scheduler_identity": _scheduler_identity(scheduler_def),
                    },
                )
                trial_specs.append(trial_spec)
            batch_observations = scheduler.run_batch(trial_specs)
            engine.observe([observation.to_dict() for observation in batch_observations])
            for observation in batch_observations:
                if self.study_spec.primary_metric_name not in observation.metric_values:
                    no_improvement_count += 1
                    continue
                metric = float(observation.metric_values[self.study_spec.primary_metric_name])
                if _is_better(metric, best_metric, self.study_spec.primary_metric_direction, min_delta):
                    best_metric = metric
                    best_trial_id = observation.trial_id
                    best_artifact_id = observation.artifact_id
                    no_improvement_count = 0
                else:
                    no_improvement_count += 1
            completed_trials += len(batch_observations)
            if patience_trials and no_improvement_count >= patience_trials:
                break

        summary = RunSummary(
            study_id=study_id,
            run_dir=str(store.run_dir),
            completed_trials=completed_trials,
            best_trial_id=best_trial_id,
            best_metric=best_metric,
            best_artifact_id=best_artifact_id,
            started_at=started_at,
            finished_at=utc_now_iso(),
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


def run_expanded_study_spec(
    spec_path: str,
    output_root: Optional[str] = None,
    resume_run_dir: Optional[str] = None,
    branch_from_run_dir: Optional[str] = None,
) -> RunSummary:
    """Run an already-expanded internal StudySpec.

    Public users should call `run_study` with a StudyConfig. This helper exists
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


def _call_controller_decide(controller, study_state: Dict[str, Any], engines: List[Dict[str, Any]], evidence_view):
    parameters = inspect.signature(controller.decide).parameters
    if len(parameters) >= 3:
        return controller.decide(study_state, engines, evidence_view)
    return controller.decide(study_state, engines)



def _resolve_resource_profile(execution: Dict[str, Any], engines: List[Dict[str, Any]], engine_id: str) -> ResourceProfile:
    defaults = execution.get("defaults", {}).get("resourceProfile", {})
    engine_def = next(engine for engine in engines if engine["id"] == engine_id)
    merged = dict(defaults)
    merged.update(engine_def.get("resourceProfile", {}))
    return ResourceProfile.from_dict(merged)



def _resolve_sandbox_spec(execution: Dict[str, Any], engines: List[Dict[str, Any]], engine_id: str) -> SandboxSpec:
    defaults = execution.get("defaults", {}).get("sandboxSpec", {})
    engine_def = next(engine for engine in engines if engine["id"] == engine_id)
    merged = dict(defaults)
    merged.update(engine_def.get("sandboxSpec", {}))
    return SandboxSpec.from_dict(merged)


def _resolve_materialization_plan(study_spec: StudySpec) -> Dict[str, Any]:
    return dict(
        study_spec.primary_artifact.get(
            "materializationPlan",
            {
                "implementation": "builtin.parameter_to_config",
                "config": {},
            },
        )
    )


def _resolve_validation_rules(study_spec: StudySpec) -> Dict[str, Any]:
    return dict(
        study_spec.primary_artifact.get(
            "validationRules",
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
    return {
        "target": {
            "accessPolicy": study_spec.target.get("accessPolicy"),
            "mutationPolicy": study_spec.target.get("mutationPolicy"),
            "runtimeContract": dict(study_spec.target.get("runtimeContract", {})),
        },
        "execution": {
            "backend": _backend_identity(study_spec.execution.get("backend", {})),
            "scheduler": _scheduler_identity(scheduler_def),
            "parallelism": dict(study_spec.execution.get("parallelism", {})),
            "defaults": dict(study_spec.execution.get("defaults", {})),
        },
        "evidence": dict(study_spec.evidence),
        "reproducibility": dict(study_spec.reproducibility),
    }


def _prior_run_state(evidence_view: EvidenceView, previous_summary: Dict[str, Any]) -> Dict[str, Any]:
    summary = evidence_view.summary()
    completed_trials = int(previous_summary.get("completed_trials", summary.observation_count) or 0)
    return {
        "completed_trials": completed_trials,
        "best_metric": previous_summary.get("best_metric", summary.best_metric),
        "best_trial_id": previous_summary.get("best_trial_id", summary.best_trial_id),
        "best_artifact_id": previous_summary.get("best_artifact_id", summary.best_artifact_id),
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
