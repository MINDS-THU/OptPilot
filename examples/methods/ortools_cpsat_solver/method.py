"""Method that emits the included JobShopLib OR-Tools solver as a file candidate."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List

from optpilot.candidate_files import CandidateFileStore


JsonDict = Dict[str, Any]


class OrToolsCpSatSolverMethod:
    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.study_spec = study_spec
        self._emitted = False

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if self._emitted or n_candidates <= 0:
            return []
        runtime_context = dict(study_state.get("runtime_context", {}))
        candidate_store_dir = runtime_context.get("candidate_store_dir")
        if not candidate_store_dir:
            raise ValueError("OrToolsCpSatSolverMethod requires runtime_context.candidate_store_dir.")
        self._emitted = True
        store = CandidateFileStore(
            candidate_store_dir,
            content_ref_mode=runtime_context.get("candidate_content_ref_mode", "absolute"),
        )
        return [
            store.store_file(
                Path(__file__).with_name("solver.py"),
                path="solver.py",
                candidate_id=f"job-shop-lib-cpsat-{uuid.uuid4().hex[:12]}",
                generator={
                    "method_id": self.definition["id"],
                    "strategy": "job_shop_lib_ortools_solver",
                },
                metadata={
                    "summary": "JobShopLib ORToolsSolver candidate for job-shop scheduling.",
                },
            )
        ]

    def observe(self, observations: List[JsonDict]) -> None:
        return None
