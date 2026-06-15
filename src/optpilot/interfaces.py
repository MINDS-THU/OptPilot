"""Public integration protocols for OptPilot components."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Protocol

from .models import Observation, TrialSpec


JsonDict = Dict[str, Any]


class CandidateProposalMethod(Protocol):
    """Simple synchronous method shape used by the runner."""

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        ...

    def observe(self, observations: List[JsonDict]) -> None:
        ...


class Method(Protocol):
    """Lifecycle-oriented method interface for longer-running workflows."""

    def start(self, method_input: JsonDict) -> str:
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

    def record_method_call(self, call: JsonDict) -> None:
        ...

    def record_scheduler_event(self, event: JsonDict) -> None:
        ...

    def record_method_event(self, event: JsonDict) -> None:
        ...

    def record_trial(self, trial: JsonDict) -> None:
        ...

    def record_observation(self, observation: JsonDict) -> None:
        ...

    def write_summary(self, summary: JsonDict) -> None:
        ...

    def create_trial_workspace(self, trial_id: str) -> Path:
        ...

    def read_method_calls(self) -> List[JsonDict]:
        ...

    def read_scheduler_events(self) -> List[JsonDict]:
        ...

    def read_method_events(self) -> List[JsonDict]:
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
