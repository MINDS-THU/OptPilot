from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock

import optpilot.realm_retained_batch_run_driver as retained_batch_run_driver
from optpilot.attempts import AttemptEnvelope, AttemptFinalization
from optpilot.realm.content import LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.method_exchange_records import method_exchange_sequence
from optpilot.realm.refs import canonical_json_bytes
from optpilot.realm.run_projection import RunSummaryProjection
from optpilot.method_exchange_projection import (
    build_method_observation_exchange_input,
    build_method_proposal_exchange_input,
    proposal_worker_payload,
)
from optpilot.realm_retained_batch_run_driver import (
    DigestRealmAttemptIdentitySource,
    RealmRetainedBatchRunDriver,
    RealmRetainedBatchRunError,
    RunControllerTakeoverExpectation,
)
from optpilot.retained_batch_runtime import (
    RetainedBatchCacheAck,
    RetainedBatchExchangeCoordinate,
    RetainedBatchMethodError,
    RetainedBatchRuntimeError,
    RetainedBatchWorkerResponse,
    RetainedBatchWorkerStatus,
    retained_batch_worker_request_digest,
)
from optpilot.retained_batch_worker import (
    BATCH_RESPONSE_SCHEMA,
    INITIAL_BATCH_EXCHANGE_CHAIN,
    retained_batch_exchange_chain_digest,
)
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


def _candidate(candidate_id: str, value: int) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "format": "parameters",
        "spec": {"x": value},
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class _RuntimeGraph:
    def __init__(self, ledger: RealmLedger) -> None:
        self.actor_principal_id = "operator"
        self.ledger = ledger
        self.attempt_provider = _UnusedAttemptProvider(ledger)


class _UnusedAttemptProvider:
    def __init__(self, ledger: RealmLedger) -> None:
        self._ledger = ledger
        self._actor_principal_id = "operator"

    def start_or_attach(self, **_kwargs):
        raise AssertionError("takeover construction must not launch an attempt")

    def wait_terminal(self, **_kwargs):
        raise AssertionError("takeover construction must not wait for an attempt")

    def stop_and_wait_terminal(self, **_kwargs):
        raise AssertionError("takeover construction must not stop an attempt")

    def terminalize_current(self, **_kwargs):
        raise AssertionError("takeover construction must not terminalize an attempt")

    def finalize_terminal(self, **_kwargs):
        raise AssertionError("takeover construction must not finalize an attempt")

    def resume_cleanup(self, **_kwargs):
        raise AssertionError("takeover construction must not clean an attempt")


class _NoopHeartbeat:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def raise_if_failed(self) -> None:
        if not self.started or self.stopped:
            raise RuntimeError("heartbeat is not live")


class _CancellingScheduler:
    def __init__(self, authority: RetainedRunAuthority) -> None:
        self.authority = authority
        self.calls: list[tuple[str, str]] = []
        self.terminalize_calls: list[tuple[str, str]] = []

    def advance(
        self,
        *,
        logical_trial_id: str,
        attempt_id: str,
        attempt_ttl_seconds: float,
    ) -> None:
        self.calls.append((logical_trial_id, attempt_id))
        snapshot = self.authority.refresh_controller()
        self.authority.ledger.cancel_run_logical_trial(
            operation_id=f"driver-test/cancel/{attempt_id}",
            actor_principal_id=self.authority.actor_principal_id,
            run_id=self.authority.run_id,
            logical_trial_id=logical_trial_id,
            expected_run_revision=snapshot.revision.revision,
            controller_lease_id=self.authority.controller_lease_id,
            controller_holder_id=self.authority.controller_holder_id,
            controller_fencing_token=self.authority.controller_fencing_token,
            code="user_cancelled",
        )

    def terminalize(self, *, logical_trial_id: str, attempt_id: str) -> None:
        self.terminalize_calls.append((logical_trial_id, attempt_id))
        raise AssertionError("driver test scheduler has no active provider attempt")


class _SuccessfulScheduler:
    """Adopt one synthetic successful evaluation and acknowledge cleanup."""

    def __init__(self, authority: RetainedRunAuthority) -> None:
        self.authority = authority
        self.calls: list[tuple[str, str]] = []
        self.cleanup_calls: list[tuple[str, str]] = []

    def advance(
        self,
        *,
        logical_trial_id: str,
        attempt_id: str,
        attempt_ttl_seconds: float,
    ) -> None:
        self.calls.append((logical_trial_id, attempt_id))
        snapshot = self.authority.refresh_controller()
        existing = next(
            (
                item
                for item in snapshot.attempts
                if item.attempt_id == attempt_id
            ),
            None,
        )
        if existing is not None:
            if existing.state != "terminal":
                raise AssertionError("synthetic attempt is unexpectedly active")
            self.cleanup_calls.append((logical_trial_id, attempt_id))
            return

        prepared = self.authority.ledger.prepare_run_attempt(
            operation_id=f"driver-test/success/prepare/{attempt_id}",
            actor_principal_id=self.authority.actor_principal_id,
            run_id=self.authority.run_id,
            logical_trial_id=logical_trial_id,
            attempt_id=attempt_id,
            expected_run_revision=snapshot.revision.revision,
            controller_lease_id=self.authority.controller_lease_id,
            controller_holder_id=self.authority.controller_holder_id,
            controller_fencing_token=self.authority.controller_fencing_token,
            attempt_ttl_seconds=attempt_ttl_seconds,
        )
        envelope = AttemptEnvelope(
            attempt_id=attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            outcome="success",
            phase="environment_evaluation",
            wall_clock_seconds=0.1,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {"x": 1}, "metadata": {}},
            metric_values={"score": 65.0754},
            constraint_results={},
            output_declarations=(),
            event_summary={"primary_metric": "score"},
            execution_metadata={"worker": "synthetic"},
            error={},
        )
        self.authority.ledger.adopt_run_attempt(
            operation_id=f"driver-test/success/adopt/{attempt_id}",
            actor_principal_id=self.authority.actor_principal_id,
            run_id=self.authority.run_id,
            attempt_id=attempt_id,
            change_id=prepared.attempt.capture_change_id,
            finalization=AttemptFinalization(
                attempt_id=attempt_id,
                evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                binding_id=prepared.attempt.binding_id,
                effective_outcome="success",
                effective_code=None,
                captured_artifacts=(),
                envelope=envelope,
            ),
            expected_run_revision=prepared.revision.revision,
            expected_owner_revision=prepared.revision.owner_revision,
            controller_lease_id=self.authority.controller_lease_id,
            controller_holder_id=self.authority.controller_holder_id,
            controller_fencing_token=self.authority.controller_fencing_token,
        )
        self.authority.refresh_controller()

    def terminalize(self, *, logical_trial_id: str, attempt_id: str) -> None:
        del logical_trial_id, attempt_id
        raise AssertionError("successful scheduler has no active attempt")


class _GatedCancellingScheduler:
    """Expose evaluator overlap while serializing the fake canonical seam."""

    def __init__(
        self,
        authority: RetainedRunAuthority,
        candidate_ids: Sequence[str],
        *,
        auto_release: bool = False,
        fail_candidates: Sequence[str] = (),
    ) -> None:
        self.authority = authority
        self.release = {value: threading.Event() for value in candidate_ids}
        self.entered = {value: threading.Event() for value in candidate_ids}
        self.completed = {value: threading.Event() for value in candidate_ids}
        if auto_release:
            for event in self.release.values():
                event.set()
        self.fail_candidates = frozenset(fail_candidates)
        self.started_candidates: list[str] = []
        self.completed_candidates: list[str] = []
        self.active = 0
        self.max_active = 0
        self._state_lock = threading.Lock()
        self._canonical_lock = threading.Lock()

    def advance(
        self,
        *,
        logical_trial_id: str,
        attempt_id: str,
        attempt_ttl_seconds: float,
    ) -> None:
        del attempt_id, attempt_ttl_seconds
        snapshot = self.authority.ledger.read_run_snapshot(
            actor_principal_id=self.authority.actor_principal_id,
            run_id=self.authority.run_id,
        )
        trial = next(
            item
            for item in snapshot.logical_trials
            if item.admission.logical_trial_id == logical_trial_id
        )
        candidate_id = trial.admission.candidate_id
        with self._state_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started_candidates.append(candidate_id)
            self.entered[candidate_id].set()
        try:
            if not self.release[candidate_id].wait(timeout=10):
                raise RuntimeError("test evaluator gate timed out")
            if candidate_id in self.fail_candidates:
                raise RuntimeError(f"simulated interruption for {candidate_id}")
            with self._canonical_lock:
                snapshot = self.authority.refresh_controller()
                current = next(
                    item
                    for item in snapshot.logical_trials
                    if item.admission.logical_trial_id == logical_trial_id
                )
                if current.state != "terminal":
                    self.authority.ledger.cancel_run_logical_trial(
                        operation_id=f"driver-test/gated-cancel/{logical_trial_id}",
                        actor_principal_id=self.authority.actor_principal_id,
                        run_id=self.authority.run_id,
                        logical_trial_id=logical_trial_id,
                        expected_run_revision=snapshot.revision.revision,
                        controller_lease_id=self.authority.controller_lease_id,
                        controller_holder_id=self.authority.controller_holder_id,
                        controller_fencing_token=(
                            self.authority.controller_fencing_token
                        ),
                        code="user_cancelled",
                    )
        finally:
            with self._state_lock:
                self.active -= 1
                self.completed_candidates.append(candidate_id)
                self.completed[candidate_id].set()

    def terminalize(self, *, logical_trial_id: str, attempt_id: str) -> None:
        del logical_trial_id, attempt_id
        raise AssertionError("gated scheduler has no provider attempt to terminalize")

    def release_all(self) -> None:
        for event in self.release.values():
            event.set()


class _DispatchGateScheduler:
    """Measure physical dispatch width without mutating canonical run state."""

    def __init__(
        self,
        authority: RetainedRunAuthority,
        *,
        saturation_width: int,
    ) -> None:
        self.authority = authority
        self.saturation_width = saturation_width
        self.release = threading.Event()
        self.saturated = threading.Event()
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started = 0

    def advance(
        self,
        *,
        logical_trial_id: str,
        attempt_id: str,
        attempt_ttl_seconds: float,
    ) -> None:
        del logical_trial_id, attempt_id, attempt_ttl_seconds
        with self._lock:
            self.active += 1
            self.started += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == self.saturation_width:
                self.saturated.set()
        try:
            if not self.release.wait(timeout=10):
                raise RuntimeError("test dispatch gate timed out")
        finally:
            with self._lock:
                self.active -= 1

    def terminalize(self, *, logical_trial_id: str, attempt_id: str) -> None:
        del logical_trial_id, attempt_id
        raise AssertionError("dispatch gate has no provider attempt to terminalize")


class _FakeRetainedWorker:
    def __init__(
        self,
        ledger: RealmLedger,
        *,
        run_id: str,
        candidate: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
        fail_ack_sequence: int | None = None,
        proposal_error_code: str | None = None,
        observation_error_code: str | None = None,
        shutdown_error_once: bool = False,
        force_stop_error: bool = False,
        corrupt_ack_chain_sequence: int | None = None,
        runtime_error_sequence: int | None = None,
    ) -> None:
        self.ledger = ledger
        self.run_id = run_id
        if candidate is None:
            self.candidates: list[dict[str, Any]] = []
        elif isinstance(candidate, Mapping):
            self.candidates = [copy.deepcopy(dict(candidate))]
        else:
            self.candidates = [copy.deepcopy(dict(item)) for item in candidate]
        self.fail_ack_sequence = fail_ack_sequence
        self.proposal_error_code = proposal_error_code
        self.observation_error_code = observation_error_code
        self.shutdown_error_once = shutdown_error_once
        self.force_stop_error = force_stop_error
        self.corrupt_ack_chain_sequence = corrupt_ack_chain_sequence
        self.runtime_error_sequence = runtime_error_sequence
        self.acknowledged_sequence = 0
        self.acknowledged_chain = INITIAL_BATCH_EXCHANGE_CHAIN
        self.pending: RetainedBatchExchangeCoordinate | None = None
        self.pending_response: RetainedBatchWorkerResponse | None = None
        self.pending_error: RetainedBatchMethodError | None = None
        self.callback_calls: list[tuple[str, str, int]] = []
        self.callback_payloads: list[tuple[str, dict[str, Any]]] = []
        self.observe_entered = threading.Event()
        self.ack_calls: list[int] = []
        self.shutdown_called = False
        self.force_stop_called = False

    def request(
        self,
        exchange_id: str,
        operation: str,
        payload: Mapping[str, Any],
        *,
        exchange_sequence: int,
    ) -> RetainedBatchWorkerResponse:
        request_digest = retained_batch_worker_request_digest(operation, payload)
        if self.pending is not None:
            if (
                self.pending.exchange_id != exchange_id
                or self.pending.exchange_sequence != exchange_sequence
                or self.pending.request_digest != request_digest
            ):
                raise AssertionError("driver attempted a different pending exchange")
            if self.pending_error is not None:
                raise self.pending_error
            assert self.pending_response is not None
            return self.pending_response
        if exchange_sequence != self.acknowledged_sequence + 1:
            raise AssertionError("driver skipped the worker exchange order")

        self.callback_calls.append((operation, exchange_id, exchange_sequence))
        self.callback_payloads.append((operation, copy.deepcopy(dict(payload))))
        if self.runtime_error_sequence == exchange_sequence:
            raise RetainedBatchRuntimeError("worker_request_timeout")
        error_code = (
            self.proposal_error_code
            if operation == "propose"
            else self.observation_error_code
        )
        if error_code is not None:
            body = {
                "exchange_id": exchange_id,
                "error": {
                    "code": error_code,
                    "diagnostic_id": None,
                    "message": "The retained method operation failed.",
                },
                "ok": False,
                "schema": BATCH_RESPONSE_SCHEMA,
            }
            error = RetainedBatchMethodError(
                code=error_code,
                message=body["error"]["message"],
                diagnostic_id=None,
                response_digest=_digest(body),
            )
            self.pending = RetainedBatchExchangeCoordinate(
                exchange_id=exchange_id,
                exchange_sequence=exchange_sequence,
                request_digest=request_digest,
                response_digest=error.response_digest,
            )
            self.pending_error = error
            raise error
        if operation == "propose":
            result: Mapping[str, Any] = {
                "candidates": copy.deepcopy(self.candidates)
            }
        elif operation == "observe":
            self.observe_entered.set()
            result = {"observation_count": len(payload["observations"])}
        else:
            raise AssertionError("unexpected state-changing operation")
        body = {
            "exchange_id": exchange_id,
            "ok": True,
            "result": result,
            "schema": BATCH_RESPONSE_SCHEMA,
        }
        response = RetainedBatchWorkerResponse(body, _digest(body))
        self.pending = RetainedBatchExchangeCoordinate(
            exchange_id=exchange_id,
            exchange_sequence=exchange_sequence,
            request_digest=request_digest,
            response_digest=response.response_digest,
        )
        self.pending_response = response
        return response

    def status(self, request_exchange_id: str) -> RetainedBatchWorkerStatus:
        body = {
            "acknowledged_chain": self.acknowledged_chain,
            "acknowledged_sequence": self.acknowledged_sequence,
            "pending": None if self.pending is None else self.pending.to_dict(),
        }
        return RetainedBatchWorkerStatus(
            request_exchange_id=request_exchange_id,
            acknowledged_sequence=self.acknowledged_sequence,
            acknowledged_chain=self.acknowledged_chain,
            pending_exchange=self.pending,
            pending_response_bytes=(0 if self.pending is None else 128),
            response_digest=_digest(body),
        )

    def ack(
        self,
        request_exchange_id: str,
        *,
        exchange: RetainedBatchExchangeCoordinate,
        previous_acknowledged_chain: str,
    ) -> RetainedBatchCacheAck:
        # The test worker makes the production ordering observable: ACK is
        # rejected until Realm contains this exact completed exchange.
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.run_id
        )
        completion = next(
            (
                item
                for item in snapshot.method_exchange_completions
                if item.exchange_id == exchange.exchange_id
            ),
            None,
        )
        if completion is None or completion.response_digest != exchange.response_digest:
            raise AssertionError("worker ACK happened before canonical completion")
        if self.pending != exchange or previous_acknowledged_chain != self.acknowledged_chain:
            raise AssertionError("worker ACK differs from pending state")
        if self.fail_ack_sequence == exchange.exchange_sequence:
            self.fail_ack_sequence = None
            raise RuntimeError("simulated crash before worker ACK")

        self.acknowledged_chain = retained_batch_exchange_chain_digest(
            self.acknowledged_chain,
            exchange_id=exchange.exchange_id,
            exchange_sequence=exchange.exchange_sequence,
            request_digest_value=exchange.request_digest,
            response_digest=exchange.response_digest,
        )
        self.acknowledged_sequence = exchange.exchange_sequence
        self.pending = None
        self.pending_response = None
        self.pending_error = None
        self.ack_calls.append(exchange.exchange_sequence)
        return RetainedBatchCacheAck(
            request_exchange_id=request_exchange_id,
            acknowledged_sequence=self.acknowledged_sequence,
            acknowledged_chain=(
                "0" * 64
                if self.corrupt_ack_chain_sequence == exchange.exchange_sequence
                else self.acknowledged_chain
            ),
            acknowledged_exchange=exchange,
            response_digest=_digest(
                {"acknowledged_sequence": self.acknowledged_sequence}
            ),
        )

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> object:
        return object()

    def shutdown(self) -> None:
        self.shutdown_called = True
        if self.shutdown_error_once:
            self.shutdown_error_once = False
            raise RuntimeError("simulated shutdown failure")

    def force_stop(self) -> None:
        self.force_stop_called = True
        if self.force_stop_error:
            raise RuntimeError("simulated force-stop failure")


@dataclass(frozen=True)
class _FakeTerminalRetirement:
    run_id: str
    controller_generation: int
    run_definition_digest: str
    worker_disposition: str
    resources_reconciled: bool


class _FakeMethodProvider:
    def __init__(
        self,
        worker: _FakeRetainedWorker,
        *,
        resources_reconciled: bool = True,
    ) -> None:
        self.worker = worker
        self.generations: list[int] = []
        self.retirement_calls: list[int] = []
        self.resources_reconciled = resources_reconciled

    def realize(self, snapshot, **_kwargs):
        self.generations.append(snapshot.run.controller_generation)
        return self.worker

    def reconcile_inactive(self, snapshot, **_kwargs):
        self.retirement_calls.append(snapshot.run.controller_generation)
        return _FakeTerminalRetirement(
            run_id=snapshot.run.run_id,
            controller_generation=snapshot.run.controller_generation,
            run_definition_digest=snapshot.definition.digest,
            worker_disposition="stopped",
            resources_reconciled=self.resources_reconciled,
        )


class RealmRetainedBatchRunDriverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="driver/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="driver/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, bindings, source_owner_id, source_revision = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="driver",
        )
        self.closure = closure
        self.closure_bindings = bindings
        self.source_owner_id = source_owner_id
        self.source_revision = source_revision
        manifest = replace(
            prepare_test_run_control_manifest(closure, max_trials=1),
            proposal_width=1,
        )
        definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="driver/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        self.normalizer_version = manifest.normalizer_version
        self.runtime = _RuntimeGraph(self.ledger)

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def authority_from_create(self) -> RetainedRunAuthority:
        return RetainedRunAuthority.from_create_receipt(
            ledger=self.ledger,
            actor_principal_id="operator",
            receipt=self.created,
            candidate_normalizer=_normalizer,
            normalizer_version=self.normalizer_version,
        )

    def parallel_authority(
        self,
        *,
        run_id: str = "run-parallel",
        candidate_count: int = 3,
        evaluator_capacity: int = 2,
    ) -> RetainedRunAuthority:
        manifest = replace(
            prepare_test_run_control_manifest(
                self.closure, max_trials=candidate_count
            ),
            proposal_width=candidate_count,
        )
        definition, definition_bindings = prepare_test_run_definition(
            self.closure,
            manifest,
            self.closure_bindings,
            evaluator_capacity=evaluator_capacity,
        )
        receipt = self.ledger.create_run_namespace(
            operation_id=f"driver/{run_id}/create",
            actor_principal_id="operator",
            controller_holder_id=f"controller-{run_id}",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_revision,
            run_id=run_id,
            owner_id=f"owner-{run_id}",
        )
        return RetainedRunAuthority.from_create_receipt(
            ledger=self.ledger,
            actor_principal_id="operator",
            receipt=receipt,
            candidate_normalizer=_normalizer,
            normalizer_version=manifest.normalizer_version,
        )

    def serial_authority(
        self,
        *,
        run_id: str,
        max_trials: int,
    ) -> RetainedRunAuthority:
        manifest = replace(
            prepare_test_run_control_manifest(
                self.closure, max_trials=max_trials
            ),
            proposal_width=1,
        )
        definition, definition_bindings = prepare_test_run_definition(
            self.closure,
            manifest,
            self.closure_bindings,
        )
        receipt = self.ledger.create_run_namespace(
            operation_id=f"driver/{run_id}/create",
            actor_principal_id="operator",
            controller_holder_id=f"controller-{run_id}",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_revision,
            run_id=run_id,
            owner_id=f"owner-{run_id}",
        )
        return RetainedRunAuthority.from_create_receipt(
            ledger=self.ledger,
            actor_principal_id="operator",
            receipt=receipt,
            candidate_normalizer=_normalizer,
            normalizer_version=manifest.normalizer_version,
        )

    def replace_controller(
        self, *, run_id: str, holder: str
    ) -> RetainedRunAuthority:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=run_id
        )
        self.ledger.replace_run_controller(
            operation_id=f"driver/{run_id}/takeover/{holder}",
            actor_principal_id="operator",
            run_id=run_id,
            expected_controller_generation=snapshot.run.controller_generation,
            expected_controller_lease_id=snapshot.run.controller_lease_id,
            expected_controller_holder_id=snapshot.run.controller_holder_id,
            expected_controller_fencing_token=(
                snapshot.run.controller_fencing_token
            ),
            new_controller_holder_id=holder,
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        return RetainedRunAuthority.hydrate(
            ledger=self.ledger,
            actor_principal_id="operator",
            run_id=run_id,
            candidate_normalizer=_normalizer,
            normalizer_version=self.normalizer_version,
        )

    def take_over(self, *, holder: str) -> RetainedRunAuthority:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.ledger.replace_run_controller(
            operation_id=f"driver/takeover/{holder}",
            actor_principal_id="operator",
            run_id="run-a",
            expected_controller_generation=snapshot.run.controller_generation,
            expected_controller_lease_id=snapshot.run.controller_lease_id,
            expected_controller_holder_id=snapshot.run.controller_holder_id,
            expected_controller_fencing_token=snapshot.run.controller_fencing_token,
            new_controller_holder_id=holder,
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        return RetainedRunAuthority.hydrate(
            ledger=self.ledger,
            actor_principal_id="operator",
            run_id="run-a",
            candidate_normalizer=_normalizer,
            normalizer_version=self.normalizer_version,
        )

    def driver(
        self,
        authority: RetainedRunAuthority,
        worker: _FakeRetainedWorker,
    ) -> tuple[RealmRetainedBatchRunDriver, _CancellingScheduler]:
        scheduler = _CancellingScheduler(authority)
        driver = RealmRetainedBatchRunDriver(
            self.runtime,
            authority,
            method_runtime_provider=_FakeMethodProvider(worker),
            scheduler=scheduler,
            heartbeat_factory=lambda _snapshot, _method: _NoopHeartbeat(),
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
        )
        return driver, scheduler

    def test_complete_run_commits_before_ack_and_returns_path_free_summary(self) -> None:
        authority = self.authority_from_create()
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
        )
        driver, scheduler = self.driver(authority, worker)

        summary = driver.run()
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

        self.assertIsInstance(summary, RunSummaryProjection)
        self.assertNotEqual(summary.run_status, "running")
        self.assertEqual(
            [(item.kind, item.outcome) for item in snapshot.method_exchange_completions],
            [("proposal", "admitted"), ("observation", "acknowledged")],
        )
        self.assertEqual(worker.ack_calls, [1, 2])
        self.assertEqual(
            [item[0] for item in worker.callback_calls], ["propose", "observe"]
        )
        expected_attempt_id = DigestRealmAttemptIdentitySource().attempt(
            run_id="run-a",
            logical_trial_id=snapshot.logical_trials[0].admission.logical_trial_id,
            attempt_index=1,
        )
        self.assertEqual(scheduler.calls[0][1], expected_attempt_id)
        self.assertTrue(worker.shutdown_called)
        self.assertNotIn(str(self.root), repr(summary.to_dict()))

    def test_methodless_driver_executes_preseeded_plan_without_starting_method(
        self,
    ) -> None:
        authority = self.authority_from_create()
        accepted = authority.admit(
            (_candidate("candidate-a", 1),),
            admission_id="preseeded-evaluation-plan",
        )
        seeded = authority.refresh_controller()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(seeded.control.current_submission.state, "draining")
        self.assertEqual(seeded.control.current_submission.stop_code, "max_trials")

        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
        )
        driver, scheduler = self.driver(authority, worker)
        summary = driver.run()
        terminal = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

        self.assertNotEqual(summary.run_status, "running")
        self.assertEqual(len(scheduler.calls), 1)
        self.assertEqual(
            scheduler.calls[0][0], accepted[0].logical_trial_id
        )
        self.assertEqual(driver.method_runtime_provider.generations, [])
        self.assertEqual(worker.callback_calls, [])
        self.assertEqual(terminal.run.state, summary.run_status)
        self.assertTrue(
            all(item.state == "terminal" for item in terminal.logical_trials)
        )
        self.assertEqual(terminal.method_exchange_preparations, ())
        self.assertEqual(terminal.method_exchange_completions, ())

    def test_evaluator_capacity_overlaps_and_observes_in_proposal_order(self) -> None:
        run_id = "run-parallel"
        candidate_ids = ("candidate-a", "candidate-b", "candidate-c")
        candidates = tuple(
            _candidate(candidate_id, index)
            for index, candidate_id in enumerate(candidate_ids, start=1)
        )
        authority = self.parallel_authority(run_id=run_id)
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id=run_id,
            candidate=candidates,
        )
        scheduler = _GatedCancellingScheduler(authority, candidate_ids)
        driver = RealmRetainedBatchRunDriver(
            self.runtime,
            authority,
            method_runtime_provider=_FakeMethodProvider(worker),
            scheduler=scheduler,
            heartbeat_factory=lambda _snapshot, _method: _NoopHeartbeat(),
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
        )

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(driver.run)
        try:
            self.assertTrue(scheduler.entered["candidate-a"].wait(timeout=5))
            self.assertTrue(scheduler.entered["candidate-b"].wait(timeout=5))
            self.assertFalse(scheduler.entered["candidate-c"].is_set())
            self.assertEqual(
                set(scheduler.started_candidates[:2]),
                {"candidate-a", "candidate-b"},
            )

            scheduler.release["candidate-b"].set()
            self.assertTrue(scheduler.completed["candidate-b"].wait(timeout=5))
            self.assertTrue(scheduler.entered["candidate-c"].wait(timeout=5))
            self.assertEqual(scheduler.max_active, 2)

            scheduler.release["candidate-c"].set()
            self.assertTrue(scheduler.completed["candidate-c"].wait(timeout=5))
            self.assertFalse(worker.observe_entered.is_set())

            scheduler.release["candidate-a"].set()
            summary = future.result(timeout=10)
        finally:
            scheduler.release_all()
            executor.shutdown(wait=True, cancel_futures=True)

        self.assertNotEqual(summary.run_status, "running")
        self.assertEqual(
            scheduler.completed_candidates,
            ["candidate-b", "candidate-c", "candidate-a"],
        )
        observation_payload = next(
            payload
            for operation, payload in worker.callback_payloads
            if operation == "observe"
        )
        self.assertEqual(
            [item["candidate_id"] for item in observation_payload["observations"]],
            list(candidate_ids),
        )

    def test_local_dispatch_threads_are_bounded_by_capacity_and_host_limit(
        self,
    ) -> None:
        implementation_cap = 3
        cases = (
            ("declared-capacity", 2, 2),
            ("host-implementation-cap", 10**9, implementation_cap),
        )

        for label, declared_capacity, expected_width in cases:
            with self.subTest(label=label):
                authority = self.authority_from_create()
                scheduler = _DispatchGateScheduler(
                    authority,
                    saturation_width=expected_width,
                )
                worker = _FakeRetainedWorker(
                    self.ledger,
                    run_id="run-a",
                    candidate=None,
                )
                driver = RealmRetainedBatchRunDriver(
                    self.runtime,
                    authority,
                    method_runtime_provider=_FakeMethodProvider(worker),
                    scheduler=scheduler,
                    heartbeat_factory=lambda _snapshot, _method: _NoopHeartbeat(),
                    controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
                    attempt_ttl_seconds=60,
                )
                coordinates = tuple(
                    (f"trial-{index}", f"attempt-{index}")
                    for index in range(expected_width + 1)
                )

                with mock.patch.object(
                    retained_batch_run_driver,
                    "MAX_LOCAL_EVALUATOR_DISPATCH_THREADS",
                    implementation_cap,
                ), mock.patch.object(
                    retained_batch_run_driver,
                    "ThreadPoolExecutor",
                    wraps=ThreadPoolExecutor,
                ) as executor_factory:
                    coordinator = ThreadPoolExecutor(max_workers=1)
                    try:
                        future = coordinator.submit(
                            driver._advance_attempts,
                            coordinates,
                            evaluator_capacity=declared_capacity,
                        )
                        try:
                            self.assertTrue(scheduler.saturated.wait(timeout=5))
                            self.assertEqual(scheduler.started, expected_width)
                            self.assertEqual(scheduler.max_active, expected_width)
                            self.assertFalse(future.done())
                        finally:
                            scheduler.release.set()
                        future.result(timeout=10)
                    finally:
                        scheduler.release.set()
                        coordinator.shutdown(wait=True, cancel_futures=True)

                self.assertEqual(
                    executor_factory.call_args.kwargs["max_workers"],
                    expected_width,
                )
                self.assertEqual(scheduler.started, len(coordinates))
                self.assertEqual(scheduler.active, 0)
                self.assertLessEqual(scheduler.max_active, declared_capacity)
                self.assertLessEqual(scheduler.max_active, implementation_cap)

    def test_takeover_resumes_only_unfinished_parallel_attempts(self) -> None:
        run_id = "run-partial-parallel"
        candidate_ids = ("candidate-a", "candidate-b", "candidate-c")
        candidates = tuple(
            _candidate(candidate_id, index)
            for index, candidate_id in enumerate(candidate_ids, start=1)
        )
        authority = self.parallel_authority(run_id=run_id)
        first_worker = _FakeRetainedWorker(
            self.ledger,
            run_id=run_id,
            candidate=candidates,
        )
        first_scheduler = _GatedCancellingScheduler(
            authority,
            candidate_ids,
            fail_candidates=("candidate-a",),
        )
        first_driver = RealmRetainedBatchRunDriver(
            self.runtime,
            authority,
            method_runtime_provider=_FakeMethodProvider(first_worker),
            scheduler=first_scheduler,
            heartbeat_factory=lambda _snapshot, _method: _NoopHeartbeat(),
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
        )

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(first_driver.run)
        try:
            self.assertTrue(first_scheduler.entered["candidate-a"].wait(timeout=5))
            self.assertTrue(first_scheduler.entered["candidate-b"].wait(timeout=5))
            first_scheduler.release["candidate-a"].set()
            self.assertTrue(first_scheduler.completed["candidate-a"].wait(timeout=5))
            # A freed worker may refill candidate-c before the driver observes
            # candidate-a's exception.  Release it unconditionally so the
            # coordinator can quiesce every already-running task.
            first_scheduler.release["candidate-c"].set()
            first_scheduler.release["candidate-b"].set()
            with self.assertRaisesRegex(RuntimeError, "candidate-a"):
                future.result(timeout=10)
        finally:
            first_scheduler.release_all()
            executor.shutdown(wait=True, cancel_futures=True)

        self.assertEqual(first_scheduler.active, 0)
        interrupted = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=run_id
        )
        interrupted_states = {
            item.admission.candidate_id: item.state
            for item in interrupted.logical_trials
        }
        self.assertEqual(interrupted_states["candidate-a"], "accepted")
        self.assertEqual(interrupted_states["candidate-b"], "terminal")
        unfinished = {
            candidate_id
            for candidate_id, state in interrupted_states.items()
            if state != "terminal"
        }
        self.assertIn(
            unfinished,
            ({"candidate-a"}, {"candidate-a", "candidate-c"}),
        )

        recovered_authority = self.replace_controller(
            run_id=run_id, holder="controller-recovered"
        )
        recovered_worker = _FakeRetainedWorker(
            self.ledger,
            run_id=run_id,
            candidate=candidates,
        )
        recovered_scheduler = _GatedCancellingScheduler(
            recovered_authority,
            candidate_ids,
            auto_release=True,
        )
        recovered_driver = RealmRetainedBatchRunDriver(
            self.runtime,
            recovered_authority,
            method_runtime_provider=_FakeMethodProvider(recovered_worker),
            scheduler=recovered_scheduler,
            heartbeat_factory=lambda _snapshot, _method: _NoopHeartbeat(),
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
        )

        summary = recovered_driver.run()

        self.assertNotEqual(summary.run_status, "running")
        self.assertEqual(
            set(recovered_scheduler.started_candidates),
            unfinished,
        )
        self.assertNotIn("candidate-b", recovered_scheduler.started_candidates)
        self.assertLessEqual(recovered_scheduler.max_active, 2)
        observation_payload = next(
            payload
            for operation, payload in recovered_worker.callback_payloads
            if operation == "observe"
        )
        self.assertEqual(
            [item["candidate_id"] for item in observation_payload["observations"]],
            list(candidate_ids),
        )

    def test_empty_proposal_closes_without_entering_admission_or_observation(self) -> None:
        authority = self.authority_from_create()
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
        )
        driver, scheduler = self.driver(authority, worker)

        summary = driver.run()
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

        self.assertNotEqual(summary.run_status, "running")
        self.assertEqual(len(snapshot.candidates), 0)
        self.assertEqual(len(snapshot.logical_trials), 0)
        self.assertEqual(
            [(item.kind, item.outcome) for item in snapshot.method_exchange_completions],
            [("proposal", "empty")],
        )
        self.assertEqual(snapshot.control.current_submission.stop_code, "method_completed")
        self.assertEqual(scheduler.calls, [])
        self.assertEqual(worker.ack_calls, [1])

    def test_malformed_candidate_is_acked_and_force_stops_worker(self) -> None:
        authority = self.authority_from_create()
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate={
                **_candidate("candidate-malformed", 1),
                "unexpected": True,
            },
        )
        driver, scheduler = self.driver(authority, worker)

        summary = driver.run()
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

        self.assertEqual(summary.run_status, "failed")
        self.assertEqual(summary.submission_state, "terminal")
        self.assertEqual(summary.stop_code, "protocol_error")
        self.assertEqual(snapshot.run.state, "failed")
        self.assertEqual(snapshot.finalization.terminal_state, "failed")
        self.assertEqual(snapshot.finalization.code, "protocol_error")
        self.assertEqual(
            [
                (item.kind, item.outcome, item.error_code)
                for item in snapshot.method_exchange_completions
            ],
            [("proposal", "protocol_error", "candidate_malformed")],
        )
        self.assertEqual(snapshot.candidates, ())
        self.assertEqual(snapshot.logical_trials, ())
        self.assertEqual(scheduler.calls, [])
        self.assertEqual(worker.ack_calls, [1])
        self.assertFalse(worker.shutdown_called)
        self.assertTrue(worker.force_stop_called)

    def test_method_failure_closes_and_is_acknowledged_as_its_exact_response(self) -> None:
        authority = self.authority_from_create()
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
            proposal_error_code="method_failed",
        )
        driver, scheduler = self.driver(authority, worker)

        summary = driver.run()
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

        self.assertEqual(summary.run_status, "failed")
        self.assertEqual(
            snapshot.method_exchange_completions[0].outcome, "method_failed"
        )
        self.assertEqual(
            snapshot.method_exchange_completions[0].error_code, "method_failed"
        )
        self.assertEqual(scheduler.calls, [])
        self.assertEqual(worker.ack_calls, [1])

    def test_second_proposal_transport_timeout_terminalizes_visible_method_failure(
        self,
    ) -> None:
        authority = self.serial_authority(
            run_id="run-method-timeout",
            max_trials=2,
        )
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-method-timeout",
            candidate=_candidate("candidate-baseline", 1),
            runtime_error_sequence=3,
        )
        scheduler = _SuccessfulScheduler(authority)
        driver = RealmRetainedBatchRunDriver(
            self.runtime,
            authority,
            method_runtime_provider=_FakeMethodProvider(worker),
            scheduler=scheduler,
            heartbeat_factory=lambda _snapshot, _method: _NoopHeartbeat(),
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
        )

        summary = driver.run()
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id="run-method-timeout",
        )
        timeline = self.ledger.read_run_timeline_page(
            actor_principal_id="operator",
            run_id="run-method-timeout",
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )

        self.assertEqual(summary.run_status, "failed")
        self.assertEqual(summary.stop_code, "method_failed")
        self.assertEqual(snapshot.run.state, "failed")
        self.assertEqual(snapshot.finalization.code, "method_failed")
        self.assertEqual(snapshot.run.controller_generation, 1)
        self.assertEqual(
            [
                (item.kind, item.outcome, item.error_code)
                for item in snapshot.method_exchange_completions
            ],
            [
                ("proposal", "admitted", None),
                ("observation", "acknowledged", None),
            ],
        )
        abandoned = [
            event
            for event in timeline.items
            if event.event == "method_exchange_abandoned"
        ]
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0].method_round_index, 2)
        self.assertEqual(abandoned[0].method_exchange_kind, "proposal")
        self.assertEqual(abandoned[0].code, "method_failed")
        terminal_transitions = [
            transition
            for transition in snapshot.logical_transitions
            if transition.to_state == "terminal"
        ]
        self.assertEqual(
            [transition.outcome for transition in terminal_transitions],
            ["success"],
        )
        self.assertEqual(
            snapshot.observations[0].envelope.metric_values,
            {"score": 65.0754},
        )
        self.assertEqual(
            [
                operation
                for operation, _exchange_id, _sequence in worker.callback_calls
            ],
            ["propose", "observe", "propose"],
        )
        self.assertEqual(worker.ack_calls, [1, 2])
        self.assertTrue(worker.force_stop_called)
        self.assertFalse(worker.shutdown_called)
        self.assertNotIn(
            "controller_replaced",
            [event.event for event in timeline.items],
        )

    def test_observation_timeout_escalates_natural_drain_to_method_failure(
        self,
    ) -> None:
        authority = self.authority_from_create()
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-baseline", 1),
            runtime_error_sequence=2,
        )
        scheduler = _SuccessfulScheduler(authority)
        driver = RealmRetainedBatchRunDriver(
            self.runtime,
            authority,
            method_runtime_provider=_FakeMethodProvider(worker),
            scheduler=scheduler,
            heartbeat_factory=lambda _snapshot, _method: _NoopHeartbeat(),
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
        )

        summary = driver.run()
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id="run-a",
        )
        timeline = self.ledger.read_run_timeline_page(
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )

        self.assertEqual(summary.run_status, "failed")
        self.assertEqual(summary.stop_code, "method_failed")
        self.assertEqual(snapshot.finalization.code, "method_failed")
        self.assertEqual(
            [
                (item.kind, item.outcome)
                for item in snapshot.method_exchange_completions
            ],
            [("proposal", "admitted")],
        )
        abandoned = [
            event
            for event in timeline.items
            if event.event == "method_exchange_abandoned"
        ]
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0].method_exchange_kind, "observation")
        self.assertEqual(abandoned[0].code, "method_failed")
        self.assertIn(
            "run_stop_escalated",
            [event.event for event in timeline.items],
        )
        self.assertNotIn(
            "controller_replaced",
            [event.event for event in timeline.items],
        )
        self.assertEqual(worker.ack_calls, [1])
        self.assertTrue(worker.force_stop_called)
        self.assertFalse(worker.shutdown_called)

    def test_same_generation_pending_completion_is_acked_without_callback_replay(
        self,
    ) -> None:
        authority = self.authority_from_create()
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
            fail_ack_sequence=1,
        )
        first_driver, _scheduler = self.driver(authority, worker)
        with self.assertRaises(RuntimeError):
            first_driver.run()
        self.assertEqual(len(worker.callback_calls), 1)
        self.assertIsNotNone(worker.pending)

        attached_authority = RetainedRunAuthority.hydrate(
            ledger=self.ledger,
            actor_principal_id="operator",
            run_id="run-a",
            candidate_normalizer=_normalizer,
            normalizer_version=self.normalizer_version,
        )
        attached_driver, _attached_scheduler = self.driver(
            attached_authority, worker
        )
        summary = attached_driver.run()

        self.assertNotEqual(summary.run_status, "running")
        self.assertEqual(
            [item[0] for item in worker.callback_calls], ["propose", "observe"]
        )
        self.assertEqual(worker.ack_calls, [1, 2])

    def test_terminal_entry_retires_finish_before_shutdown_failure_idempotently(
        self,
    ) -> None:
        authority = self.authority_from_create()
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
            shutdown_error_once=True,
        )
        first_driver, _scheduler = self.driver(authority, worker)
        with self.assertRaisesRegex(RuntimeError, "shutdown failure"):
            first_driver.run()
        terminal = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertNotEqual(terminal.run.state, "running")

        recovered_authority = RetainedRunAuthority.hydrate(
            ledger=self.ledger,
            actor_principal_id="operator",
            run_id="run-a",
            candidate_normalizer=_normalizer,
            normalizer_version=self.normalizer_version,
        )
        maintenance_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
        )
        recovered_driver, recovered_scheduler = self.driver(
            recovered_authority, maintenance_worker
        )
        provider = recovered_driver.method_runtime_provider

        first_summary = recovered_driver.run()
        second_summary = recovered_driver.run()

        self.assertEqual(first_summary, second_summary)
        self.assertEqual(provider.generations, [])
        self.assertEqual(provider.retirement_calls, [1, 1])
        self.assertEqual(recovered_scheduler.calls, [])
        self.assertEqual(maintenance_worker.callback_calls, [])

    def test_terminal_retirement_receipt_must_confirm_exact_reconciliation(self) -> None:
        authority = self.authority_from_create()
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
        )
        first_driver, _scheduler = self.driver(authority, worker)
        first_driver.run()
        terminal_authority = RetainedRunAuthority.hydrate(
            ledger=self.ledger,
            actor_principal_id="operator",
            run_id="run-a",
            candidate_normalizer=_normalizer,
            normalizer_version=self.normalizer_version,
        )
        provider = _FakeMethodProvider(
            _FakeRetainedWorker(
                self.ledger,
                run_id="run-a",
                candidate=None,
            ),
            resources_reconciled=False,
        )
        driver = RealmRetainedBatchRunDriver(
            self.runtime,
            terminal_authority,
            method_runtime_provider=provider,
            scheduler=_CancellingScheduler(terminal_authority),
            heartbeat_factory=lambda _snapshot, _method: _NoopHeartbeat(),
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
        )

        with self.assertRaises(RealmRetainedBatchRunError) as raised:
            driver.run()
        self.assertEqual(raised.exception.code, "canonical_state_invalid")
        self.assertEqual(provider.generations, [])

    def test_ack_receipt_chain_is_verified_independently_of_provider(self) -> None:
        authority = self.authority_from_create()
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
            corrupt_ack_chain_sequence=1,
        )
        driver, scheduler = self.driver(authority, worker)

        with self.assertRaises(RealmRetainedBatchRunError) as raised:
            driver.run()

        self.assertEqual(raised.exception.code, "worker_state_diverged")
        self.assertTrue(worker.force_stop_called)
        self.assertEqual(scheduler.calls, [])
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(snapshot.method_exchange_completions[0].outcome, "empty")

    def test_takeover_replays_completed_prefix_then_resumes_next_exchange(self) -> None:
        first_authority = self.authority_from_create()
        first_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
            fail_ack_sequence=1,
        )
        first_driver, _first_scheduler = self.driver(first_authority, first_worker)
        with self.assertRaisesRegex(RuntimeError, "before worker ACK"):
            first_driver.run()
        after_crash = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(len(after_crash.method_exchange_completions), 1)
        self.assertEqual(first_worker.acknowledged_sequence, 0)

        recovered_authority = self.take_over(holder="controller-b")
        recovered_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
        )
        recovered_driver, _recovered_scheduler = self.driver(
            recovered_authority, recovered_worker
        )
        summary = recovered_driver.run()

        self.assertNotEqual(summary.run_status, "running")
        self.assertEqual(recovered_worker.ack_calls, [1, 2])
        self.assertEqual(
            [item[0] for item in recovered_worker.callback_calls],
            ["propose", "observe"],
        )
        final = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(final.run.controller_generation, 2)
        self.assertEqual(len(final.candidates), 1)
        self.assertEqual(len(final.method_exchange_completions), 2)

    def test_takeover_completes_a_proposal_prepared_by_the_prior_term(self) -> None:
        first_authority = self.authority_from_create()
        snapshot = first_authority.refresh_controller()
        preparation = self.ledger.prepare_run_method_exchange(
            operation_id="driver/pending-proposal/prepare",
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=snapshot.revision.revision,
            expected_controller_generation=snapshot.run.controller_generation,
            controller_lease_id=first_authority.controller_lease_id,
            controller_holder_id=first_authority.controller_holder_id,
            controller_fencing_token=first_authority.controller_fencing_token,
            exchange_input=build_method_proposal_exchange_input(
                snapshot, requested_width=1
            ),
        )
        recovered_authority = self.take_over(holder="controller-b")
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
        )
        driver, _scheduler = self.driver(recovered_authority, worker)

        summary = driver.run()
        final = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

        self.assertNotEqual(summary.run_status, "running")
        completion = next(
            item
            for item in final.method_exchange_completions
            if item.exchange_id == preparation.exchange_id
        )
        self.assertEqual(completion.outcome, "admitted")
        self.assertEqual(completion.controller_generation, 2)
        self.assertEqual([item[0] for item in worker.callback_calls], ["propose", "observe"])

    def test_takeover_completes_an_observation_prepared_by_the_prior_term(self) -> None:
        first_authority = self.authority_from_create()
        snapshot = first_authority.refresh_controller()
        proposal = self.ledger.prepare_run_method_exchange(
            operation_id="driver/pending-observation/proposal/prepare",
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=snapshot.revision.revision,
            expected_controller_generation=snapshot.run.controller_generation,
            controller_lease_id=first_authority.controller_lease_id,
            controller_holder_id=first_authority.controller_holder_id,
            controller_fencing_token=first_authority.controller_fencing_token,
            exchange_input=build_method_proposal_exchange_input(
                snapshot, requested_width=1
            ),
        )
        old_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
        )
        response = old_worker.request(
            proposal.exchange_id,
            "propose",
            proposal_worker_payload(proposal),
            exchange_sequence=1,
        )
        first_authority.refresh_controller()
        admitted = first_authority.complete_method_proposal(
            proposal,
            candidates=(_candidate("candidate-a", 1),),
            response_digest=response.response_digest,
        )
        assert old_worker.pending is not None
        old_worker.ack(
            "manual-proposal-ack",
            exchange=old_worker.pending,
            previous_acknowledged_chain=INITIAL_BATCH_EXCHANGE_CHAIN,
        )
        snapshot = first_authority.refresh_controller()
        self.ledger.cancel_run_logical_trial(
            operation_id="driver/pending-observation/cancel",
            actor_principal_id="operator",
            run_id="run-a",
            logical_trial_id=admitted.completion.logical_trial_ids[0],
            expected_run_revision=snapshot.revision.revision,
            controller_lease_id=first_authority.controller_lease_id,
            controller_holder_id=first_authority.controller_holder_id,
            controller_fencing_token=first_authority.controller_fencing_token,
            code="user_cancelled",
        )
        snapshot = first_authority.refresh_controller()
        observation = self.ledger.prepare_run_method_exchange(
            operation_id="driver/pending-observation/observe/prepare",
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=snapshot.revision.revision,
            expected_controller_generation=snapshot.run.controller_generation,
            controller_lease_id=first_authority.controller_lease_id,
            controller_holder_id=first_authority.controller_holder_id,
            controller_fencing_token=first_authority.controller_fencing_token,
            exchange_input=build_method_observation_exchange_input(
                snapshot, round_index=1
            ),
        )

        recovered_authority = self.take_over(holder="controller-b")
        recovered_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
        )
        driver, scheduler = self.driver(recovered_authority, recovered_worker)
        summary = driver.run()
        final = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

        self.assertNotEqual(summary.run_status, "running")
        completed_observation = next(
            item
            for item in final.method_exchange_completions
            if item.exchange_id == observation.exchange_id
        )
        self.assertEqual(completed_observation.outcome, "acknowledged")
        self.assertEqual(completed_observation.controller_generation, 2)
        self.assertEqual(scheduler.calls, [])
        self.assertEqual(
            [item[0] for item in recovered_worker.callback_calls],
            ["propose", "observe"],
        )

    def test_takeover_fails_closed_before_ack_when_replay_response_changes(self) -> None:
        first_authority = self.authority_from_create()
        first_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
            fail_ack_sequence=1,
        )
        first_driver, _first_scheduler = self.driver(first_authority, first_worker)
        with self.assertRaises(RuntimeError):
            first_driver.run()

        recovered_authority = self.take_over(holder="controller-b")
        divergent_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-changed", 999),
        )
        recovered_driver, recovered_scheduler = self.driver(
            recovered_authority, divergent_worker
        )
        with self.assertRaises(RealmRetainedBatchRunError) as raised:
            recovered_driver.run()

        self.assertEqual(raised.exception.code, "replay_diverged")
        self.assertEqual(divergent_worker.ack_calls, [])
        self.assertTrue(divergent_worker.force_stop_called)
        self.assertEqual(recovered_scheduler.calls, [])
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(snapshot.run.state, "failed")
        self.assertEqual(snapshot.finalization.code, "protocol_error")
        self.assertEqual(
            [item.stop_code for item in snapshot.control.submission_records],
            [None, "max_trials", "protocol_error", "protocol_error"],
        )
        self.assertTrue(
            all(item.state == "terminal" for item in snapshot.logical_trials)
        )
        self.assertEqual(len(snapshot.method_exchange_completions), 1)
        self.assertEqual(
            method_exchange_sequence(round_index=1, kind="proposal"), 1
        )

    def test_cleanup_failure_retains_divergence_without_canonical_mutation(self) -> None:
        first_authority = self.authority_from_create()
        first_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
            fail_ack_sequence=1,
        )
        first_driver, _first_scheduler = self.driver(first_authority, first_worker)
        with self.assertRaises(RuntimeError):
            first_driver.run()

        recovered_authority = self.take_over(holder="controller-b")
        before = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        divergent_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-changed", 999),
            force_stop_error=True,
        )
        recovered_driver, recovered_scheduler = self.driver(
            recovered_authority, divergent_worker
        )

        with self.assertRaises(RealmRetainedBatchRunError) as raised:
            recovered_driver.run()

        self.assertEqual(raised.exception.code, "definitive_cleanup_failed")
        self.assertEqual(
            raised.exception.canonical_failure_code,
            "replay_diverged",
        )
        self.assertTrue(divergent_worker.force_stop_called)
        self.assertEqual(divergent_worker.ack_calls, [])
        self.assertEqual(recovered_scheduler.calls, [])
        after = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(
            after.method_exchange_completions,
            before.method_exchange_completions,
        )
        self.assertEqual(after.logical_trials, before.logical_trials)
        self.assertEqual(after.revision, before.revision)

    def test_takeover_response_loss_replays_same_expected_prior_term(self) -> None:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        expected = RunControllerTakeoverExpectation.from_snapshot(snapshot)
        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
        )
        provider = _FakeMethodProvider(worker)
        original = self.ledger.replace_run_controller
        calls = 0

        def lose_first_response(**kwargs):
            nonlocal calls
            calls += 1
            receipt = original(**kwargs)
            if calls == 1:
                raise RuntimeError("simulated takeover response loss")
            return receipt

        common = {
            "expected_controller": expected,
            "takeover_operation_id": "driver/takeover/replayable",
            "new_controller_holder_id": "controller-b",
            "candidate_normalizer": _normalizer,
            "normalizer_version": self.normalizer_version,
            "controller_ttl_seconds": 60,
            "method_runtime_provider": provider,
            "heartbeat_factory": (
                lambda _snapshot, _method: _NoopHeartbeat()
            ),
        }
        with mock.patch.object(
            self.ledger,
            "replace_run_controller",
            side_effect=lose_first_response,
        ):
            with self.assertRaisesRegex(RuntimeError, "response loss"):
                RealmRetainedBatchRunDriver.take_over(self.runtime, **common)
            recovered = RealmRetainedBatchRunDriver.take_over(
                self.runtime, **common
            )

        current = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertIsInstance(recovered, RealmRetainedBatchRunDriver)
        self.assertEqual(calls, 2)
        self.assertEqual(current.run.controller_generation, 2)
        self.assertEqual(current.run.controller_holder_id, "controller-b")

    def test_takeover_refuses_a_term_replaced_again_before_hydration(self) -> None:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        expected = RunControllerTakeoverExpectation.from_snapshot(snapshot)
        original = self.ledger.replace_run_controller

        def replace_then_race(**kwargs):
            replacement = original(**kwargs)
            lease = replacement.controller_lease
            original(
                operation_id="driver/takeover/concurrent-gen-3",
                actor_principal_id="operator",
                run_id="run-a",
                expected_controller_generation=replacement.run.controller_generation,
                expected_controller_lease_id=lease.lease_id,
                expected_controller_holder_id=lease.holder_id,
                expected_controller_fencing_token=lease.fencing_token,
                new_controller_holder_id="controller-c",
                controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            )
            return replacement

        worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
        )
        with mock.patch.object(
            self.ledger,
            "replace_run_controller",
            side_effect=replace_then_race,
        ):
            with self.assertRaises(RealmRetainedBatchRunError) as raised:
                RealmRetainedBatchRunDriver.take_over(
                    self.runtime,
                    expected_controller=expected,
                    takeover_operation_id="driver/takeover/concurrent-gen-2",
                    new_controller_holder_id="controller-b",
                    candidate_normalizer=_normalizer,
                    normalizer_version=self.normalizer_version,
                    controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
                    method_runtime_provider=_FakeMethodProvider(worker),
                    heartbeat_factory=(
                        lambda _snapshot, _method: _NoopHeartbeat()
                    ),
                )

        self.assertEqual(raised.exception.code, "canonical_state_invalid")
        current = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(current.run.controller_generation, 3)
        self.assertEqual(current.run.controller_holder_id, "controller-c")

    def test_takeover_reconciles_hard_drained_proposal_without_worker_replay(
        self,
    ) -> None:
        first_authority = self.authority_from_create()
        first_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
            proposal_error_code="method_failed",
            fail_ack_sequence=1,
        )
        first_driver, _scheduler = self.driver(first_authority, first_worker)
        with self.assertRaises(RuntimeError):
            first_driver.run()

        recovered_authority = self.take_over(holder="controller-b")
        recovered_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=None,
            proposal_error_code="method_failed",
        )
        recovered_driver, recovered_scheduler = self.driver(
            recovered_authority, recovered_worker
        )
        summary = recovered_driver.run()

        self.assertEqual(summary.run_status, "failed")
        self.assertEqual(recovered_scheduler.calls, [])
        self.assertEqual(recovered_worker.ack_calls, [])
        self.assertEqual(recovered_worker.callback_calls, [])
        self.assertEqual(recovered_driver.method_runtime_provider.generations, [])

    def test_takeover_reconciles_hard_drained_observation_without_worker_replay(
        self,
    ) -> None:
        first_authority = self.authority_from_create()
        first_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
            observation_error_code="method_failed",
            fail_ack_sequence=2,
        )
        first_driver, _scheduler = self.driver(first_authority, first_worker)
        with self.assertRaises(RuntimeError):
            first_driver.run()
        crashed = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(
            [(item.kind, item.outcome) for item in crashed.method_exchange_completions],
            [("proposal", "admitted"), ("observation", "method_failed")],
        )

        recovered_authority = self.take_over(holder="controller-b")
        recovered_worker = _FakeRetainedWorker(
            self.ledger,
            run_id="run-a",
            candidate=_candidate("candidate-a", 1),
            observation_error_code="method_failed",
        )
        recovered_driver, recovered_scheduler = self.driver(
            recovered_authority, recovered_worker
        )
        summary = recovered_driver.run()

        self.assertEqual(summary.run_status, "failed")
        self.assertEqual(recovered_scheduler.calls, [])
        self.assertEqual(recovered_worker.ack_calls, [])
        self.assertEqual(recovered_worker.callback_calls, [])
        self.assertEqual(recovered_driver.method_runtime_provider.generations, [])



class RecordedMethodCauseTest(unittest.TestCase):
    """What a broken method is allowed to write into the durable stream."""

    def reduce(self, cause):
        return retained_batch_run_driver._recorded_method_cause(cause)

    def test_a_worker_cause_becomes_a_bounded_path_free_record(self) -> None:
        recorded = self.reduce(
            {
                "type": "RuntimeError",
                "message": (
                    "FileNotFoundError: [Errno 2] No such file or directory: "
                    "'/Users/someone/Library/Application Support/OptPilot/env/simulator'"
                ),
            }
        )

        self.assertEqual(recorded["type"], "RuntimeError")
        self.assertIn("FileNotFoundError", recorded["message"])
        self.assertNotIn("someone", recorded["message"])
        self.assertNotIn("/Users/", recorded["message"])
        self.assertIn("<path>", recorded["message"])
        self.assertFalse(recorded["truncated"])

    def test_traceback_frames_never_reach_the_record(self) -> None:
        recorded = self.reduce(
            {
                "type": "RuntimeError",
                "message": (
                    "Traceback (most recent call last):\n"
                    '  File "/Users/someone/method.py", line 3, in observe\n'
                    "    raise ValueError('boom')\n"
                    "ValueError: boom"
                ),
            }
        )

        for line in recorded["message"].splitlines():
            self.assertFalse(line.lstrip().startswith('File "'), line)
        self.assertIn("ValueError: boom", recorded["message"])
        self.assertNotIn("someone", recorded["message"])

    def test_an_unrecoverable_cause_records_nothing(self) -> None:
        for cause in (None, {}, "not a mapping", {"type": None}):
            with self.subTest(cause=cause):
                self.assertIsNone(self.reduce(cause))


class RecoveredWorkerDiagnosticTest(unittest.TestCase):
    """Reading the worker's private diagnostic back before its volume dies."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def runtime(self):
        from optpilot import retained_batch_runtime

        handle = retained_batch_runtime.RetainedPythonBatchRuntime.__new__(
            retained_batch_runtime.RetainedPythonBatchRuntime
        )
        object.__setattr__(handle, "_volume", SimpleNamespace(path=self.root))
        return handle

    def write(self, *lines: str) -> None:
        (self.root / "worker-diagnostics.jsonl").write_text(
            "".join(f"{line}\n" for line in lines), encoding="utf-8"
        )

    def test_the_matching_diagnostic_is_recovered(self) -> None:
        self.write(
            "a stray line the method printed to stdout",
            json.dumps({"diagnostic_id": "a" * 32, "exception_type": "KeyError", "message": "other"}),
            json.dumps(
                {
                    "diagnostic_id": "b" * 32,
                    "exception_type": "RuntimeError",
                    "message": "the real cause",
                    "traceback": "frames that must not travel",
                }
            ),
        )
        cause = self.runtime()._recorded_cause("b" * 32)

        self.assertEqual(cause, {"type": "RuntimeError", "message": "the real cause"})
        self.assertNotIn("traceback", cause)

    def test_every_failure_to_recover_is_silent(self) -> None:
        # A method failure must stay a method failure even when its
        # explanation cannot be read back.
        handle = self.runtime()
        self.assertIsNone(handle._recorded_cause("c" * 32))
        self.write("{ not json at all")
        self.assertIsNone(handle._recorded_cause("c" * 32))
        self.assertIsNone(handle._recorded_cause(None))
        self.assertIsNone(handle._recorded_cause(""))


if __name__ == "__main__":
    unittest.main()
