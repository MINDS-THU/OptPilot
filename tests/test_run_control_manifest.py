from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

from optpilot.method_protocol_limits import MAX_BATCH_EXCHANGE_ITEMS
from optpilot.run_control_manifest import (
    ConvergencePolicy,
    RetryPolicy,
    RunControlIntegrityError,
    RunControlManifest,
    SubmissionControlRecord,
    build_run_controller,
    candidate_contract_digest,
    validate_submission_control_chain,
)


def _contract() -> dict[str, object]:
    return {
        "format": "parameters",
        "materialization": {"implementation": "builtin.parameter_to_config"},
        "validation": {"implementation": "builtin.schema_validation"},
    }


def _manifest(**overrides: object) -> RunControlManifest:
    values: dict[str, object] = {
        "method_id": "method-a",
        "method_protocol": "optpilot.method.batch.v1",
        "compiler_version": "study-compiler-v3",
        "normalizer_version": "candidate-normalizer-v2",
        "proposal_width": 2,
        "objective_metric": "score",
        "objective_direction": "maximize",
        "max_trials": 9,
        "max_failures": 3,
        "convergence": ConvergencePolicy(patience_trials=4, min_delta=0.25),
        "retry_policy": RetryPolicy(
            max_attempts=3,
            retryable_outcomes=("timeout", "failed"),
        ),
        "candidate_contract_digest": candidate_contract_digest(_contract()),
    }
    values.update(overrides)
    return RunControlManifest(**values)  # type: ignore[arg-type]


def _normalizer(candidate: dict[str, object]) -> dict[str, object]:
    candidate_id = candidate.get("candidate_id", candidate.get("id"))
    return {
        "candidate_id": candidate_id,
        "format": "parameters",
        "spec": dict(candidate.get("spec", {})),
        "lineage": {"parents": []},
        "generator": {"method_id": "method-a"},
        "validation": {},
        "materialization": {},
    }


class RunControlManifestTest(unittest.TestCase):
    def test_proposal_width_matches_the_durable_exchange_limit(self) -> None:
        self.assertEqual(
            _manifest(proposal_width=MAX_BATCH_EXCHANGE_ITEMS).proposal_width,
            MAX_BATCH_EXCHANGE_ITEMS,
        )
        with self.assertRaisesRegex(ValueError, "between 1 and"):
            _manifest(proposal_width=MAX_BATCH_EXCHANGE_ITEMS + 1)

    def test_manifest_is_canonical_immutable_and_digest_checked(self) -> None:
        manifest = _manifest()

        restored = RunControlManifest.from_bytes(
            manifest.to_bytes(), expected_digest=manifest.digest
        )

        self.assertEqual(restored, manifest)
        self.assertEqual(restored.digest, manifest.digest)
        self.assertEqual(
            restored.to_dict()["retry_policy"]["retryable_outcomes"],
            ["failed", "timeout"],
        )
        self.assertNotIn("/", restored.to_dict()["method"]["id"])
        with self.assertRaises(FrozenInstanceError):
            restored.proposal_width = 7  # type: ignore[misc]

    def test_candidate_contract_digest_is_order_independent_and_semantic(self) -> None:
        left = _contract()
        right = {
            "validation": {"implementation": "builtin.schema_validation"},
            "format": "parameters",
            "materialization": {"implementation": "builtin.parameter_to_config"},
        }

        self.assertEqual(candidate_contract_digest(left), candidate_contract_digest(right))
        right["format"] = "opaque"
        self.assertNotEqual(candidate_contract_digest(left), candidate_contract_digest(right))

        frozen = MappingProxyType(
            {
                "format": "parameters",
                "materialization": MappingProxyType(
                    {"implementation": "builtin.parameter_to_config"}
                ),
                "validation": MappingProxyType(
                    {"implementation": "builtin.schema_validation"}
                ),
            }
        )
        self.assertEqual(candidate_contract_digest(left), candidate_contract_digest(frozen))

    def test_manifest_rejects_unknown_fields_noncanonical_bytes_and_wrong_digest(self) -> None:
        manifest = _manifest()
        with self.assertRaisesRegex(RunControlIntegrityError, "digest does not match"):
            RunControlManifest.from_bytes(manifest.to_bytes(), expected_digest="0" * 64)

        extended = manifest.to_dict()
        extended["path"] = "/tmp/should-never-be-here"
        with self.assertRaisesRegex(RunControlIntegrityError, "extra=.*path"):
            RunControlManifest.from_dict(extended)

        pretty = json.dumps(manifest.to_dict(), indent=2).encode("utf-8")
        with self.assertRaisesRegex(RunControlIntegrityError, "not canonical JSON"):
            RunControlManifest.from_bytes(pretty)

    def test_manifest_policy_validation_removes_ambiguous_forms(self) -> None:
        with self.assertRaisesRegex(ValueError, "supported canonical protocol"):
            _manifest(method_protocol="batch")
        with self.assertRaisesRegex(ValueError, "must be zero"):
            ConvergencePolicy(patience_trials=None, min_delta=0.1)
        with self.assertRaisesRegex(ValueError, "duplicates"):
            RetryPolicy(max_attempts=2, retryable_outcomes=("failed", "failed"))

        payload = _manifest().to_dict()
        payload["retry_policy"]["retryable_outcomes"] = ["timeout", "failed"]
        with self.assertRaisesRegex(RunControlIntegrityError, "not canonical"):
            RunControlManifest.from_dict(payload)

    def test_reconstruction_requires_exact_contract_and_explicit_normalizer(self) -> None:
        manifest = _manifest()
        controller = build_run_controller(
            manifest,
            candidate_contract=_contract(),
            candidate_normalizer=_normalizer,
            normalizer_version="candidate-normalizer-v2",
            logical_trial_id_factory=lambda: "logical-a",
        )

        self.assertEqual(controller.method_id, "method-a")
        self.assertEqual(controller.objective_metric, "score")
        self.assertEqual(controller.next_proposal_width, 2)
        accepted = controller.accept_proposal([{"id": "candidate-a", "spec": {"x": 1}}])
        self.assertEqual(accepted[0].logical_trial_id, "logical-a")
        self.assertEqual(accepted[0].candidate["candidate_id"], "candidate-a")

        frozen_contract = MappingProxyType(
            {
                key: MappingProxyType(value) if isinstance(value, dict) else value
                for key, value in _contract().items()
            }
        )
        rebuilt = build_run_controller(
            manifest,
            candidate_contract=frozen_contract,
            candidate_normalizer=_normalizer,
            normalizer_version="candidate-normalizer-v2",
        )
        self.assertEqual(rebuilt.next_proposal_width, 2)

        bad_contract = _contract()
        bad_contract["format"] = "opaque"
        with self.assertRaisesRegex(ValueError, "does not match"):
            build_run_controller(
                manifest,
                candidate_contract=bad_contract,
                candidate_normalizer=_normalizer,
                normalizer_version="candidate-normalizer-v2",
            )
        with self.assertRaisesRegex(TypeError, "explicitly supplied callable"):
            build_run_controller(
                manifest,
                candidate_contract=_contract(),
                candidate_normalizer=None,  # type: ignore[arg-type]
                normalizer_version="candidate-normalizer-v2",
            )
        with self.assertRaisesRegex(ValueError, "version does not match"):
            build_run_controller(
                manifest,
                candidate_contract=_contract(),
                candidate_normalizer=_normalizer,
                normalizer_version="candidate-normalizer-v1",
            )


class SubmissionControlRecordTest(unittest.TestCase):
    def test_accepting_draining_terminal_chain_is_canonical_and_anchored(self) -> None:
        manifest = _manifest()
        accepting = SubmissionControlRecord.initial(manifest_digest=manifest.digest)
        draining = accepting.transition(
            state="draining", run_revision=5, stop_code="max_trials"
        )
        terminal = draining.transition(
            state="terminal", run_revision=9, stop_code="max_trials"
        )

        current = validate_submission_control_chain(
            (accepting, draining, terminal), manifest_digest=manifest.digest
        )

        self.assertEqual(current, terminal)
        self.assertEqual(draining.previous_record_digest, accepting.digest)
        self.assertEqual(terminal.previous_run_revision, 5)
        self.assertEqual(
            SubmissionControlRecord.from_bytes(
                terminal.to_bytes(), expected_digest=terminal.digest
            ),
            terminal,
        )

    def test_state_machine_rejects_skips_rewinds_and_post_terminal_appends(self) -> None:
        accepting = SubmissionControlRecord.initial(manifest_digest=_manifest().digest)
        with self.assertRaisesRegex(ValueError, "accepting -> draining"):
            accepting.transition(
                state="terminal", run_revision=1, stop_code="method_completed"
            )
        with self.assertRaisesRegex(ValueError, "greater than"):
            accepting.transition(
                state="draining", run_revision=0, stop_code="method_completed"
            )
        draining = accepting.transition(
            state="draining", run_revision=1, stop_code="method_completed"
        )
        terminal = draining.transition(
            state="terminal", run_revision=2, stop_code="no_successful_observation"
        )
        with self.assertRaisesRegex(ValueError, "accepting -> draining"):
            terminal.transition(
                state="terminal", run_revision=3, stop_code="no_successful_observation"
            )

    def test_chain_detects_forged_predecessor_and_manifest_anchors(self) -> None:
        manifest = _manifest()
        accepting = SubmissionControlRecord.initial(manifest_digest=manifest.digest)
        draining = accepting.transition(
            state="draining", run_revision=2, stop_code="wall_clock_budget"
        )
        forged = replace(draining, previous_record_digest="f" * 64)

        with self.assertRaisesRegex(RunControlIntegrityError, "predecessor anchor"):
            validate_submission_control_chain((accepting, forged))
        with self.assertRaisesRegex(RunControlIntegrityError, "different manifest"):
            validate_submission_control_chain(
                (accepting,), manifest_digest="e" * 64
            )

    def test_record_rejects_noncanonical_encoding_and_digest_mismatch(self) -> None:
        record = SubmissionControlRecord.initial(manifest_digest=_manifest().digest)
        with self.assertRaisesRegex(RunControlIntegrityError, "digest does not match"):
            SubmissionControlRecord.from_bytes(
                record.to_bytes(), expected_digest="0" * 64
            )
        with self.assertRaisesRegex(RunControlIntegrityError, "not canonical JSON"):
            SubmissionControlRecord.from_bytes(record.to_bytes() + b"\n")


if __name__ == "__main__":
    unittest.main()
