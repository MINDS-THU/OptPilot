from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from optpilot.realm.content import LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm_retained_batch_run_driver import RealmRetainedBatchRunDriver
from optpilot.retained_batch_runtime import RetainedBatchWorkerStatus
from optpilot.retained_batch_worker import INITIAL_BATCH_EXCHANGE_CHAIN
from optpilot.run_authority import RetainedRunAuthority
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


def _normalizer(candidate: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(candidate)
    result.setdefault("format", "parameters")
    result.setdefault("spec", {})
    result.setdefault("lineage", {"parents": []})
    result.setdefault(
        "generator", {"method_id": "test-method", "strategy": "external"}
    )
    result.setdefault("validation", {})
    result.setdefault("materialization", {})
    return result


def _candidate() -> dict[str, object]:
    return {
        "candidate_id": "candidate-a",
        "format": "parameters",
        "spec": {"x": 1},
    }


@dataclass(frozen=True)
class _Retirement:
    run_id: str
    controller_generation: int
    run_definition_digest: str
    worker_disposition: str = "stopped"
    resources_reconciled: bool = True


class _MethodProvider:
    def __init__(self, worker: "_IdleWorker") -> None:
        self.worker = worker
        self.realized_generations: list[int] = []
        self.retired_generations: list[int] = []

    def realize(self, snapshot, **_kwargs):
        self.realized_generations.append(snapshot.run.controller_generation)
        return self.worker

    def reconcile_inactive(self, snapshot, **_kwargs):
        self.retired_generations.append(snapshot.run.controller_generation)
        return _Retirement(
            run_id=snapshot.run.run_id,
            controller_generation=snapshot.run.controller_generation,
            run_definition_digest=snapshot.definition.digest,
        )


class _IdleWorker:
    def __init__(self) -> None:
        self.request_calls = 0
        self.force_stop_called = False
        self.shutdown_called = False

    def status(self, request_exchange_id: str) -> RetainedBatchWorkerStatus:
        return RetainedBatchWorkerStatus(
            request_exchange_id=request_exchange_id,
            acknowledged_sequence=0,
            acknowledged_chain=INITIAL_BATCH_EXCHANGE_CHAIN,
            pending_exchange=None,
            pending_response_bytes=0,
            response_digest="1" * 64,
        )

    def request(self, *_args, **_kwargs):
        self.request_calls += 1
        raise AssertionError("durable cancellation must precede method work")

    def ack(self, *_args, **_kwargs):
        raise AssertionError("an idle cancelled worker has nothing to acknowledge")

    def force_stop(self) -> None:
        self.force_stop_called = True

    def shutdown(self) -> None:
        self.shutdown_called = True


class _Heartbeat:
    def __init__(self) -> None:
        self.live = False

    def start(self) -> None:
        self.live = True

    def stop(self) -> None:
        self.live = False

    def raise_if_failed(self) -> None:
        if not self.live:
            raise RuntimeError("heartbeat is inactive")


class _Scheduler:
    def __init__(self, authority: RetainedRunAuthority) -> None:
        self.authority = authority
        self.advance_calls: list[tuple[str, str]] = []

    def advance(
        self,
        *,
        logical_trial_id: str,
        attempt_id: str,
        attempt_ttl_seconds: float,
    ) -> None:
        del attempt_ttl_seconds
        self.advance_calls.append((logical_trial_id, attempt_id))
        raise AssertionError("durable cancellation must precede evaluator dispatch")

    def terminalize(self, *, logical_trial_id: str, attempt_id: str) -> None:
        del logical_trial_id, attempt_id
        raise AssertionError("the cancellation tests create no live attempt")


class _Runtime:
    def __init__(self, ledger: RealmLedger) -> None:
        self.actor_principal_id = "operator"
        self.ledger = ledger
        self.attempt_provider = object()


class RealmRetainedBatchDriverCancellationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="driver-cancel/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="driver-cancel/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, bindings, source_owner_id, source_revision = (
            prepare_test_run_closure(
                ledger=self.ledger,
                store=self.store,
                root=self.root,
                actor_principal_id="operator",
                prefix="driver-cancel",
            )
        )
        manifest = replace(
            prepare_test_run_control_manifest(closure, max_trials=1),
            proposal_width=1,
        )
        definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        created = self.ledger.create_run_namespace(
            operation_id="driver-cancel/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="run-cancel-a",
            owner_id="run-cancel-owner-a",
        )
        self.authority = RetainedRunAuthority.from_create_receipt(
            ledger=self.ledger,
            actor_principal_id="operator",
            receipt=created,
            candidate_normalizer=_normalizer,
            normalizer_version=manifest.normalizer_version,
        )
        self.runtime = _Runtime(self.ledger)

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    @staticmethod
    def cancellation(digest: str = "a" * 64):
        return SimpleNamespace(
            reason_code="user_cancelled",
            request_digest=digest,
            created_txn_id=42,
        )

    def driver(self):
        worker = _IdleWorker()
        provider = _MethodProvider(worker)
        scheduler = _Scheduler(self.authority)
        driver = RealmRetainedBatchRunDriver(
            self.runtime,
            self.authority,
            method_runtime_provider=provider,
            scheduler=scheduler,
            heartbeat_factory=lambda _snapshot, _method: _Heartbeat(),
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
        )
        return driver, provider, scheduler, worker

    def terminal_snapshot(self):
        return self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-cancel-a"
        )

    def test_entry_cancellation_closes_empty_run_before_method_realization(self) -> None:
        driver, provider, scheduler, worker = self.driver()

        with mock.patch.object(
            RealmLedger,
            "read_run_cancellation_request",
            create=True,
            return_value=self.cancellation(),
        ):
            summary = driver.run()

        snapshot = self.terminal_snapshot()
        self.assertEqual(summary.run_status, "cancelled")
        self.assertEqual(snapshot.run.state, "cancelled")
        self.assertEqual(snapshot.control.current_submission.stop_code, "user_cancelled")
        self.assertEqual(provider.realized_generations, [])
        self.assertEqual(scheduler.advance_calls, [])
        self.assertEqual(worker.request_calls, 0)

    def test_active_drive_boundary_applies_request_before_proposal(self) -> None:
        driver, provider, scheduler, worker = self.driver()
        calls = 0

        def read_request(**_kwargs):
            nonlocal calls
            calls += 1
            return None if calls == 1 else self.cancellation("b" * 64)

        with mock.patch.object(
            RealmLedger,
            "read_run_cancellation_request",
            create=True,
            side_effect=read_request,
        ):
            summary = driver.run()

        self.assertEqual(summary.run_status, "cancelled")
        self.assertEqual(provider.realized_generations, [1])
        self.assertTrue(worker.force_stop_called)
        self.assertFalse(worker.shutdown_called)
        self.assertEqual(worker.request_calls, 0)
        self.assertEqual(scheduler.advance_calls, [])

    def test_methodless_drive_escalates_soft_drain_before_attempt_dispatch(self) -> None:
        accepted = self.authority.admit(
            (_candidate(),), admission_id="seeded-plan"
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(
            self.authority.refresh_controller().control.current_submission.stop_code,
            "max_trials",
        )
        driver, provider, scheduler, worker = self.driver()
        calls = 0

        def read_request(**_kwargs):
            nonlocal calls
            calls += 1
            return None if calls == 1 else self.cancellation("c" * 64)

        with mock.patch.object(
            RealmLedger,
            "read_run_cancellation_request",
            create=True,
            side_effect=read_request,
        ):
            summary = driver.run()

        snapshot = self.terminal_snapshot()
        self.assertEqual(summary.run_status, "cancelled")
        self.assertEqual(
            [
                (record.state, record.stop_code)
                for record in snapshot.control.submission_records
            ],
            [
                ("accepting", None),
                ("draining", "max_trials"),
                ("draining", "user_cancelled"),
                ("terminal", "user_cancelled"),
            ],
        )
        self.assertEqual(provider.realized_generations, [])
        self.assertEqual(scheduler.advance_calls, [])
        self.assertEqual(worker.request_calls, 0)


if __name__ == "__main__":
    unittest.main()
