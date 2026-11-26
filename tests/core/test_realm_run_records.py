from __future__ import annotations

import unittest

from optpilot.realm.errors import RealmIntegrityError
from optpilot.realm.refs import BlobRef
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
    RunCandidateSelection,
    SessionHandleAdmission,
)


class RunAdmissionRecordTest(unittest.TestCase):
    def test_parameter_plan_is_canonical_immutable_and_round_trips(self) -> None:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters",
            spec={"x": 3, "nested": {"enabled": True}},
        )
        candidate = CandidateAdmission(
            "candidate-a",
            envelope,
            lineage={"parents": []},
            generator={"method_id": "method-a"},
        )
        trial = LogicalTrialAdmission(
            "trial-a",
            "candidate-a",
            seed=7,
            repetition_index=0,
            submission_metadata={"iteration": 3},
        )
        plan = RunAdmissionPlan(
            (candidate,),
            (trial,),
            (SessionHandleAdmission("handle-a", "trial-a"),),
        )

        self.assertEqual(RunAdmissionPlan.from_dict(plan.to_dict()), plan)
        self.assertEqual(plan.digest, RunAdmissionPlan.from_dict(plan.to_dict()).digest)
        with self.assertRaises((TypeError, ValueError)):
            envelope.spec["x"] = 4  # type: ignore[index]

    def test_file_envelope_requires_an_explicit_content_closure(self) -> None:
        blob = BlobRef.from_bytes(b"payload")
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="files",
            spec={"entrypoint": "run.py"},
            content_refs=(blob,),
        )
        self.assertEqual(
            NormalizedCandidateEnvelope.from_dict(envelope.to_dict()), envelope
        )
        with self.assertRaisesRegex(ValueError, "require at least one"):
            NormalizedCandidateEnvelope.build(
                candidate_format="files", spec={"entrypoint": "run.py"}
            )

    def test_opaque_ref_and_path_like_strings_remain_opaque_data(self) -> None:
        blob = BlobRef.from_bytes(b"payload")
        spec = {
            "contentRef": str(blob),
            "posix": "/tmp/live.py",
            "windows": "C:\\Users\\person\\live.py",
        }
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="opaque", spec=spec
        )
        self.assertEqual(envelope.content_refs, ())
        self.assertEqual(envelope.to_dict()["spec"], spec)

    def test_nonfinite_values_are_not_canonical_candidate_identity(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            NormalizedCandidateEnvelope.build(
                candidate_format="parameters", spec={"value": float("nan")}
            )

    def test_plan_rejects_duplicate_identity_and_unreferenced_candidates(self) -> None:
        first = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        second = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 2}
        )
        candidate_a = CandidateAdmission("candidate-a", first)
        candidate_b = CandidateAdmission("candidate-b", second)
        trial = LogicalTrialAdmission(
            "trial-a", "candidate-a", seed=1
        )
        with self.assertRaisesRegex(ValueError, "at least one logical trial"):
            RunAdmissionPlan((candidate_a, candidate_b), (trial,))
        with self.assertRaisesRegex(ValueError, "duplicate candidate ids"):
            RunAdmissionPlan(
                (candidate_a, CandidateAdmission("candidate-a", second)),
                (
                    trial,
                    LogicalTrialAdmission(
                        "trial-b", "candidate-a", seed=2
                    ),
                ),
            )

    def test_duplicate_candidate_refs_and_repeated_evaluations_are_allowed(self) -> None:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        plan = RunAdmissionPlan(
            (
                CandidateAdmission("candidate-a", envelope),
                CandidateAdmission("candidate-b", envelope),
            ),
            (
                LogicalTrialAdmission("trial-a0", "candidate-a", seed=1),
                LogicalTrialAdmission("trial-a1", "candidate-a", seed=1),
                LogicalTrialAdmission("trial-b0", "candidate-b", seed=1),
            ),
        )
        self.assertEqual(
            [item.candidate_ref for item in plan.candidates],
            [envelope.candidate_ref, envelope.candidate_ref],
        )

    def test_session_handles_are_optional_and_reference_plan_trials(self) -> None:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        candidate = CandidateAdmission("candidate-a", envelope)
        trial = LogicalTrialAdmission(
            "trial-a",
            "candidate-a",
            submission_metadata={"display_label": "first"},
        )
        batch_plan = RunAdmissionPlan((candidate,), (trial,))
        self.assertEqual(batch_plan.session_handles, ())

        session_plan = RunAdmissionPlan(
            (candidate,),
            (trial,),
            (SessionHandleAdmission("handle-a", "trial-a"),),
        )
        self.assertEqual(RunAdmissionPlan.from_dict(session_plan.to_dict()), session_plan)
        with self.assertRaisesRegex(ValueError, "reference a logical trial"):
            RunAdmissionPlan(
                (candidate,),
                (trial,),
                (SessionHandleAdmission("handle-a", "trial-missing"),),
            )

    def test_plan_serialization_rejects_legacy_or_extra_fields(self) -> None:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        plan = RunAdmissionPlan(
            (CandidateAdmission("candidate-a", envelope),),
            (LogicalTrialAdmission("trial-a", "candidate-a"),),
        )
        payload = plan.to_dict()
        del payload["session_handles"]
        with self.assertRaises(ValueError):
            RunAdmissionPlan.from_dict(payload)

        legacy_trial = plan.to_dict()
        legacy_trial["logical_trials"][0]["submission_handle_id"] = "handle-a"
        with self.assertRaises(RealmIntegrityError):
            RunAdmissionPlan.from_dict(legacy_trial)

    def test_selection_digest_detects_tampering(self) -> None:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        selection = RunCandidateSelection.build(
            run_id="run-a",
            evaluation_template_digest="a" * 64,
            run_revision=1,
            owner_revision=0,
            sequence=1,
            candidate_id="candidate-a",
            candidate_ref=envelope.candidate_ref,
        )
        self.assertEqual(RunCandidateSelection.from_dict(selection.to_dict()), selection)
        payload = selection.to_dict()
        payload["sequence"] = 2
        with self.assertRaises(RealmIntegrityError):
            RunCandidateSelection.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
