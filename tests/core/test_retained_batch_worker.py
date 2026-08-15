from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from optpilot.realm._validation import thaw_json
from optpilot.realm.refs import canonical_json_bytes, request_digest
from optpilot.retained_batch_worker import (
    BATCH_REQUEST_SCHEMA,
    INITIAL_BATCH_EXCHANGE_CHAIN,
    MAX_BATCH_EXCHANGE_ITEMS,
    MAX_BATCH_DURABLE_RESPONSE_BYTES,
    MAX_BATCH_FRAME_BYTES,
    MAX_UNIX_SOCKET_PATH_BYTES,
    RetainedBatchWorkerConfigurationError,
    RetainedBatchWorkerInit,
    RetainedPythonBatchEngine,
    UnixBatchWorkerServer,
    retained_batch_exchange_chain_digest,
    unix_batch_worker_request,
)
from optpilot.retained_study_compiler import compile_retained_process_study
from tests.core.test_retained_study_compiler import (
    _capability_study,
    _command_study,
    _manifest,
    _package,
    _provider,
    _study,
    _study_with_method_context,
)


_HEADER = struct.Struct("!I")


def _definition():
    return compile_retained_process_study(
        _study(),
        package=_package(),
        package_manifest=_manifest(),
        provider=_provider(),
        target_owner_id="retained-batch-worker-definition",
    ).run_definition


def _context_definition():
    study, manifest, package = _study_with_method_context()
    return compile_retained_process_study(
        study,
        package=package,
        package_manifest=manifest,
        provider=_provider(),
        target_owner_id="retained-batch-worker-context-definition",
    ).run_definition


def _request(
    exchange_id: str,
    op: str,
    payload: dict[str, Any],
    *,
    exchange_sequence: int | None = None,
) -> dict[str, Any]:
    result = {
        "exchange_id": exchange_id,
        "op": op,
        "payload": payload,
        "schema": BATCH_REQUEST_SCHEMA,
    }
    if op in {"propose", "observe"}:
        result["exchange_sequence"] = (
            1 if exchange_sequence is None else exchange_sequence
        )
    return result


def _propose(
    exchange_id: str,
    *,
    evidence: dict[str, Any] | None = None,
    exchange_sequence: int = 1,
    n_candidates: int = 1,
    study_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _request(
        exchange_id,
        "propose",
        {
            "evidence": evidence or {},
            "n_candidates": n_candidates,
            "study_state": study_state or {},
        },
        exchange_sequence=exchange_sequence,
    )


def _observe(
    exchange_id: str,
    observations: list[dict[str, Any]],
    *,
    exchange_sequence: int = 1,
) -> dict[str, Any]:
    return _request(
        exchange_id,
        "observe",
        {"observations": observations},
        exchange_sequence=exchange_sequence,
    )


def _ack(
    exchange_id: str,
    request: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    return _request(
        exchange_id,
        "ack",
        {
            "exchange": {
                "exchange_id": request["exchange_id"],
                "exchange_sequence": request["exchange_sequence"],
                "request_digest": request_digest(
                    {"op": request["op"], "payload": request["payload"]}
                ),
                "response_digest": hashlib.sha256(
                    canonical_json_bytes(response)
                ).hexdigest(),
            }
        },
    )


def _status(exchange_id: str) -> dict[str, Any]:
    return _request(exchange_id, "status", {})


def _shutdown(exchange_id: str = "shutdown-1") -> dict[str, Any]:
    return _request(exchange_id, "shutdown", {})


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    result = b""
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise AssertionError("socket response ended early")
        result += chunk
    return result


def _raw_exchange(socket_path: Path, payload: bytes, *, declared_size: int | None = None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(socket_path))
        connection.sendall(_HEADER.pack(declared_size or len(payload)) + payload)
        header = _receive_exact(connection, _HEADER.size)
        (size,) = _HEADER.unpack(header)
        response = _receive_exact(connection, size)
    return json.loads(response.decode("utf-8"))


def _framed_exchange(
    connection: socket.socket,
    payload: bytes,
    *,
    declared_size: int | None = None,
) -> dict[str, Any]:
    size = len(payload) if declared_size is None else declared_size
    connection.sendall(_HEADER.pack(size) + payload)
    header = _receive_exact(connection, _HEADER.size)
    (response_size,) = _HEADER.unpack(header)
    response = _receive_exact(connection, response_size)
    return json.loads(response.decode("utf-8"))


class RetainedBatchWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.projection = self.root / "projection"
        self.methods = self.projection / "methods"
        self.methods.mkdir(parents=True)
        (self.methods / "random.yaml").write_text("retained: true\n", encoding="utf-8")
        for module in ("method_impl", "worker_helper"):
            sys.modules.pop(module, None)
        self.addCleanup(sys.modules.pop, "method_impl", None)
        self.addCleanup(sys.modules.pop, "worker_helper", None)

    def _write_method(self, source: str) -> None:
        (self.methods / "method_impl.py").write_text(source, encoding="utf-8")

    def _engine(self, **kwargs: Any) -> RetainedPythonBatchEngine:
        engine = RetainedPythonBatchEngine(
            run_definition=_definition(),
            projection_root=self.projection,
            scope_roots={"study-package-source": "."},
            **kwargs,
        )
        self.addCleanup(engine.close)
        return engine

    def _require_unix_sockets(self) -> None:
        probe_path = self.root / "socket-probe.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(str(probe_path))
            except PermissionError as error:
                if error.errno == 1:
                    self.skipTest("sandbox denies AF_UNIX bind")
                raise
            finally:
                if probe_path.exists():
                    probe_path.unlink()

    def test_complete_manifest_context_dynamic_evidence_import_order_and_workdir(self) -> None:
        (self.methods / "worker_helper.py").write_text(
            "VALUE = 'declared-import-root'\n", encoding="utf-8"
        )
        (self.methods / "workdir-marker.txt").write_text(
            "exact-workdir", encoding="utf-8"
        )
        self._write_method(
            """
from pathlib import Path
from worker_helper import VALUE

class Method:
    def __init__(self, definition, study_spec, rng):
        self.definition = definition
        self.context = {
            "candidate_format": study_spec.candidate["format"],
            "candidate_min": study_spec.candidate["context"]["parameters"]["schema"]["x"]["min"],
            "environment_id": study_spec.environment["environmentId"],
            "objective": study_spec.objective["primaryMetric"]["name"],
            "parallelism": study_spec.execution["parallelism"]["candidateParallelism"],
            "seed": study_spec.reproducibility["seedPolicy"]["globalSeed"],
            "config_name": study_spec.path.name,
        }

    def propose(self, n_candidates, study_state, evidence_view):
        print("user stdout stays off the protocol")
        return [{
            "candidate_id": "context-candidate",
            "format": self.context["candidate_format"],
            "spec": {
                **self.context,
                "evidence_round": evidence_view.decision_context()["round"],
                "helper": VALUE,
                "marker": Path("workdir-marker.txt").read_text(encoding="utf-8"),
                "study_round": study_state["round"],
            },
        }]
""",
        )
        redirected: list[str] = []

        class Sink:
            def write(self, value: str) -> int:
                redirected.append(value)
                return len(value)

            def flush(self) -> None:
                return None

        engine = self._engine(user_stdout=Sink())
        first_request = _propose(
            "proposal-context-1",
            evidence={"round": 1},
            study_state={"round": 11},
        )
        first = engine.handle(first_request)
        engine.handle(_ack("ack-context-1", first_request, first))
        second = engine.handle(
            _propose(
                "proposal-context-2",
                exchange_sequence=2,
                evidence={"round": 2},
                study_state={"round": 12},
            )
        )

        self.assertTrue(first["ok"])
        spec = first["result"]["candidates"][0]["spec"]
        self.assertEqual(spec["candidate_format"], "parameters")
        self.assertEqual(spec["candidate_min"], 0.0)
        self.assertEqual(spec["environment_id"], "toy-factory")
        self.assertEqual(spec["objective"], "throughput")
        self.assertEqual(spec["parallelism"], 4)
        self.assertEqual(spec["seed"], 7)
        self.assertEqual(spec["config_name"], "random.yaml")
        self.assertEqual(spec["helper"], "declared-import-root")
        self.assertEqual(spec["marker"], "exact-workdir")
        self.assertEqual(spec["evidence_round"], 1)
        self.assertEqual(spec["study_round"], 11)
        self.assertEqual(
            second["result"]["candidates"][0]["spec"]["evidence_round"], 2
        )
        self.assertIn("user stdout stays off the protocol", "".join(redirected))

    def test_method_context_paths_are_hydrated_only_at_the_worker_boundary(self) -> None:
        context_root = self.projection / "environments" / "context"
        context_root.mkdir(parents=True)
        (context_root / "prompt.md").write_text("optimize", encoding="utf-8")
        (context_root / "cases.yaml").write_text("cases: []\n", encoding="utf-8")
        (context_root / "undeclared.txt").write_text("private", encoding="utf-8")
        self._write_method(
            """
from pathlib import Path

class Method:
    def __init__(self, definition, study_spec, rng):
        context = study_spec.candidate["context"]["methodContext"]
        self.prompt = Path(context["instructions"][0]).read_text(encoding="utf-8")

    def propose(self, n_candidates, study_state):
        context = study_state["candidate_context"]["methodContext"]
        reference = context["references"][0]
        return [{
            "candidate_id": "context-boundary",
            "format": "parameters",
            "spec": {
                "prompt": self.prompt,
                "cases": Path(reference["path"]).read_text(encoding="utf-8"),
                "declared_names": sorted(item["name"] for item in context["references"]),
            },
        }]
""",
        )
        definition = _context_definition()
        engine = RetainedPythonBatchEngine(
            run_definition=definition,
            projection_root=self.projection,
            scope_roots={
                "method-context": "environments/context",
                "study-package-source": ".",
            },
        )
        self.addCleanup(engine.close)
        durable_context = json.loads(
            canonical_json_bytes(
                thaw_json(
                    definition.evaluation_closure.environment_revision.candidate_contract[
                        "context"
                    ]
                )
            ).decode("utf-8")
        )
        request = _propose(
            "proposal-method-context",
            study_state={"candidate_context": durable_context},
        )

        response = engine.handle(request)
        replay = engine.handle(request)

        self.assertTrue(response["ok"], response)
        self.assertEqual(replay, response)
        spec = response["result"]["candidates"][0]["spec"]
        self.assertEqual(spec["prompt"], "optimize")
        self.assertEqual(spec["cases"], "cases: []\n")
        self.assertEqual(spec["declared_names"], ["cases"])
        encoded_request = json.dumps(request, sort_keys=True)
        self.assertNotIn(str(self.projection), encoded_request)
        self.assertNotIn("undeclared.txt", encoded_request)

    def test_uses_copied_retained_projection_after_original_changes_and_disappears(self) -> None:
        original = self.root / "original"
        original_methods = original / "methods"
        original_methods.mkdir(parents=True)
        (original_methods / "random.yaml").write_text("original: true\n", encoding="utf-8")
        (original_methods / "method_impl.py").write_text(
            """
class Method:
    def __init__(self, definition, study_spec, rng):
        self.value = "sealed-value"
    def propose(self, n_candidates, study_state, evidence_view):
        return [{"candidate_id": "sealed", "format": "parameters", "spec": {"value": self.value}}]
""",
            encoding="utf-8",
        )
        shutil.rmtree(self.projection)
        shutil.copytree(original, self.projection)
        (original_methods / "method_impl.py").write_text(
            "raise RuntimeError('mutable checkout was used')\n", encoding="utf-8"
        )
        shutil.rmtree(original)

        response = self._engine().handle(_propose("proposal-sealed"))

        self.assertEqual(
            response["result"]["candidates"][0]["spec"]["value"], "sealed-value"
        )

    def test_proposal_replay_is_exact_and_changed_payload_is_rejected(self) -> None:
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): self.calls = 0
    def propose(self, n_candidates, study_state, evidence_view):
        self.calls += 1
        return [{"candidate_id": f"call-{self.calls}", "format": "parameters", "spec": {"calls": self.calls}}]
"""
        )
        engine = self._engine()
        request = _propose("proposal-replay")

        first = engine.handle(request)
        replay = engine.handle(request)
        conflict = engine.handle(_propose("proposal-replay", n_candidates=2))
        acknowledgement = engine.handle(_ack("ack-replay", request, first))
        after = engine.handle(
            _propose("proposal-next", exchange_sequence=2)
        )

        self.assertEqual(first, replay)
        self.assertEqual(conflict["error"]["code"], "exchange_conflict")
        self.assertTrue(acknowledgement["ok"])
        self.assertEqual(first["result"]["candidates"][0]["spec"]["calls"], 1)
        self.assertEqual(after["result"]["candidates"][0]["spec"]["calls"], 2)

    def test_overproduced_batch_becomes_one_replayable_protocol_error(self) -> None:
        self._write_method(
            """
CALLS = 0
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view):
        global CALLS
        CALLS += 1
        return [
            {"candidate_id": "a", "format": "parameters", "spec": {}},
            {"candidate_id": "b", "format": "parameters", "spec": {}},
        ]
"""
        )
        engine = self._engine()
        request = _propose("proposal-overproduced", n_candidates=1)

        first = engine.handle(request)
        replay = engine.handle(request)

        self.assertEqual(first, replay)
        self.assertEqual(first["error"]["code"], "batch_overproduced")
        self.assertEqual(sys.modules["method_impl"].CALLS, 1)

    def test_observe_replay_does_not_repeat_callback(self) -> None:
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): self.observed = 0
    def observe(self, observations): self.observed += len(observations)
    def propose(self, n_candidates, study_state, evidence_view):
        return [{"candidate_id": "observed", "format": "parameters", "spec": {"observed": self.observed}}]
"""
        )
        engine = self._engine()
        request = _observe("observe-replay", [{"status": "success"}])

        first = engine.handle(request)
        replay = engine.handle(request)
        acknowledgement = engine.handle(_ack("ack-observe", request, first))
        proposal = engine.handle(
            _propose("proposal-after-observe", exchange_sequence=2)
        )

        self.assertEqual(first, replay)
        self.assertTrue(acknowledgement["ok"])
        self.assertEqual(
            proposal["result"]["candidates"][0]["spec"]["observed"], 1
        )

    def test_observe_rejects_more_than_the_durable_item_bound(self) -> None:
        self._write_method(
            """
CALLS = 0
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def observe(self, observations):
        global CALLS
        CALLS += 1
    def propose(self, n_candidates, study_state, evidence_view): return []
"""
        )
        engine = self._engine()
        request = _observe(
            "observe-too-wide",
            [{} for _ in range(MAX_BATCH_EXCHANGE_ITEMS + 1)],
        )

        first = engine.handle(request)
        replay = engine.handle(request)

        self.assertEqual(first, replay)
        self.assertEqual(first["error"]["code"], "invalid_observation")
        self.assertEqual(sys.modules["method_impl"].CALLS, 0)

    def test_observe_rejects_an_empty_batch(self) -> None:
        self._write_method(
            """
CALLS = 0
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def observe(self, observations):
        global CALLS
        CALLS += 1
    def propose(self, n_candidates, study_state, evidence_view): return []
"""
        )
        engine = self._engine()

        response = engine.handle(_observe("observe-empty", []))

        self.assertEqual(response["error"]["code"], "invalid_observation")
        self.assertEqual(sys.modules["method_impl"].CALLS, 0)

    def test_only_one_exchange_is_pending_until_exact_acknowledgement(self) -> None:
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): self.calls = 0
    def propose(self, n_candidates, study_state, evidence_view):
        self.calls += 1
        return [{"candidate_id": f"candidate-{self.calls}", "format": "parameters", "spec": {"calls": self.calls}}]
"""
        )
        engine = self._engine()

        first_request = _propose("proposal-cache-1")
        second_request = _propose("proposal-cache-2", exchange_sequence=2)
        first = engine.handle(first_request)
        full = engine.handle(second_request)
        acknowledgement = engine.handle(
            _ack("ack-cache-1", first_request, first)
        )
        second = engine.handle(second_request)

        self.assertTrue(first["ok"])
        self.assertEqual(full["error"]["code"], "exchange_out_of_order")
        self.assertEqual(
            acknowledgement["result"]["acknowledged_exchange"]["exchange_id"],
            "proposal-cache-1",
        )
        self.assertEqual(second["result"]["candidates"][0]["spec"]["calls"], 2)
        self.assertEqual(engine.cached_exchange_count, 1)

    def test_acknowledged_watermark_prevents_duplicate_callback_after_recovery(
        self,
    ) -> None:
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): self.calls = 0
    def propose(self, n_candidates, study_state, evidence_view):
        self.calls += 1
        return [{"candidate_id": f"candidate-{self.calls}", "format": "parameters", "spec": {"calls": self.calls}}]
"""
        )
        engine = self._engine()
        request = _propose("proposal-acked")

        first = engine.handle(request)
        pending = engine.handle(_status("status-pending"))
        ack_request = _ack("ack-proposal", request, first)
        acknowledgement = engine.handle(ack_request)
        acknowledged = engine.handle(_status("status-acknowledged"))
        replay = engine.handle(request)
        conflict = engine.handle(_propose("proposal-acked", n_candidates=2))
        next_proposal = engine.handle(
            _propose("proposal-next", exchange_sequence=2)
        )
        repeated_ack = engine.handle(
            {**ack_request, "exchange_id": "ack-again"}
        )
        conflicting_ack = _ack("ack-conflict", request, first)
        conflicting_ack["payload"]["exchange"]["response_digest"] = "0" * 64
        conflict_response = engine.handle(conflicting_ack)

        expected_response_digest = hashlib.sha256(
            canonical_json_bytes(first)
        ).hexdigest()
        expected_request_digest = request_digest(
            {"op": request["op"], "payload": request["payload"]}
        )
        self.assertEqual(pending["result"]["acknowledged_sequence"], 0)
        self.assertEqual(
            pending["result"]["pending_exchange"]["request_digest"],
            expected_request_digest,
        )
        self.assertEqual(
            pending["result"]["pending_exchange"]["response_digest"],
            expected_response_digest,
        )
        self.assertEqual(
            acknowledgement["result"]["acknowledged_sequence"], 1
        )
        self.assertEqual(acknowledged["result"]["acknowledged_sequence"], 1)
        self.assertIsNone(acknowledged["result"]["pending_exchange"])
        self.assertEqual(
            acknowledgement["result"]["acknowledged_exchange"]["response_digest"],
            expected_response_digest,
        )
        self.assertEqual(replay["error"]["code"], "exchange_acknowledged")
        self.assertEqual(conflict["error"]["code"], "exchange_acknowledged")
        self.assertEqual(
            next_proposal["result"]["candidates"][0]["spec"]["calls"], 2
        )
        self.assertEqual(
            repeated_ack["result"], acknowledgement["result"]
        )
        self.assertEqual(conflict_response["error"]["code"], "ack_conflict")
        self.assertEqual(engine.acknowledged_exchange_count, 1)

    def test_many_acknowledged_rounds_reduce_to_one_contiguous_chain_watermark(
        self,
    ) -> None:
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): self.calls = 0
    def propose(self, n_candidates, study_state, evidence_view):
        self.calls += 1
        return [{"candidate_id": f"candidate-{self.calls}", "format": "parameters", "spec": {"calls": self.calls}}]
"""
        )
        engine = self._engine()
        expected_chain = INITIAL_BATCH_EXCHANGE_CHAIN
        for sequence in range(1, 101):
            request = _propose(
                f"proposal-{sequence}", exchange_sequence=sequence
            )
            response = engine.handle(request)
            request_digest_value = request_digest(
                {"op": request["op"], "payload": request["payload"]}
            )
            response_digest = hashlib.sha256(
                canonical_json_bytes(response)
            ).hexdigest()
            expected_chain = retained_batch_exchange_chain_digest(
                expected_chain,
                exchange_id=request["exchange_id"],
                exchange_sequence=sequence,
                request_digest_value=request_digest_value,
                response_digest=response_digest,
            )
            acknowledgement = engine.handle(
                _ack(f"ack-{sequence}", request, response)
            )
            self.assertEqual(
                acknowledgement["result"]["acknowledged_chain"],
                expected_chain,
            )

        status = engine.handle(_status("status-after-many"))
        self.assertEqual(status["result"]["acknowledged_sequence"], 100)
        self.assertEqual(status["result"]["acknowledged_chain"], expected_chain)
        self.assertIsNone(status["result"]["pending_exchange"])
        self.assertEqual(status["result"]["pending_response_bytes"], 0)
        self.assertEqual(engine.cached_exchange_count, 0)
        self.assertEqual(engine.acknowledged_exchange_count, 100)
        self.assertNotIn("_acknowledged", engine.__dict__)

    def test_method_exception_is_path_free_bounded_private_and_replayable(self) -> None:
        secret = str(self.root / "private" / "source.py")
        self._write_method(
            f"""
class Method:
    def __init__(self, definition, study_spec, rng): self.calls = 0
    def propose(self, n_candidates, study_state, evidence_view):
        self.calls += 1
        raise RuntimeError({secret!r} + ": sensitive details")
"""
        )
        diagnostics: list[dict[str, Any]] = []
        engine = self._engine(diagnostic=lambda event: diagnostics.append(dict(event)))
        request = _propose("proposal-error")

        first = engine.handle(request)
        replay = engine.handle(request)
        public = canonical_json_bytes(first)

        self.assertEqual(first, replay)
        self.assertEqual(first["error"]["code"], "method_failed")
        self.assertNotIn(secret.encode(), public)
        self.assertLess(len(public), 1024)
        self.assertEqual(len(diagnostics), 1)
        self.assertIn(secret, diagnostics[0]["message"])
        self.assertEqual(
            first["error"]["diagnostic_id"], diagnostics[0]["diagnostic_id"]
        )

    def test_method_failure_full_response_digest_replays_across_fresh_engines(
        self,
    ) -> None:
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view):
        raise RuntimeError("private failure detail")
"""
        )
        request = _propose("proposal-failure-across-engines")
        first_diagnostics: list[dict[str, Any]] = []
        first_engine = self._engine(
            diagnostic=lambda event: first_diagnostics.append(dict(event))
        )
        first = first_engine.handle(request)
        first_engine.close()

        # A replacement worker imports a fresh projected module and reconstructs
        # method state from the same retained definition and exact request.
        sys.modules.pop("method_impl", None)
        second_diagnostics: list[dict[str, Any]] = []
        second_engine = self._engine(
            diagnostic=lambda event: second_diagnostics.append(dict(event))
        )
        second = second_engine.handle(request)

        self.assertEqual(first, second)
        self.assertEqual(
            hashlib.sha256(canonical_json_bytes(first)).hexdigest(),
            hashlib.sha256(canonical_json_bytes(second)).hexdigest(),
        )
        self.assertEqual(first["error"]["code"], "method_failed")
        self.assertEqual(
            first["error"]["diagnostic_id"],
            second["error"]["diagnostic_id"],
        )
        self.assertEqual(
            first_diagnostics[0]["diagnostic_id"],
            second_diagnostics[0]["diagnostic_id"],
        )

    def test_oversized_method_response_becomes_bounded_replayable_error(self) -> None:
        self._write_method(
            """
CALLS = 0
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view):
        global CALLS
        CALLS += 1
        return [{"candidate_id": "oversized", "format": "parameters", "spec": {"value": "x" * 4096}}]
"""
        )
        engine = self._engine(max_payload_bytes=1024)
        request = _propose("proposal-oversized")

        first = engine.handle(request)
        replay = engine.handle(request)

        self.assertEqual(first, replay)
        self.assertEqual(first["error"]["code"], "response_too_large")
        self.assertLess(len(canonical_json_bytes(first)), 1024)
        self.assertEqual(sys.modules["method_impl"].CALLS, 1)

    def test_default_response_bound_matches_durable_exchange_digest_bound(self) -> None:
        self._write_method(
            f"""
CALLS = 0
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view):
        global CALLS
        CALLS += 1
        return [{{"candidate_id": "oversized", "format": "parameters", "spec": {{"value": "x" * {MAX_BATCH_DURABLE_RESPONSE_BYTES}}}}}]
"""
        )
        engine = self._engine()
        request = _propose("proposal-durable-bound")

        first = engine.handle(request)
        replay = engine.handle(request)

        self.assertEqual(first, replay)
        self.assertEqual(first["error"]["code"], "response_too_large")
        self.assertLess(
            len(canonical_json_bytes(first)), MAX_BATCH_DURABLE_RESPONSE_BYTES
        )
        self.assertEqual(sys.modules["method_impl"].CALLS, 1)

    def test_projection_rejects_symlinked_import_or_workdir(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "random.yaml").write_text("outside: true\n", encoding="utf-8")
        (outside / "method_impl.py").write_text("class Method: pass\n", encoding="utf-8")
        shutil.rmtree(self.methods)
        os.symlink(outside, self.methods)

        with self.assertRaisesRegex(
            RetainedBatchWorkerConfigurationError, "symbolic link"
        ):
            self._engine()

    def test_ambient_same_name_module_is_rejected_before_its_code_executes(
        self,
    ) -> None:
        ambient = self.root / "ambient"
        ambient.mkdir()
        sentinel = self.root / "ambient-imported"
        (ambient / "method_impl.py").write_text(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n",
            encoding="utf-8",
        )
        sys.path.insert(0, str(ambient))
        self.addCleanup(sys.path.remove, str(ambient))

        with self.assertRaisesRegex(
            RetainedBatchWorkerConfigurationError, "absent from its import roots"
        ):
            self._engine()

        self.assertFalse(sentinel.exists())
        self.assertNotIn("method_impl", sys.modules)

    def test_projected_module_cannot_forge_its_loaded_origin(self) -> None:
        forged = self.methods / "forged.py"
        forged.write_text("# marker\n", encoding="utf-8")
        self._write_method(
            f"""
__file__ = {str(forged)!r}
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view): return []
"""
        )

        with self.assertRaisesRegex(
            RetainedBatchWorkerConfigurationError,
            "resolved outside its declared import roots",
        ):
            self._engine()

    def test_overlong_unix_socket_path_fails_before_launch(self) -> None:
        path = "/" + "s" * MAX_UNIX_SOCKET_PATH_BYTES
        self.assertGreater(len(os.fsencode(path)), MAX_UNIX_SOCKET_PATH_BYTES)

        with self.assertRaisesRegex(
            RetainedBatchWorkerConfigurationError, "Unix-socket length bound"
        ):
            RetainedBatchWorkerInit(
                run_definition=_definition(),
                projection_root=str(self.projection),
                scope_roots={"study-package-source": "."},
                socket_path=path,
                diagnostic_path=str(self.root / "diagnostic.log"),
            )

    def test_socket_transport_rejects_malformed_noncanonical_and_oversized_frames(self) -> None:
        self._require_unix_sockets()
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view): return []
"""
        )
        engine = self._engine()
        socket_path = self.root / "worker.sock"
        server = UnixBatchWorkerServer(engine, socket_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        ready_identity = socket_path.lstat()

        malformed = _raw_exchange(socket_path, b"{")
        noncanonical = _raw_exchange(socket_path, b"{} ")
        oversized = _raw_exchange(
            socket_path, b"", declared_size=MAX_BATCH_FRAME_BYTES + 1
        )
        stopped = unix_batch_worker_request(socket_path, _shutdown())
        thread.join(5)

        self.assertEqual(malformed["error"]["code"], "malformed_frame")
        self.assertEqual(noncanonical["error"]["code"], "noncanonical_frame")
        self.assertEqual(oversized["error"]["code"], "frame_too_large")
        self.assertTrue(stopped["result"]["shutdown"])
        self.assertFalse(thread.is_alive())
        self.assertTrue(engine._closed)
        retained_identity = socket_path.lstat()
        self.assertTrue(stat.S_ISSOCK(retained_identity.st_mode))
        self.assertEqual(
            (retained_identity.st_dev, retained_identity.st_ino),
            (ready_identity.st_dev, ready_identity.st_ino),
        )

    def test_framed_socket_connection_smoke_and_shutdown_without_stdio(self) -> None:
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view):
        print("socketpair user output")
        return [{"candidate_id": "socketpair", "format": "parameters", "spec": {"ok": True}}]
"""
        )
        redirected: list[str] = []

        class Sink:
            def write(self, value: str) -> int:
                redirected.append(value)
                return len(value)

            def flush(self) -> None:
                return None

        engine = self._engine(user_stdout=Sink())
        server = UnixBatchWorkerServer(engine, self.root / "unbound.sock")

        first_client, first_server = socket.socketpair()
        first_thread = threading.Thread(
            target=server.serve_connection, args=(first_server,), daemon=True
        )
        first_thread.start()
        oversized = _framed_exchange(
            first_client, b"", declared_size=MAX_BATCH_FRAME_BYTES + 1
        )
        first_client.close()
        first_server.close()
        first_thread.join(5)

        client, accepted = socket.socketpair()
        thread = threading.Thread(
            target=server.serve_connection, args=(accepted,), daemon=True
        )
        thread.start()
        malformed = _framed_exchange(client, b"{")
        noncanonical = _framed_exchange(client, b"{} ")
        proposal_request = canonical_json_bytes(_propose("proposal-socketpair"))
        proposal = _framed_exchange(client, proposal_request)
        shutdown_request = canonical_json_bytes(_shutdown("shutdown-socketpair"))
        shutdown = _framed_exchange(client, shutdown_request)
        client.close()
        accepted.close()
        thread.join(5)

        self.assertEqual(oversized["error"]["code"], "frame_too_large")
        self.assertEqual(malformed["error"]["code"], "malformed_frame")
        self.assertEqual(noncanonical["error"]["code"], "noncanonical_frame")
        self.assertTrue(proposal["result"]["candidates"][0]["spec"]["ok"])
        self.assertTrue(shutdown["result"]["shutdown"])
        self.assertIn("socketpair user output", "".join(redirected))
        self.assertFalse(thread.is_alive())

    def test_partial_frame_times_out_without_blocking_the_next_connection(self) -> None:
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view):
        return [{"candidate_id": "after-timeout", "format": "parameters", "spec": {"ok": True}}]
"""
        )
        engine = self._engine()
        server = UnixBatchWorkerServer(
            engine,
            self.root / "unused-timeout.sock",
            connection_timeout=0.05,
        )

        stalled_client, stalled_server = socket.socketpair()
        stalled = threading.Thread(
            target=server.serve_connection, args=(stalled_server,), daemon=True
        )
        stalled.start()
        stalled_client.sendall(b"\x00\x00")
        stalled.join(1)
        stalled_client.close()
        stalled_server.close()
        self.assertFalse(stalled.is_alive())

        client, accepted = socket.socketpair()
        serving = threading.Thread(
            target=server.serve_connection, args=(accepted,), daemon=True
        )
        serving.start()
        response = _framed_exchange(
            client, canonical_json_bytes(_propose("proposal-after-timeout"))
        )
        client.close()
        accepted.close()
        serving.join(1)

        self.assertTrue(response["result"]["candidates"][0]["spec"]["ok"])
        self.assertFalse(serving.is_alive())

    def test_supervised_shape_uses_socket_with_all_stdio_detached_and_shuts_down(self) -> None:
        self._require_unix_sockets()
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view):
        print("private worker stdout")
        return [{"candidate_id": "socket", "format": "parameters", "spec": {"socket": True}}]
"""
        )
        socket_path = self.root / "subprocess.sock"
        diagnostic_path = self.root / "subprocess.log"
        initialization = RetainedBatchWorkerInit(
            run_definition=_definition(),
            projection_root=str(self.projection),
            scope_roots={"study-package-source": "."},
            socket_path=str(socket_path),
            diagnostic_path=str(diagnostic_path),
        )
        init_path = self.root / "init.json"
        init_path.write_bytes(initialization.to_bytes())
        process = subprocess.Popen(
            [sys.executable, "-m", "optpilot.retained_batch_worker", str(init_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while not socket_path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(process.poll())
        self.assertTrue(socket_path.exists())
        ready_identity = socket_path.lstat()

        proposal = unix_batch_worker_request(
            socket_path, _propose("proposal-socket")
        )
        shutdown = unix_batch_worker_request(socket_path, _shutdown("shutdown-socket"))
        process.wait(timeout=5)

        self.assertTrue(proposal["result"]["candidates"][0]["spec"]["socket"])
        self.assertTrue(shutdown["result"]["shutdown"])
        self.assertEqual(process.returncode, 0)
        retained_identity = socket_path.lstat()
        self.assertTrue(stat.S_ISSOCK(retained_identity.st_mode))
        self.assertEqual(
            (retained_identity.st_dev, retained_identity.st_ino),
            (ready_identity.st_dev, ready_identity.st_ino),
        )
        self.assertIn(
            "private worker stdout", diagnostic_path.read_text(encoding="utf-8")
        )

    def test_api_has_no_run_directory_evidence_store_or_resubmitted_study_spec(self) -> None:
        self._write_method(
            """
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view): return []
"""
        )
        signature = inspect.signature(RetainedPythonBatchEngine)
        encoded = json.dumps(
            RetainedBatchWorkerInit(
                run_definition=_definition(),
                projection_root=str(self.projection),
                scope_roots={"study-package-source": "."},
                socket_path=str(self.root / "api.sock"),
                diagnostic_path=str(self.root / "api.log"),
            ).to_dict(),
            sort_keys=True,
        )

        for forbidden in ("run_dir", "evidence_store", "study_spec_path", "study_spec_raw"):
            self.assertNotIn(forbidden, signature.parameters)
            self.assertNotIn(forbidden, encoded)


def _capability_definition():
    return compile_retained_process_study(
        _capability_study(),
        package=_package(),
        package_manifest=_manifest(),
        provider=_provider(),
        target_owner_id="retained-batch-worker-capability-definition",
    ).run_definition


class RetainedCapabilityImportRootTest(unittest.TestCase):
    """A required capability callable is resolvable without a pythonPath hack (F5)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.projection = Path(self.temporary.name) / "projection"
        self.methods = self.projection / "methods"
        self.environments = self.projection / "environments"
        self.methods.mkdir(parents=True)
        self.environments.mkdir(parents=True)
        (self.methods / "random.yaml").write_text("retained: true\n", encoding="utf-8")
        for module in ("method_impl", "env_impl"):
            sys.modules.pop(module, None)
            self.addCleanup(sys.modules.pop, module, None)

    def test_method_resolves_the_environment_capability_callable(self) -> None:
        (self.environments / "env_impl.py").write_text(
            """
def evaluate(candidate, context):
    return {"score": 1.0}

def replay_candidate(seed):
    return {"seed": seed, "trace": "exact"}
""",
            encoding="utf-8",
        )
        (self.methods / "method_impl.py").write_text(
            """
from env_impl import replay_candidate

class Method:
    def __init__(self, definition, study_spec, rng):
        capabilities = study_spec.candidate["context"]["capabilities"]
        self.declared = {
            item["id"]: item.get("callable") for item in capabilities
        }

    def propose(self, n_candidates, study_state, evidence_view):
        replayed = replay_candidate(7)
        return [{
            "candidate_id": "capability-candidate",
            "format": "parameters",
            "spec": {
                "declared_callable": self.declared["exact_seed_replay"],
                "replayed_seed": replayed["seed"],
                "x": 0.5,
            },
        }]
""",
            encoding="utf-8",
        )
        engine = RetainedPythonBatchEngine(
            run_definition=_capability_definition(),
            projection_root=self.projection,
            scope_roots={"study-package-source": "."},
        )
        self.addCleanup(engine.close)
        response = engine.handle(_propose("capability-proposal"))

        self.assertTrue(response["ok"], response)
        spec = response["result"]["candidates"][0]["spec"]
        self.assertEqual(spec["declared_callable"], "env_impl:replay_candidate")
        self.assertEqual(spec["replayed_seed"], 7)


def _command_definition(
    command: list[str] | None = None,
    *,
    exchange_timeout: int | None = None,
):
    study = _command_study(command)
    if exchange_timeout is not None:
        study.method["runtime"]["exchangeTimeoutSeconds"] = exchange_timeout
    return compile_retained_process_study(
        study,
        package=_package(),
        package_manifest=_manifest(),
        provider=_provider(),
        target_owner_id="retained-command-batch-worker-definition",
    ).run_definition


class RetainedCommandBatchWorkerTest(unittest.TestCase):
    """Worker-level coverage for command-protocol batch methods (F3)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.projection = self.root / "projection"
        self.methods = self.projection / "methods"
        self.methods.mkdir(parents=True)
        (self.methods / "random.yaml").write_text("retained: true\n", encoding="utf-8")

    def _write_command_script(self, source: str) -> None:
        (self.methods / "method_impl.py").write_text(source, encoding="utf-8")

    def _engine(
        self,
        command: list[str] | None = None,
        *,
        exchange_timeout: int | None = None,
        **kwargs: Any,
    ) -> RetainedPythonBatchEngine:
        engine = RetainedPythonBatchEngine(
            run_definition=_command_definition(
                command, exchange_timeout=exchange_timeout
            ),
            projection_root=self.projection,
            scope_roots={"study-package-source": "."},
            **kwargs,
        )
        self.addCleanup(engine.close)
        return engine

    def test_stdin_stdout_exchange_carries_the_documented_request(self) -> None:
        self._write_command_script(
            """
import json
import os
import sys

request = json.load(sys.stdin)
print("command stderr stays off the protocol", file=sys.stderr)
json.dump(
    {
        "candidates": [
            {
                "candidate_id": "cmd-1",
                "format": "parameters",
                "spec": {
                    "protocol": request["protocol"],
                    "request_id": request["request_id"],
                    "n_candidates": request["n_candidates"],
                    "seed": request["seed"],
                    "objective": request["objective"]["primaryMetric"]["name"],
                    "study_round": request["study_state"]["round"],
                    "evidence_round": request["evidence"]["round"],
                    "has_settings": "settings" in request,
                    "has_candidate_contract": "schema" in request["candidate"].get("parameters", {}),
                    "workspace_exists": os.path.isdir(request["runtime_context"]["method_workspace"]),
                    "cwd_is_workdir": os.getcwd() == os.path.realpath(os.getcwd()) and os.path.basename(os.getcwd()) == "methods",
                    "pythonpath_has_import_root": any(
                        os.path.basename(entry) == "methods"
                        for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                    ),
                },
            }
        ],
        "method_events": [{"event": "proposed", "detail": "one"}],
    },
    sys.stdout,
)
""",
        )
        redirected: list[str] = []

        class Sink:
            def write(self, value: str) -> int:
                redirected.append(value)
                return len(value)

            def flush(self) -> None:
                return None

        engine = self._engine(user_stdout=Sink())
        request = _propose(
            "command-proposal-1",
            evidence={"round": 3},
            study_state={"round": 9},
        )
        response = engine.handle(request)
        replay = engine.handle(request)

        self.assertTrue(response["ok"], response)
        self.assertEqual(replay, response)
        spec = response["result"]["candidates"][0]["spec"]
        self.assertEqual(spec["protocol"], "optpilot.method.batch.v1")
        self.assertEqual(spec["request_id"], "command-proposal-1")
        self.assertEqual(spec["n_candidates"], 1)
        self.assertEqual(spec["seed"], 7)
        self.assertEqual(spec["objective"], "throughput")
        self.assertEqual(spec["study_round"], 9)
        self.assertEqual(spec["evidence_round"], 3)
        self.assertTrue(spec["has_settings"])
        self.assertTrue(spec["has_candidate_contract"])
        self.assertTrue(spec["workspace_exists"])
        self.assertTrue(spec["cwd_is_workdir"])
        self.assertTrue(spec["pythonpath_has_import_root"])
        joined = "".join(redirected)
        self.assertIn("command stderr stays off the protocol", joined)
        self.assertIn('"event": "proposed"', joined)

    def test_file_placeholder_exchange_reads_request_and_writes_response(self) -> None:
        self._write_command_script(
            """
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    request = json.load(handle)
print("stdout noise is not the protocol in file mode")
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(
        {
            "candidates": [
                {
                    "candidate_id": "cmd-file-1",
                    "format": "parameters",
                    "spec": {"request_id": request["request_id"]},
                }
            ]
        },
        handle,
    )
""",
        )
        noise: list[str] = []

        class Sink:
            def write(self, value: str) -> int:
                noise.append(value)
                return len(value)

            def flush(self) -> None:
                return None

        engine = self._engine(
            ["python", "method_impl.py", "{input_file}", "{output_file}"],
            user_stdout=Sink(),
        )
        response = engine.handle(_propose("command-file-proposal"))

        self.assertTrue(response["ok"], response)
        self.assertEqual(
            response["result"]["candidates"][0]["spec"]["request_id"],
            "command-file-proposal",
        )
        self.assertIn("stdout noise is not the protocol in file mode", "".join(noise))

    def test_command_failure_modes_return_method_failed_with_diagnostics(self) -> None:
        diagnostics: list[dict[str, Any]] = []
        cases = (
            ("exit", "import sys\nsys.exit(3)\n"),
            ("bad-json", "print('not json')\n"),
            ("non-object", "print('[1, 2]')\n"),
        )
        for label, source in cases:
            with self.subTest(case=label):
                self._write_command_script(source)
                engine = self._engine(diagnostic=diagnostics.append)
                response = engine.handle(_propose(f"command-{label}"))
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "method_failed")
                engine.close()
        self.assertEqual(len(diagnostics), len(cases))

    def test_hung_command_times_out_without_wedging_the_worker(self) -> None:
        self._write_command_script("import time\ntime.sleep(30)\n")
        engine = self._engine(exchange_timeout=1)
        response = engine.handle(_propose("command-timeout"))

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "method_failed")
        status = engine.handle(_status("command-status-after-timeout"))
        self.assertTrue(status["ok"], status)

    def test_overproduced_command_batch_is_rejected(self) -> None:
        self._write_command_script(
            """
import json
import sys

json.dump(
    {
        "candidates": [
            {"candidate_id": "a", "format": "parameters", "spec": {}},
            {"candidate_id": "b", "format": "parameters", "spec": {}},
        ]
    },
    sys.stdout,
)
""",
        )
        engine = self._engine()
        response = engine.handle(_propose("command-overproduced", n_candidates=1))

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "batch_overproduced")

    def test_supervised_command_worker_executes_over_the_socket(self) -> None:
        probe_path = self.root / "socket-probe.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(str(probe_path))
            except PermissionError as error:
                if error.errno == 1:
                    self.skipTest("sandbox denies AF_UNIX bind")
                raise
            finally:
                if probe_path.exists():
                    probe_path.unlink()
        self._write_command_script(
            """
import json
import sys

request = json.load(sys.stdin)
print("private command stderr", file=sys.stderr)
json.dump(
    {
        "candidates": [
            {
                "candidate_id": "socket-cmd",
                "format": "parameters",
                "spec": {"request_id": request["request_id"]},
            }
        ]
    },
    sys.stdout,
)
""",
        )
        socket_path = self.root / "command-subprocess.sock"
        diagnostic_path = self.root / "command-subprocess.log"
        initialization = RetainedBatchWorkerInit(
            run_definition=_command_definition(),
            projection_root=str(self.projection),
            scope_roots={"study-package-source": "."},
            socket_path=str(socket_path),
            diagnostic_path=str(diagnostic_path),
        )
        init_path = self.root / "command-init.json"
        init_path.write_bytes(initialization.to_bytes())
        process = subprocess.Popen(
            [sys.executable, "-m", "optpilot.retained_batch_worker", str(init_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while (
            not socket_path.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        self.assertIsNone(process.poll())
        self.assertTrue(socket_path.exists())

        proposal = unix_batch_worker_request(
            socket_path, _propose("proposal-command-socket")
        )
        shutdown = unix_batch_worker_request(
            socket_path, _shutdown("shutdown-command-socket")
        )
        process.wait(timeout=5)

        self.assertTrue(proposal["ok"], proposal)
        self.assertEqual(
            proposal["result"]["candidates"][0]["spec"]["request_id"],
            "proposal-command-socket",
        )
        self.assertTrue(shutdown["result"]["shutdown"])
        self.assertEqual(process.returncode, 0)
        self.assertIn(
            "private command stderr", diagnostic_path.read_text(encoding="utf-8")
        )

    def test_observations_are_acknowledged_without_invoking_the_command(self) -> None:
        marker = self.methods / "invoked.marker"
        self._write_command_script(
            """
import json
import pathlib
import sys

pathlib.Path("invoked.marker").write_text("ran", encoding="utf-8")
json.dump({"candidates": []}, sys.stdout)
""",
        )
        engine = self._engine()
        response = engine.handle(
            _observe(
                "command-observe-1",
                [{"candidate_id": "cmd-1", "status": "completed"}],
            )
        )

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"], {"observation_count": 1})
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()


class StdioBatchWorkerServerTest(unittest.TestCase):
    """The frame protocol over a stream that stays open.

    This is the transport a containerised method uses: the container's own
    standard input and output. The property that matters most is exclusivity --
    nothing authored code does may put bytes on the protocol stream, because a
    single stray print corrupts a frame and ends the run.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.projection = self.root / "projection"
        self.methods = self.projection / "methods"
        self.methods.mkdir(parents=True)
        (self.methods / "random.yaml").write_text("retained: true\n", encoding="utf-8")
        sys.modules.pop("method_impl", None)
        self.addCleanup(sys.modules.pop, "method_impl", None)

    def _serve(self, engine) -> tuple[Any, Any]:
        """Start the server on a pair of pipes; return (send, receive)."""

        from optpilot.retained_batch_worker import StdioBatchWorkerServer

        request_read, request_write = os.pipe()
        response_read, response_write = os.pipe()
        server = StdioBatchWorkerServer(
            engine,
            os.fdopen(request_read, "rb", buffering=0),
            os.fdopen(response_write, "wb", buffering=0),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        sender = os.fdopen(request_write, "wb", buffering=0)
        receiver = os.fdopen(response_read, "rb", buffering=0)
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)
        return sender, receiver

    @staticmethod
    def _send(stream, payload: bytes, *, declared_size: int | None = None) -> None:
        from optpilot.retained_batch_worker import _HEADER

        stream.write(_HEADER.pack(declared_size if declared_size is not None else len(payload)))
        stream.write(payload)

    @staticmethod
    def _receive(stream) -> dict[str, Any]:
        from optpilot.retained_batch_worker import _HEADER

        header = stream.read(_HEADER.size)
        assert header and len(header) == _HEADER.size, "response ended early"
        (size,) = _HEADER.unpack(header)
        payload = b""
        while len(payload) < size:
            chunk = stream.read(size - len(payload))
            assert chunk, "response payload ended early"
            payload += chunk
        return json.loads(payload.decode("utf-8"))

    def _engine(self, **kwargs: Any) -> RetainedPythonBatchEngine:
        engine = RetainedPythonBatchEngine(
            run_definition=_definition(),
            projection_root=self.projection,
            scope_roots={"study-package-source": "."},
            **kwargs,
        )
        self.addCleanup(engine.close)
        return engine

    def test_a_propose_exchange_rides_the_stream_and_shutdown_ends_it(self) -> None:
        (self.methods / "method_impl.py").write_text(
            """
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view):
        print("authored output stays off the protocol")
        return [{"candidate_id": "stdio-1", "format": "parameters", "spec": {"x": 1}}]
""",
            encoding="utf-8",
        )
        printed: list[str] = []

        class Sink:
            def write(self, value: str) -> int:
                printed.append(value)
                return len(value)

            def flush(self) -> None:
                return None

        engine = self._engine(user_stdout=Sink())
        sender, receiver = self._serve(engine)

        self._send(sender, canonical_json_bytes(_propose("stdio-exchange-1")))
        response = self._receive(receiver)
        self.assertTrue(response["ok"], response)
        self.assertEqual(
            response["result"]["candidates"][0]["candidate_id"], "stdio-1"
        )
        self.assertIn("authored output stays off the protocol", "".join(printed))

        self._send(sender, canonical_json_bytes(_shutdown()))
        stopped = self._receive(receiver)
        self.assertTrue(stopped["result"]["shutdown"])
        self.assertEqual(receiver.read(1), b"", "nothing follows shutdown")
        self.assertTrue(engine._closed)

    def test_transport_errors_answer_in_band_and_oversize_ends_the_stream(self) -> None:
        (self.methods / "method_impl.py").write_text(
            """
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view): return []
""",
            encoding="utf-8",
        )
        engine = self._engine()
        sender, receiver = self._serve(engine)

        self._send(sender, b"{")
        self.assertEqual(self._receive(receiver)["error"]["code"], "malformed_frame")
        self._send(sender, b"{} ")
        self.assertEqual(
            self._receive(receiver)["error"]["code"], "noncanonical_frame"
        )
        self._send(sender, b"", declared_size=MAX_BATCH_FRAME_BYTES + 1)
        self.assertEqual(self._receive(receiver)["error"]["code"], "frame_too_large")
        self.assertEqual(receiver.read(1), b"", "an oversized frame ends the stream")
        self.assertTrue(engine._closed)

    def test_end_of_file_closes_the_engine_quietly(self) -> None:
        (self.methods / "method_impl.py").write_text(
            """
class Method:
    def __init__(self, definition, study_spec, rng): pass
    def propose(self, n_candidates, study_state, evidence_view): return []
""",
            encoding="utf-8",
        )
        engine = self._engine()
        sender, receiver = self._serve(engine)
        sender.close()
        self.assertEqual(receiver.read(1), b"")
        for _ in range(100):
            if engine._closed:
                break
            time.sleep(0.05)
        self.assertTrue(engine._closed)

    def test_claiming_stdio_keeps_every_kind_of_authored_write_off_the_protocol(
        self,
    ) -> None:
        """The one that matters: descriptor-level writes cannot corrupt a frame.

        Redirecting Python's stdout object reroutes Python prints only; a
        subprocess or a raw descriptor write still reaches descriptor 1. After
        claim_stdio, all three land on the error stream, reading input hits
        end-of-file, and the protocol stream carries exactly one frame.
        """

        script = self.root / "probe.py"
        script.write_text(
            """
import os, subprocess, sys
from optpilot.retained_batch_worker import StdioBatchWorkerServer, _HEADER

protocol_in, protocol_out = StdioBatchWorkerServer.claim_stdio()

os.write(1, b"raw descriptor write\\n")
print("python print")
subprocess.run([sys.executable, "-c", "print('subprocess print')"], check=True)
try:
    input()
    stdin_state = "readable"
except EOFError:
    stdin_state = "eof"

payload = ('{"stdin": "' + stdin_state + '"}').encode("utf-8")
protocol_out.write(_HEADER.pack(len(payload)) + payload)
""",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        from optpilot.retained_batch_worker import _HEADER

        (size,) = _HEADER.unpack(result.stdout[: _HEADER.size])
        frame = json.loads(result.stdout[_HEADER.size : _HEADER.size + size])
        self.assertEqual(frame, {"stdin": "eof"})
        self.assertEqual(
            len(result.stdout), _HEADER.size + size,
            "the protocol stream carries exactly one frame and nothing else",
        )
        error_text = result.stderr.decode()
        for line in ("raw descriptor write", "python print", "subprocess print"):
            self.assertIn(line, error_text)
