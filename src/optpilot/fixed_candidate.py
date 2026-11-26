"""Batch-protocol adapter for evaluating exact selected decisions.

Each proposal names one reviewed input to the comparison.
The runner validates and seals the returned candidate using its normal path.
"""
from pathlib import Path


class FixedCandidate:
    def __init__(self, definition, study_spec, rng):
        del study_spec, rng
        self.settings = definition["settings"]
        self.position = 0

    def propose(self, n_candidates, study_state, evidence_view=None):
        del evidence_view
        decisions = self.settings["decisions"]
        batch = decisions[self.position:self.position + n_candidates]
        if not batch:
            return []
        self.position += len(batch)
        if self.settings["format"] == "parameters":
            return [{"candidate_id": item["id"], "format": "parameters",
                     "spec": item["parameters"], "lineage": {"parents": []},
                     "generator": self._generator(item)} for item in batch]
        from optpilot.candidate_staging import CandidateBundleStager
        stager = CandidateBundleStager(study_state["runtime_context"]["candidate_staging_dir"])
        return [self._stage_retained(stager, item) for item in batch]

    def _stage_retained(self, stager, item):
        # Preparation already validated these exact trees. Use
        # explicit paths so the generic directory helper cannot omit files.
        root = Path(__file__).parent / item["directory"]
        entries = sorted(root.rglob("*"))
        candidate = stager.stage_files(
            [(entry, entry.relative_to(root).as_posix()) for entry in entries if entry.is_file()],
            candidate_id=item["id"],
            generator=self._generator(item),
        )
        destination = Path(candidate["spec"]["bundleRef"])
        for entry in entries:
            if entry.is_dir():
                (destination / entry.relative_to(root)).mkdir(parents=True, exist_ok=True)
        return candidate

    @staticmethod
    def _generator(item):
        generator = {"method_id": "fixed-candidates", "strategy": "selected-decision"}
        if item.get("name"):
            generator["name"] = item["name"]
        return generator

    def observe(self, observations):
        del observations
