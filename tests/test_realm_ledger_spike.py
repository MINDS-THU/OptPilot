"""Crash-safety checks for the disposable RealmLedger architecture spike."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Sequence

from scripts.spikes.realm_ledger_spike import (
    LedgerConflict,
    LedgerExpired,
    RealmLedgerSpike,
    candidate_ref_for,
    fake_content_ref,
)


class InjectedCrash(RuntimeError):
    pass


class RealmLedgerSpikeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "realm" / "realm.sqlite3"
        self.ledger = RealmLedgerSpike(self.database_path)
        self.run = self.ledger.create_run(run_id="run-a", store_id="store-a", max_trials=4, now=10.0)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _stage(self, *, owner_id: str, label: str, role: str, now: float = 10.0) -> tuple[str, str]:
        intent_id = self.ledger.begin_owner_intent(
            owner_id=owner_id,
            ttl_seconds=100.0,
            now=now,
        )
        content_ref = fake_content_ref(label)
        self.ledger.stage_content(
            intent_id=intent_id,
            content_ref=content_ref,
            role=role,
            size_bytes=len(label),
            metadata={"label": label},
            now=now,
        )
        return intent_id, content_ref

    def _commit_candidate(
        self,
        *,
        operation_id: str,
        intent_id: str,
        candidate_content_refs: Sequence[str],
        candidate_ref: str | None = None,
        expected_run_revision: int = 0,
        fencing_token: int = 1,
        controller_id: str = "controller-a",
        candidate_id: str = "candidate-a",
        trial_id: str = "trial-a",
        handle_id: str = "handle-a",
    ):
        payload = {"x": 1}
        candidate_ref = candidate_ref or candidate_ref_for(
            candidate_format="files",
            spec=payload,
            content_refs=candidate_content_refs,
        )
        return self.ledger.commit_candidate(
            operation_id=operation_id,
            intent_id=intent_id,
            run_id="run-a",
            expected_run_revision=expected_run_revision,
            controller_id=controller_id,
            fencing_token=fencing_token,
            candidate_id=candidate_id,
            candidate_ref=candidate_ref,
            candidate_format="files",
            candidate_content_refs=candidate_content_refs,
            logical_trial_id=trial_id,
            handle_id=handle_id,
            seed=7,
            repetition_index=0,
            payload=payload,
            now=11.0,
        )

    def test_candidate_budget_records_and_owner_membership_commit_together(self) -> None:
        intent_id, content_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="candidate-tree",
            role="candidate",
        )

        receipt = self._commit_candidate(
            operation_id="accept-a",
            intent_id=intent_id,
            candidate_content_refs=[content_ref],
        )

        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(receipt.run_revision, 1)
        self.assertEqual(receipt.owner_revision, 1)
        self.assertEqual((receipt.first_sequence, receipt.last_sequence), (1, 2))
        self.assertEqual(snapshot["run"]["accepted_trials"], 1)
        self.assertEqual(snapshot["run"]["next_sequence"], 2)
        self.assertEqual([item["content_ref"] for item in snapshot["owner_content"]], [content_ref])
        self.assertEqual([item["candidate_id"] for item in snapshot["candidates"]], ["candidate-a"])
        self.assertEqual([item["logical_trial_id"] for item in snapshot["logical_trials"]], ["trial-a"])
        self.assertEqual(
            [(item["handle_id"], item["state"]) for item in snapshot["handles"]],
            [("handle-a", "accepted")],
        )
        self.assertEqual([item["sequence"] for item in snapshot["events"]], [1, 2])
        self.assertEqual(self.ledger.integrity_check(), {
            "journal_mode": "wal",
            "integrity": ["ok"],
            "foreign_key_errors": [],
        })

    def test_one_realm_ledger_spans_isolated_store_namespaces(self) -> None:
        run_b = self.ledger.create_run(
            run_id="run-b",
            store_id="store-b",
            max_trials=1,
            now=10.0,
        )
        shared_ref = fake_content_ref("identical-bytes")
        payload = {"x": 1}
        for run_id, owner_id, operation_id in (
            ("run-a", self.run["owner_id"], "store-a-accept"),
            ("run-b", run_b["owner_id"], "store-b-accept"),
        ):
            intent_id = self.ledger.begin_owner_intent(
                owner_id=owner_id,
                ttl_seconds=100.0,
                now=10.0,
            )
            self.ledger.stage_content(
                intent_id=intent_id,
                content_ref=shared_ref,
                role="candidate",
                size_bytes=15,
                metadata={"label": "identical-bytes"},
                now=10.0,
            )
            self.ledger.commit_candidate(
                operation_id=operation_id,
                intent_id=intent_id,
                run_id=run_id,
                expected_run_revision=0,
                controller_id="controller-a",
                fencing_token=1,
                candidate_id="candidate-shared",
                candidate_ref=candidate_ref_for(
                    candidate_format="files",
                    spec=payload,
                    content_refs=[shared_ref],
                ),
                candidate_format="files",
                candidate_content_refs=[shared_ref],
                logical_trial_id="trial-shared",
                handle_id="handle-shared",
                seed=0,
                repetition_index=0,
                payload=payload,
                now=11.0,
            )

        snapshot_a = self.ledger.snapshot("run-a")
        snapshot_b = self.ledger.snapshot("run-b")
        self.assertEqual(snapshot_a["run"]["store_id"], "store-a")
        self.assertEqual(snapshot_b["run"]["store_id"], "store-b")
        self.assertEqual(snapshot_a["owner_content"][0]["content_ref"], shared_ref)
        self.assertEqual(snapshot_b["owner_content"][0]["content_ref"], shared_ref)
        with sqlite3.connect(str(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM realm_meta WHERE key='realm_id'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM stores").fetchone()[0], 2)
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM content_objects WHERE content_ref=?",
                    (shared_ref,),
                ).fetchone()[0],
                2,
            )

    def test_injected_failure_rolls_back_owner_and_run_state(self) -> None:
        intent_id, content_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="candidate-tree",
            role="candidate",
        )

        def crash(step: str) -> None:
            if step == "after_owner_membership":
                raise InjectedCrash(step)

        self.ledger.fault_hook = crash
        with self.assertRaises(InjectedCrash):
            self._commit_candidate(
                operation_id="accept-a",
                intent_id=intent_id,
                candidate_content_refs=[content_ref],
            )

        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(snapshot["run"]["accepted_trials"], 0)
        self.assertEqual(snapshot["run"]["run_revision"], 0)
        self.assertEqual(snapshot["run"]["owner_revision"], 0)
        self.assertEqual(snapshot["owner_content"], [])
        self.assertEqual(snapshot["candidates"], [])
        self.assertEqual(snapshot["logical_trials"], [])
        self.assertEqual(snapshot["handles"], [])
        self.assertEqual(snapshot["events"], [])
        self.assertEqual(snapshot["operations"], [])

        self.ledger.fault_hook = None
        receipt = self._commit_candidate(
            operation_id="accept-a",
            intent_id=intent_id,
            candidate_content_refs=[content_ref],
        )
        self.assertEqual(receipt.run_revision, 1)

    def test_operation_replay_after_lost_response_is_exactly_once(self) -> None:
        intent_id, content_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="candidate-tree",
            role="candidate",
        )
        arguments = {
            "operation_id": "accept-a",
            "intent_id": intent_id,
            "candidate_content_refs": [content_ref],
        }

        first = self._commit_candidate(**arguments)
        replay = self._commit_candidate(**arguments)

        self.assertEqual(replay, first)
        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(len(snapshot["candidates"]), 1)
        self.assertEqual(len(snapshot["logical_trials"]), 1)
        self.assertEqual(len(snapshot["owner_content"]), 1)
        self.assertEqual(len(snapshot["operations"]), 1)

        with self.assertRaises(LedgerConflict):
            self._commit_candidate(
                operation_id="accept-a",
                intent_id=intent_id,
                candidate_content_refs=[content_ref],
                candidate_ref=fake_content_ref("different-envelope"),
            )

    def test_attempt_and_artifact_commit_roll_back_and_retry_atomically(self) -> None:
        candidate_intent, candidate_content_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="candidate-tree",
            role="candidate",
        )
        candidate_receipt = self._commit_candidate(
            operation_id="accept-a",
            intent_id=candidate_intent,
            candidate_content_refs=[candidate_content_ref],
        )
        artifact_intent, artifact_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="attempt-output",
            role="artifact",
            now=12.0,
        )

        def crash(step: str) -> None:
            if step == "after_domain_records":
                raise InjectedCrash(step)

        self.ledger.fault_hook = crash
        arguments = {
            "operation_id": "attempt-a",
            "intent_id": artifact_intent,
            "run_id": "run-a",
            "expected_run_revision": candidate_receipt.run_revision,
            "controller_id": "controller-a",
            "fencing_token": 1,
            "logical_trial_id": "trial-a",
            "attempt_id": "attempt-a",
            "attempt_index": 1,
            "outcome": "success",
            "observation": {"metric_values": {"score": 0.9}},
            "artifacts": [
                {
                    "artifact_id": "artifact-a",
                    "content_ref": artifact_ref,
                    "role": "artifact",
                }
            ],
            "payload": {"runtime_seconds": 1.2},
            "now": 13.0,
        }
        with self.assertRaises(InjectedCrash):
            self.ledger.commit_attempt(**arguments)

        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(snapshot["attempts"], [])
        self.assertEqual(snapshot["observations"], [])
        self.assertEqual(snapshot["artifacts"], [])
        self.assertNotIn(artifact_ref, [item["content_ref"] for item in snapshot["owner_content"]])
        self.assertEqual(snapshot["logical_trials"][0]["state"], "accepted")
        self.assertEqual(snapshot["handles"][0]["state"], "accepted")

        self.ledger.fault_hook = None
        receipt = self.ledger.commit_attempt(**arguments)
        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(receipt.run_revision, 2)
        self.assertEqual(receipt.owner_revision, 2)
        self.assertEqual(snapshot["logical_trials"][0]["state"], "terminal")
        self.assertEqual(snapshot["handles"][0]["state"], "terminal")
        self.assertEqual(len(snapshot["attempts"]), 1)
        self.assertEqual(len(snapshot["observations"]), 1)
        self.assertEqual([item["content_ref"] for item in snapshot["artifacts"]], [artifact_ref])
        self.assertIn(artifact_ref, [item["content_ref"] for item in snapshot["owner_content"]])
        self.assertEqual([item["sequence"] for item in snapshot["events"]], [1, 2, 3, 4])

    def test_stale_fence_cannot_adopt_provisional_content(self) -> None:
        handoff = self.ledger.advance_fence(
            operation_id="handoff-a",
            run_id="run-a",
            expected_controller_id="controller-a",
            expected_fencing_token=1,
            new_controller_id="controller-b",
            now=11.0,
        )
        self.assertEqual(handoff.fencing_token, 2)
        intent_id, content_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="stale-candidate",
            role="candidate",
            now=12.0,
        )

        with self.assertRaises(LedgerConflict):
            self._commit_candidate(
                operation_id="stale-accept",
                intent_id=intent_id,
                candidate_content_refs=[content_ref],
                expected_run_revision=1,
                controller_id="controller-a",
                fencing_token=1,
            )

        snapshot = self.ledger.snapshot("run-a")
        self.assertNotIn(content_ref, [item["content_ref"] for item in snapshot["owner_content"]])
        self.assertEqual(snapshot["run"]["accepted_trials"], 0)

    def test_expired_intent_loses_provisional_membership_only(self) -> None:
        intent_id = self.ledger.begin_owner_intent(
            owner_id=self.run["owner_id"],
            ttl_seconds=1.0,
            now=20.0,
        )
        abandoned_ref = fake_content_ref("abandoned")
        self.ledger.stage_content(
            intent_id=intent_id,
            content_ref=abandoned_ref,
            role="candidate",
            size_bytes=9,
            now=20.0,
        )
        self.assertEqual(self.ledger.expire_owner_intents(now=22.0), 1)

        with self.assertRaises(LedgerExpired):
            self._commit_candidate(
                operation_id="expired-accept",
                intent_id=intent_id,
                candidate_content_refs=[abandoned_ref],
            )
        self.assertEqual(self.ledger.snapshot("run-a")["owner_content"], [])

    def test_content_closure_rejects_missing_or_extra_staged_refs(self) -> None:
        intent_id, first_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="first-tree",
            role="candidate",
        )
        extra_ref = fake_content_ref("extra-tree")
        self.ledger.stage_content(
            intent_id=intent_id,
            content_ref=extra_ref,
            role="candidate",
            size_bytes=10,
            metadata={"label": "extra-tree"},
            now=10.0,
        )

        with self.assertRaisesRegex(LedgerConflict, "extra"):
            self._commit_candidate(
                operation_id="extra-closure",
                intent_id=intent_id,
                candidate_content_refs=[first_ref],
            )
        self.assertEqual(self.ledger.snapshot("run-a")["owner_content"], [])

        missing_intent, staged_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="staged-tree",
            role="candidate",
        )
        missing_ref = fake_content_ref("missing-tree")
        with self.assertRaisesRegex(LedgerConflict, "missing"):
            self._commit_candidate(
                operation_id="missing-closure",
                intent_id=missing_intent,
                candidate_content_refs=[staged_ref, missing_ref],
            )
        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(snapshot["owner_content"], [])
        self.assertEqual(snapshot["candidates"], [])

    def test_reusing_owned_content_does_not_duplicate_membership_or_revision(self) -> None:
        first_intent, content_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="shared-tree",
            role="candidate",
        )
        first = self._commit_candidate(
            operation_id="shared-first",
            intent_id=first_intent,
            candidate_content_refs=[content_ref],
            candidate_id="candidate-first",
            trial_id="trial-first",
            handle_id="handle-first",
        )
        self.assertEqual(first.owner_revision, 1)

        second_intent, same_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="shared-tree",
            role="candidate",
        )
        second = self._commit_candidate(
            operation_id="shared-second",
            intent_id=second_intent,
            candidate_content_refs=[same_ref],
            expected_run_revision=1,
            candidate_id="candidate-second",
            trial_id="trial-second",
            handle_id="handle-second",
        )

        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(second.owner_revision, 1)
        self.assertEqual(second.run_revision, 2)
        self.assertEqual(len(snapshot["owner_content"]), 1)
        self.assertEqual(len(snapshot["candidates"]), 2)

    def test_stale_owner_intent_cannot_silently_rebase(self) -> None:
        first_intent, first_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="owner-revision-first",
            role="candidate",
        )
        stale_intent, stale_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="owner-revision-stale",
            role="candidate",
        )
        self._commit_candidate(
            operation_id="owner-revision-first",
            intent_id=first_intent,
            candidate_content_refs=[first_ref],
            candidate_id="candidate-first",
            trial_id="trial-first",
            handle_id="handle-first",
        )

        with self.assertRaisesRegex(LedgerConflict, "stale owner revision"):
            self._commit_candidate(
                operation_id="owner-revision-stale",
                intent_id=stale_intent,
                candidate_content_refs=[stale_ref],
                expected_run_revision=1,
                candidate_id="candidate-stale",
                trial_id="trial-stale",
                handle_id="handle-stale",
            )

        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(snapshot["run"]["run_revision"], 1)
        self.assertEqual(len(snapshot["owner_content"]), 1)
        self.assertEqual(len(snapshot["candidates"]), 1)

    def test_unsealed_or_unverified_content_cannot_enter_owner_closure(self) -> None:
        for label, flags in (
            ("unsealed", {"sealed": False, "verified": True}),
            ("unverified", {"sealed": True, "verified": False}),
        ):
            with self.subTest(label=label):
                intent_id = self.ledger.begin_owner_intent(
                    owner_id=self.run["owner_id"],
                    ttl_seconds=100.0,
                    now=10.0,
                )
                content_ref = fake_content_ref(label)
                self.ledger.stage_content(
                    intent_id=intent_id,
                    content_ref=content_ref,
                    role="candidate",
                    size_bytes=len(label),
                    metadata={"label": label},
                    now=10.0,
                    **flags,
                )
                with self.assertRaisesRegex(LedgerConflict, "sealed and verified"):
                    self._commit_candidate(
                        operation_id=f"reject-{label}",
                        intent_id=intent_id,
                        candidate_content_refs=[content_ref],
                        candidate_id=f"candidate-{label}",
                        trial_id=f"trial-{label}",
                        handle_id=f"handle-{label}",
                    )

        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(snapshot["owner_content"], [])
        self.assertEqual(snapshot["candidates"], [])

    def test_parameter_candidate_can_commit_without_content_membership(self) -> None:
        intent_id = self.ledger.begin_owner_intent(
            owner_id=self.run["owner_id"],
            ttl_seconds=100.0,
            now=10.0,
        )
        payload = {"learning_rate": 0.1}
        receipt = self.ledger.commit_candidate(
            operation_id="parameter-candidate",
            intent_id=intent_id,
            run_id="run-a",
            expected_run_revision=0,
            controller_id="controller-a",
            fencing_token=1,
            candidate_id="candidate-parameters",
            candidate_ref=candidate_ref_for(
                candidate_format="parameters",
                spec=payload,
                content_refs=[],
            ),
            candidate_format="parameters",
            candidate_content_refs=[],
            logical_trial_id="trial-parameters",
            handle_id="handle-parameters",
            seed=0,
            repetition_index=0,
            payload=payload,
            now=11.0,
        )

        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(receipt.owner_revision, 0)
        self.assertEqual(snapshot["owner_content"], [])
        self.assertEqual(snapshot["handles"][0]["state"], "accepted")

    def test_controller_handoff_replays_after_lost_response(self) -> None:
        def lose_response(step: str) -> None:
            if step == "after_commit":
                raise InjectedCrash(step)

        arguments = {
            "operation_id": "handoff-lost-response",
            "run_id": "run-a",
            "expected_controller_id": "controller-a",
            "expected_fencing_token": 1,
            "new_controller_id": "controller-b",
            "now": 11.0,
        }
        self.ledger.fault_hook = lose_response
        with self.assertRaises(InjectedCrash):
            self.ledger.advance_fence(**arguments)

        self.ledger.fault_hook = None
        receipt = self.ledger.advance_fence(**arguments)
        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(receipt.controller_id, "controller-b")
        self.assertEqual(receipt.fencing_token, 2)
        self.assertEqual(snapshot["run"]["controller_id"], "controller-b")
        self.assertEqual(snapshot["run"]["fencing_token"], 2)
        self.assertEqual(len(snapshot["operations"]), 1)

        with self.assertRaisesRegex(LedgerConflict, "different request"):
            self.ledger.advance_fence(**{**arguments, "new_controller_id": "controller-c"})

    def test_competing_controller_handoffs_choose_one_holder(self) -> None:
        path = Path(self.temp_dir.name) / "handoff" / "realm.sqlite3"
        primary = RealmLedgerSpike(path)
        primary.create_run(run_id="run-h", store_id="store-a", max_trials=1)
        write_barrier = threading.Barrier(2)

        def handoff(index: int):
            def synchronize_before_lock(step: str) -> None:
                if step == "before_write_lock":
                    write_barrier.wait(timeout=2.0)

            ledger = RealmLedgerSpike(path, fault_hook=synchronize_before_lock)
            try:
                return ledger.advance_fence(
                    operation_id=f"handoff-{index}",
                    run_id="run-h",
                    expected_controller_id="controller-a",
                    expected_fencing_token=1,
                    new_controller_id=f"controller-{index}",
                )
            except LedgerConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(handoff, range(2)))

        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        snapshot = primary.snapshot("run-h")
        self.assertEqual(snapshot["run"]["controller_id"], winners[0].controller_id)
        self.assertEqual(snapshot["run"]["fencing_token"], 2)
        self.assertEqual(snapshot["run"]["run_revision"], 1)
        self.assertEqual(len(snapshot["operations"]), 1)

    def test_intent_expiry_is_checked_after_waiting_for_write_lock(self) -> None:
        intent_id = self.ledger.begin_owner_intent(
            owner_id=self.run["owner_id"],
            ttl_seconds=0.15,
        )
        content_ref = fake_content_ref("short-lived")
        self.ledger.stage_content(
            intent_id=intent_id,
            content_ref=content_ref,
            role="candidate",
            size_bytes=11,
            metadata={"label": "short-lived"},
        )
        locked = threading.Event()

        def hold_write_lock() -> None:
            connection = sqlite3.connect(str(self.database_path), isolation_level=None)
            try:
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("BEGIN IMMEDIATE")
                locked.set()
                time.sleep(0.30)
                connection.commit()
            finally:
                connection.close()

        holder = threading.Thread(target=hold_write_lock)
        holder.start()
        self.assertTrue(locked.wait(timeout=2.0))
        payload = {"x": 1}
        with self.assertRaises(LedgerExpired):
            self.ledger.commit_candidate(
                operation_id="expired-after-lock",
                intent_id=intent_id,
                run_id="run-a",
                expected_run_revision=0,
                controller_id="controller-a",
                fencing_token=1,
                candidate_id="candidate-expired",
                candidate_ref=candidate_ref_for(
                    candidate_format="files",
                    spec=payload,
                    content_refs=[content_ref],
                ),
                candidate_format="files",
                candidate_content_refs=[content_ref],
                logical_trial_id="trial-expired",
                handle_id="handle-expired",
                seed=0,
                repetition_index=0,
                payload=payload,
            )
        holder.join(timeout=2.0)
        self.assertFalse(holder.is_alive())
        self.assertEqual(self.ledger.snapshot("run-a")["owner_content"], [])

    def test_snapshots_are_transactionally_consistent_during_writes(self) -> None:
        path = Path(self.temp_dir.name) / "snapshot" / "realm.sqlite3"
        writer_ledger = RealmLedgerSpike(path)
        run = writer_ledger.create_run(run_id="run-s", store_id="store-a", max_trials=30)
        finished = threading.Event()
        first_committed = threading.Event()
        continue_after_first = threading.Event()
        failures = []

        def write_candidates() -> None:
            try:
                for index in range(20):
                    intent = writer_ledger.begin_owner_intent(
                        owner_id=run["owner_id"],
                        ttl_seconds=10.0,
                    )
                    content_ref = fake_content_ref(f"snapshot-tree-{index}")
                    writer_ledger.stage_content(
                        intent_id=intent,
                        content_ref=content_ref,
                        role="candidate",
                        size_bytes=index + 1,
                        metadata={"index": index},
                    )
                    payload = {"index": index}
                    writer_ledger.commit_candidate(
                        operation_id=f"snapshot-op-{index}",
                        intent_id=intent,
                        run_id="run-s",
                        expected_run_revision=index,
                        controller_id="controller-a",
                        fencing_token=1,
                        candidate_id=f"candidate-{index}",
                        candidate_ref=candidate_ref_for(
                            candidate_format="files",
                            spec=payload,
                            content_refs=[content_ref],
                        ),
                        candidate_format="files",
                        candidate_content_refs=[content_ref],
                        logical_trial_id=f"trial-{index}",
                        handle_id=f"handle-{index}",
                        seed=index,
                        repetition_index=0,
                        payload=payload,
                    )
                    if index == 0:
                        first_committed.set()
                        if not continue_after_first.wait(timeout=2.0):
                            raise TimeoutError("reader did not observe the first committed snapshot")
                    time.sleep(0.001)
            except BaseException as exc:  # pragma: no cover - assertion reports details
                failures.append(exc)
            finally:
                finished.set()

        writer = threading.Thread(target=write_candidates)
        writer.start()
        reader = RealmLedgerSpike(path)
        self.assertTrue(first_committed.wait(timeout=2.0))
        intermediate_snapshots = 0
        try:
            snapshot = reader.snapshot("run-s")
            candidate_count = len(snapshot["candidates"])
            self.assertEqual(candidate_count, 1)
            self.assertEqual(snapshot["run"]["run_revision"], candidate_count)
            self.assertEqual(snapshot["run"]["accepted_trials"], candidate_count)
            self.assertEqual(snapshot["run"]["next_sequence"], candidate_count * 2)
            self.assertEqual(len(snapshot["events"]), candidate_count * 2)
            self.assertEqual(len(snapshot["owner_content"]), candidate_count)
            intermediate_snapshots += 1
        finally:
            continue_after_first.set()
        while not finished.is_set():
            snapshot = reader.snapshot("run-s")
            candidate_count = len(snapshot["candidates"])
            self.assertEqual(snapshot["run"]["run_revision"], candidate_count)
            self.assertEqual(snapshot["run"]["accepted_trials"], candidate_count)
            self.assertEqual(snapshot["run"]["next_sequence"], candidate_count * 2)
            self.assertEqual(len(snapshot["events"]), candidate_count * 2)
            self.assertEqual(len(snapshot["owner_content"]), candidate_count)
            if candidate_count < 20:
                intermediate_snapshots += 1
        writer.join(timeout=2.0)
        self.assertFalse(writer.is_alive())
        self.assertEqual(failures, [])
        self.assertGreaterEqual(intermediate_snapshots, 1)
        final = reader.snapshot("run-s")
        self.assertEqual(final["run"]["accepted_trials"], 20)
        self.assertEqual(len(final["events"]), 40)

    def test_snapshot_remains_precommit_when_writer_commits_mid_read(self) -> None:
        path = Path(self.temp_dir.name) / "snapshot-generation" / "realm.sqlite3"
        writer = RealmLedgerSpike(path)
        run = writer.create_run(run_id="run-g", store_id="store-a", max_trials=1)
        intent_id = writer.begin_owner_intent(
            owner_id=run["owner_id"],
            ttl_seconds=10.0,
        )
        content_ref = fake_content_ref("snapshot-generation")
        writer.stage_content(
            intent_id=intent_id,
            content_ref=content_ref,
            role="candidate",
            size_bytes=19,
            metadata={"fixture": "snapshot-generation"},
        )

        run_row_read = threading.Event()
        allow_reader_to_continue = threading.Event()

        def pause_after_run_row(step: str) -> None:
            if step == "after_snapshot_run_read":
                run_row_read.set()
                if not allow_reader_to_continue.wait(timeout=2.0):
                    raise TimeoutError("writer did not release the snapshot reader")

        reader = RealmLedgerSpike(path, fault_hook=pause_after_run_row)
        snapshots = []
        failures = []

        def read_snapshot() -> None:
            try:
                snapshots.append(reader.snapshot("run-g"))
            except BaseException as exc:  # pragma: no cover - assertion reports details
                failures.append(exc)

        thread = threading.Thread(target=read_snapshot)
        thread.start()
        self.assertTrue(run_row_read.wait(timeout=2.0))
        payload = {"generation": 1}
        try:
            writer.commit_candidate(
                operation_id="generation-commit",
                intent_id=intent_id,
                run_id="run-g",
                expected_run_revision=0,
                controller_id="controller-a",
                fencing_token=1,
                candidate_id="candidate-g",
                candidate_ref=candidate_ref_for(
                    candidate_format="files",
                    spec=payload,
                    content_refs=[content_ref],
                ),
                candidate_format="files",
                candidate_content_refs=[content_ref],
                logical_trial_id="trial-g",
                handle_id="handle-g",
                seed=1,
                repetition_index=0,
                payload=payload,
            )
        finally:
            allow_reader_to_continue.set()

        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(snapshots), 1)
        snapshot = snapshots[0]
        self.assertEqual(snapshot["run"]["run_revision"], 0)
        self.assertEqual(snapshot["run"]["accepted_trials"], 0)
        self.assertEqual(snapshot["owner_content"], [])
        self.assertEqual(snapshot["candidates"], [])
        self.assertEqual(snapshot["logical_trials"], [])
        self.assertEqual(snapshot["handles"], [])
        self.assertEqual(snapshot["events"], [])
        self.assertEqual(snapshot["operations"], [])

        final = writer.snapshot("run-g")
        self.assertEqual(final["run"]["run_revision"], 1)
        self.assertEqual(len(final["candidates"]), 1)
        self.assertEqual(len(final["owner_content"]), 1)

    def test_two_connections_compete_for_one_budget_slot_without_partial_state(self) -> None:
        constrained_path = Path(self.temp_dir.name) / "concurrent" / "realm.sqlite3"
        primary = RealmLedgerSpike(constrained_path)
        run = primary.create_run(run_id="run-c", store_id="store-a", max_trials=1, now=30.0)
        inputs = []
        for index in range(2):
            intent_id = primary.begin_owner_intent(
                owner_id=run["owner_id"],
                ttl_seconds=100.0,
                now=30.0,
            )
            content_ref = fake_content_ref(f"candidate-{index}")
            primary.stage_content(
                intent_id=intent_id,
                content_ref=content_ref,
                role="candidate",
                size_bytes=10,
                now=30.0,
            )
            inputs.append((index, intent_id, content_ref))

        write_barrier = threading.Barrier(2)

        def accept(item):
            index, intent_id, content_ref = item

            def synchronize_before_lock(step: str) -> None:
                if step == "before_write_lock":
                    write_barrier.wait(timeout=2.0)

            ledger = RealmLedgerSpike(constrained_path, fault_hook=synchronize_before_lock)
            try:
                payload = {"x": index}
                return ledger.commit_candidate(
                    operation_id=f"op-{index}",
                    intent_id=intent_id,
                    run_id="run-c",
                    expected_run_revision=0,
                    controller_id="controller-a",
                    fencing_token=1,
                    candidate_id=f"candidate-{index}",
                    candidate_ref=candidate_ref_for(
                        candidate_format="files",
                        spec=payload,
                        content_refs=[content_ref],
                    ),
                    candidate_format="files",
                    candidate_content_refs=[content_ref],
                    logical_trial_id=f"trial-{index}",
                    handle_id=f"handle-{index}",
                    seed=index,
                    repetition_index=0,
                    payload=payload,
                    now=31.0,
                )
            except LedgerConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(accept, inputs))

        self.assertEqual(sum(result is not None for result in results), 1)
        snapshot = primary.snapshot("run-c")
        self.assertEqual(snapshot["run"]["accepted_trials"], 1)
        self.assertEqual(len(snapshot["candidates"]), 1)
        self.assertEqual(len(snapshot["logical_trials"]), 1)
        self.assertEqual(len(snapshot["owner_content"]), 1)
        self.assertEqual(len(snapshot["operations"]), 1)

    def test_attempt_hard_crashes_recover_at_every_commit_boundary(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "realm_ledger_spike_worker.py"
        exit_codes = {
            "after_owner_membership": 73,
            "after_domain_records": 74,
            "before_commit": 71,
            "after_commit": 72,
        }

        for crash_step, exit_code in exit_codes.items():
            with self.subTest(crash_step=crash_step):
                path = Path(self.temp_dir.name) / f"attempt-{crash_step}" / "realm.sqlite3"
                ledger = RealmLedgerSpike(path)
                run = ledger.create_run(run_id="run-a", store_id="store-a", max_trials=1, now=10.0)
                candidate_intent = ledger.begin_owner_intent(
                    owner_id=run["owner_id"],
                    ttl_seconds=100.0,
                    now=10.0,
                )
                candidate_content_ref = fake_content_ref(f"candidate-{crash_step}")
                ledger.stage_content(
                    intent_id=candidate_intent,
                    content_ref=candidate_content_ref,
                    role="candidate",
                    size_bytes=10,
                    metadata={"crash_step": crash_step},
                    now=10.0,
                )
                candidate_payload = {"case": crash_step}
                ledger.commit_candidate(
                    operation_id="candidate-accept",
                    intent_id=candidate_intent,
                    run_id="run-a",
                    expected_run_revision=0,
                    controller_id="controller-a",
                    fencing_token=1,
                    candidate_id="candidate-a",
                    candidate_ref=candidate_ref_for(
                        candidate_format="files",
                        spec=candidate_payload,
                        content_refs=[candidate_content_ref],
                    ),
                    candidate_format="files",
                    candidate_content_refs=[candidate_content_ref],
                    logical_trial_id="trial-a",
                    handle_id="handle-a",
                    seed=7,
                    repetition_index=0,
                    payload=candidate_payload,
                    now=11.0,
                )
                artifact_intent = ledger.begin_owner_intent(
                    owner_id=run["owner_id"],
                    ttl_seconds=100.0,
                    now=12.0,
                )
                artifact_ref = fake_content_ref(f"artifact-{crash_step}")
                ledger.stage_content(
                    intent_id=artifact_intent,
                    content_ref=artifact_ref,
                    role="artifact",
                    size_bytes=12,
                    metadata={"crash_step": crash_step},
                    now=12.0,
                )
                process = subprocess.run(
                    [
                        sys.executable,
                        str(fixture),
                        "--action",
                        "attempt",
                        "--database",
                        str(path),
                        "--intent",
                        artifact_intent,
                        "--operation",
                        "attempt-terminal",
                        "--trial-id",
                        "trial-a",
                        "--attempt-id",
                        "attempt-a",
                        "--artifact-ref",
                        artifact_ref,
                        "--expected-revision",
                        "1",
                        "--crash",
                        crash_step,
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    check=False,
                )
                self.assertEqual(process.returncode, exit_code)

                reopened = RealmLedgerSpike(path)
                arguments = {
                    "operation_id": "attempt-terminal",
                    "intent_id": artifact_intent,
                    "run_id": "run-a",
                    "expected_run_revision": 1,
                    "controller_id": "controller-a",
                    "fencing_token": 1,
                    "logical_trial_id": "trial-a",
                    "attempt_id": "attempt-a",
                    "attempt_index": 1,
                    "outcome": "success",
                    "observation": {"metric_values": {"score": 0.9}},
                    "artifacts": [
                        {
                            "artifact_id": "artifact-crash",
                            "content_ref": artifact_ref,
                            "role": "artifact",
                        }
                    ],
                    "payload": {"runtime_seconds": 1.2},
                    "now": 13.0,
                }
                before_replay = reopened.snapshot("run-a")
                if crash_step == "after_commit":
                    self.assertEqual(len(before_replay["attempts"]), 1)
                    self.assertEqual(before_replay["handles"][0]["state"], "terminal")
                else:
                    self.assertEqual(before_replay["attempts"], [])
                    self.assertEqual(before_replay["observations"], [])
                    self.assertEqual(before_replay["artifacts"], [])
                    self.assertEqual(before_replay["handles"][0]["state"], "accepted")
                    self.assertNotIn(
                        artifact_ref,
                        [item["content_ref"] for item in before_replay["owner_content"]],
                    )

                first_receipt = reopened.commit_attempt(**arguments)
                replay_receipt = reopened.commit_attempt(**arguments)
                self.assertEqual(replay_receipt, first_receipt)
                final = reopened.snapshot("run-a")
                self.assertEqual(final["run"]["run_revision"], 2)
                self.assertEqual(len(final["attempts"]), 1)
                self.assertEqual(len(final["observations"]), 1)
                self.assertEqual(len(final["artifacts"]), 1)
                self.assertEqual(final["handles"][0]["state"], "terminal")
                self.assertEqual(len(final["operations"]), 2)
                self.assertEqual(reopened.integrity_check()["integrity"], ["ok"])

    def test_hard_crash_before_and_after_commit_recovers_exactly_once(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "realm_ledger_spike_worker.py"

        before_intent, before_content_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="before-crash-tree",
            role="candidate",
        )
        before_ref = candidate_ref_for(
            candidate_format="files",
            spec={"x": 1},
            content_refs=[before_content_ref],
        )
        before = subprocess.run(
            [
                sys.executable,
                str(fixture),
                "--database",
                str(self.database_path),
                "--intent",
                before_intent,
                "--operation",
                "hard-before",
                "--candidate-ref",
                before_ref,
                "--candidate-content-ref",
                before_content_ref,
                "--candidate-id",
                "candidate-before",
                "--trial-id",
                "trial-before",
                "--handle-id",
                "handle-before",
                "--expected-revision",
                "0",
                "--crash",
                "before_commit",
            ],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
        )
        self.assertEqual(before.returncode, 71)
        self.assertEqual(self.ledger.snapshot("run-a")["candidates"], [])

        recovered = self._commit_candidate(
            operation_id="hard-before",
            intent_id=before_intent,
            candidate_content_refs=[before_content_ref],
            candidate_ref=before_ref,
            candidate_id="candidate-before",
            trial_id="trial-before",
            handle_id="handle-before",
        )
        self.assertEqual(recovered.run_revision, 1)

        after_intent, after_content_ref = self._stage(
            owner_id=self.run["owner_id"],
            label="after-crash-tree",
            role="candidate",
        )
        after_ref = candidate_ref_for(
            candidate_format="files",
            spec={"x": 1},
            content_refs=[after_content_ref],
        )
        after = subprocess.run(
            [
                sys.executable,
                str(fixture),
                "--database",
                str(self.database_path),
                "--intent",
                after_intent,
                "--operation",
                "hard-after",
                "--candidate-ref",
                after_ref,
                "--candidate-content-ref",
                after_content_ref,
                "--candidate-id",
                "candidate-after",
                "--trial-id",
                "trial-after",
                "--handle-id",
                "handle-after",
                "--expected-revision",
                "1",
                "--crash",
                "after_commit",
            ],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
        )
        self.assertEqual(after.returncode, 72)

        replay = self._commit_candidate(
            operation_id="hard-after",
            intent_id=after_intent,
            candidate_content_refs=[after_content_ref],
            candidate_ref=after_ref,
            expected_run_revision=1,
            candidate_id="candidate-after",
            trial_id="trial-after",
            handle_id="handle-after",
        )
        self.assertEqual(replay.run_revision, 2)
        snapshot = self.ledger.snapshot("run-a")
        self.assertEqual(len(snapshot["candidates"]), 2)
        self.assertEqual(len(snapshot["logical_trials"]), 2)
        self.assertEqual(len(snapshot["owner_content"]), 2)
        self.assertEqual(len(snapshot["operations"]), 2)


if __name__ == "__main__":
    unittest.main()
