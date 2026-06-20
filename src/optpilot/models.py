"""Core data models for OptPilot."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


@dataclass
class ResourceProfile:
    cpu: int = 1
    memory_gib: int = 1
    gpu: int = 0
    gpu_class: Optional[str] = None
    timeout_seconds: int = 600
    disk_gib: Optional[int] = None
    network_required: bool = False
    runtime_image: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Optional[JsonDict]) -> "ResourceProfile":
        data = data or {}
        return cls(
            cpu=int(data.get("cpu", 1)),
            memory_gib=int(data.get("memoryGiB", data.get("memory_gib", 1))),
            gpu=int(data.get("gpu", 0)),
            gpu_class=data.get("gpuClass") or data.get("gpu_class"),
            timeout_seconds=int(data.get("timeoutSeconds", data.get("timeout_seconds", 600))),
            disk_gib=data.get("diskGiB") or data.get("disk_gib"),
            network_required=bool(data.get("networkRequired", data.get("network_required", False))),
            runtime_image=data.get("runtimeImage") or data.get("runtime_image"),
        )

    def to_dict(self) -> JsonDict:
        payload: JsonDict = {
            "cpu": self.cpu,
            "memoryGiB": self.memory_gib,
            "gpu": self.gpu,
            "gpuClass": self.gpu_class,
            "timeoutSeconds": self.timeout_seconds,
            "diskGiB": self.disk_gib,
            "networkRequired": self.network_required,
            "runtimeImage": self.runtime_image,
        }
        return {key: value for key, value in payload.items() if value is not None}


@dataclass
class SandboxSpec:
    runtime_type: str = "process"
    writable_workspace: Optional[str] = None
    read_only_mounts: List[str] = field(default_factory=list)
    network_policy: str = "disabled"
    environment_variables: JsonDict = field(default_factory=dict)
    cleanup_policy: str = "always"

    @classmethod
    def from_dict(cls, data: Optional[JsonDict]) -> "SandboxSpec":
        data = data or {}
        return cls(
            runtime_type=data.get("runtimeType", data.get("runtime_type", "process")),
            writable_workspace=data.get("writableWorkspace") or data.get("writable_workspace"),
            read_only_mounts=list(data.get("readOnlyMounts", data.get("read_only_mounts", []))),
            network_policy=data.get("networkPolicy", data.get("network_policy", "disabled")),
            environment_variables=dict(data.get("environmentVariables", data.get("environment_variables", {}))),
            cleanup_policy=data.get("cleanupPolicy", data.get("cleanup_policy", "always")),
        )

    def to_dict(self) -> JsonDict:
        return {
            "runtimeType": self.runtime_type,
            "writableWorkspace": self.writable_workspace,
            "readOnlyMounts": list(self.read_only_mounts),
            "networkPolicy": self.network_policy,
            "environmentVariables": dict(self.environment_variables),
            "cleanupPolicy": self.cleanup_policy,
        }


@dataclass
class Candidate:
    candidate_id: str
    format: str
    spec: JsonDict
    lineage: JsonDict = field(default_factory=dict)
    generator: JsonDict = field(default_factory=dict)
    validation: JsonDict = field(default_factory=dict)
    materialization: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class TrialSpec:
    trial_id: str
    study_id: str
    method_id: str
    candidate: JsonDict
    objective: JsonDict
    resource_profile: ResourceProfile
    sandbox_spec: SandboxSpec
    metadata: JsonDict = field(default_factory=dict)


@dataclass
class Observation:
    trial_id: str
    study_id: str
    candidate_id: str
    environment_id: str
    status: str
    metric_values: JsonDict
    constraint_results: JsonDict
    resource_usage: JsonDict
    output_files: List[JsonDict]
    event_summary: JsonDict
    provenance: JsonDict

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class RunSummary:
    study_id: str
    run_dir: str
    completed_trials: int
    best_trial_id: Optional[str]
    best_metric: Optional[float]
    best_candidate_id: Optional[str]
    started_at: str
    finished_at: str
    failure_count: int = 0
    policy: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)



def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
