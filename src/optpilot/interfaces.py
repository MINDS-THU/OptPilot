"""Public integration protocols for OptPilot components."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Protocol

from .models import Observation, TrialSpec


JsonDict = Dict[str, Any]


class Controller(Protocol):
    def decide(self, study_state: JsonDict, engines: List[JsonDict], evidence_view: Any = None) -> Any:
        ...


class CandidateProposalEngine(Protocol):
    """Simple synchronous engine shape used by the runner.

    This is enough for reference fixtures and lightweight user engines. More
    general engines should use the lifecycle-oriented ``Engine`` protocol below.
    """

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        ...

    def observe(self, observations: List[JsonDict]) -> None:
        ...


class Engine(Protocol):
    """General lifecycle-oriented engine interface from the V3 platform design."""

    def start(self, engine_input: JsonDict) -> str:
        ...

    def poll(self, handle: str) -> JsonDict:
        ...

    def intervene(self, handle: str, action: JsonDict) -> None:
        ...

    def finalize(self, handle: str) -> Any:
        ...


class TargetAdapter(Protocol):
    def evaluate(self, artifact_spec: JsonDict, instance: JsonDict, context: JsonDict) -> JsonDict:
        ...


class MaterializationPlan(Protocol):
    def materialize(self, artifact: JsonDict, workspace: Path, context: JsonDict) -> Any:
        ...


class ArtifactValidator(Protocol):
    def validate(self, artifact: JsonDict, context: JsonDict) -> Any:
        ...


class Evaluator(Protocol):
    def run_trial(self, trial_spec: TrialSpec) -> List[Observation]:
        ...


class ExecutionBackend(Protocol):
    def submit(self, trial_spec: TrialSpec) -> str:
        ...

    def status(self, handle: str) -> JsonDict:
        ...

    def cancel(self, handle: str) -> None:
        ...

    def collect(self, handle: str) -> List[Observation]:
        ...


class TrialScheduler(Protocol):
    def run_batch(self, trial_specs: List[TrialSpec]) -> List[Observation]:
        ...

    def submit_batch(self, trial_specs: List[TrialSpec]) -> List[JsonDict]:
        ...

    def collect_batch(self, handles: List[JsonDict]) -> List[Observation]:
        ...


class EvidenceStore(Protocol):
    def write_spec(self, spec: JsonDict) -> None:
        ...

    def write_run_policy(self, policy: JsonDict) -> None:
        ...

    def write_environment_snapshot(self, snapshot: JsonDict) -> None:
        ...

    def write_run_lineage(self, lineage: JsonDict) -> None:
        ...

    def record_artifact(self, artifact: JsonDict) -> None:
        ...

    def record_controller_decision(self, decision: JsonDict) -> None:
        ...

    def record_scheduler_event(self, event: JsonDict) -> None:
        ...

    def record_engine_snapshot(self, snapshot: JsonDict) -> None:
        ...

    def record_trial(self, trial: JsonDict) -> None:
        ...

    def record_observation(self, observation: JsonDict) -> None:
        ...

    def write_summary(self, summary: JsonDict) -> None:
        ...

    def create_trial_workspace(self, trial_id: str) -> Path:
        ...

    def read_controller_decisions(self) -> List[JsonDict]:
        ...

    def read_scheduler_events(self) -> List[JsonDict]:
        ...

    def read_engine_snapshots(self) -> List[JsonDict]:
        ...

    def read_artifacts(self) -> List[JsonDict]:
        ...

    def read_trials(self) -> List[JsonDict]:
        ...

    def read_observations(self) -> List[JsonDict]:
        ...

    def read_environment_snapshot(self) -> JsonDict:
        ...

    def read_run_lineage(self) -> JsonDict:
        ...
