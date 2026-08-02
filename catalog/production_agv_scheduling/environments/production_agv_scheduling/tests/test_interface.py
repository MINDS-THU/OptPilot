"""Focused coverage for the compiled Unity interface and local MQTT bridge."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen


ENVIRONMENT_ROOT = Path(__file__).resolve().parents[1]
if str(ENVIRONMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(ENVIRONMENT_ROOT))

from interface_runtime import (  # noqa: E402
    CandidateReplayManager,
    _BoundedPipeDrain,
)
from interface_server import create_server  # noqa: E402
from interface_worker import TelemetryRecorder  # noqa: E402
from mqtt_bridge import (  # noqa: E402
    LocalMQTTBroker,
    mqtt_publish_packet,
    mqtt_variable_integer,
)


class MQTTPacketTests(unittest.TestCase):
    def test_publish_packet_has_protocol_specific_property_layout(self) -> None:
        mqtt5 = mqtt_publish_packet("root/status", b"{}", protocol_level=5)
        mqtt311 = mqtt_publish_packet("root/status", b"{}", protocol_level=4)

        body5 = _mqtt_body(mqtt5)
        body311 = _mqtt_body(mqtt311)
        topic_length = struct.unpack("!H", body5[:2])[0]
        payload_offset = 2 + topic_length

        self.assertEqual(body5[payload_offset:], b"\x00{}")
        self.assertEqual(body311[payload_offset:], b"{}")

    def test_websocket_idle_timeout_releases_an_unresponsive_client(self) -> None:
        broker = LocalMQTTBroker(websocket_idle_timeout_seconds=0.05)
        server_connection, client_connection = socket.socketpair()
        self.addCleanup(client_connection.close)
        thread = threading.Thread(
            target=broker.serve_websocket,
            args=(server_connection,),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=1)
        broker.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(broker.client_count, 0)


class InterfaceServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            environment_root=ENVIRONMENT_ROOT,
            runtime_root=Path(self.temporary.name),
            viewer_wait_seconds=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _close_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.application.close()
        self.thread.join(timeout=2)

    def test_ready_candidate_and_dynamic_same_origin_config(self) -> None:
        ready = _get_json(self.base_url + "/ready")
        candidate = _get_json(self.base_url + "/api/candidate")
        request = Request(
            self.base_url + "/unity/StreamingAssets/MQTTBroker.json",
            headers={
                "Host": "studio.example:9443",
                "Origin": "https://studio.example:9443",
            },
        )
        with urlopen(request, timeout=5) as response:
            config = json.load(response)

        self.assertEqual(ready["status"], "ready")
        self.assertIn("create_scheduler", candidate["candidate"]["scheduler.py"])
        self.assertEqual(config["connect_mode"], {"wss": True})
        self.assertEqual(config["wss"]["host"], "studio.example")
        self.assertEqual(config["wss"]["port"], 9443)
        self.assertEqual(
            config["common_topic"]["Root_Topic_Head"],
            "optpilot_offline",
        )
        serialized = json.dumps(config).lower()
        self.assertNotIn("hivemq", serialized)
        self.assertNotIn("emqx", serialized)

    def test_unity_brotli_assets_have_browser_decompression_headers(self) -> None:
        request = Request(
            self.base_url + "/unity/Build/SimPy.wasm.unityweb",
            method="HEAD",
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers["Content-Encoding"], "br")
            self.assertEqual(response.headers.get_content_type(), "application/wasm")
            self.assertGreater(int(response.headers["Content-Length"]), 1_000_000)

    def test_mqtt5_websocket_connect_subscribe_and_local_publish(self) -> None:
        connection = socket.create_connection(
            ("127.0.0.1", self.server.server_port),
            timeout=5,
        )
        self.addCleanup(connection.close)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /mqtt HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.server_port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: mqtt\r\n"
            "\r\n"
        )
        connection.sendall(request.encode("ascii"))
        response = _receive_until(connection, b"\r\n\r\n")
        self.assertIn(b"101 Switching Protocols", response)
        self.assertIn(b"Sec-WebSocket-Protocol: mqtt", response)

        connect_body = (
            b"\x00\x04MQTT"
            b"\x05"
            b"\x02"
            b"\x00\x1e"
            b"\x00"
            b"\x00\x0bviewer-test"
        )
        _send_client_binary(
            connection,
            b"\x10" + mqtt_variable_integer(len(connect_body)) + connect_body,
        )
        self.assertEqual(_receive_server_binary(connection), b"\x20\x03\x00\x00\x00")

        topic_filter = b"optpilot_offline/#"
        subscribe_body = (
            b"\x00\x01"
            b"\x00"
            + struct.pack("!H", len(topic_filter))
            + topic_filter
            + b"\x00"
        )
        _send_client_binary(
            connection,
            b"\x82" + mqtt_variable_integer(len(subscribe_body)) + subscribe_body,
        )
        self.assertEqual(_receive_server_binary(connection), b"\x90\x04\x00\x01\x00\x00")
        self.assertTrue(
            self.server.application.broker.has_subscriber(
                "optpilot_offline/kpi/status"
            )
        )

        delivered = self.server.application.broker.publish(
            "optpilot_offline/kpi/status",
            '{"timestamp":1}',
        )
        publish = _receive_server_binary(connection)
        body = _mqtt_body(publish)
        topic_length = struct.unpack("!H", body[:2])[0]
        self.assertEqual(body[2 : 2 + topic_length], b"optpilot_offline/kpi/status")
        self.assertEqual(body[2 + topic_length], 0)
        self.assertEqual(body[3 + topic_length :], b'{"timestamp":1}')
        self.assertEqual(delivered, 1)

    def test_browser_websocket_origin_must_be_registered_by_config(self) -> None:
        rejected = _open_websocket(
            self.server.server_port,
            origin="https://unregistered.example",
        )
        self.assertIn(b"403 Forbidden", rejected)

        cross_site_request = Request(
            self.base_url + "/unity/StreamingAssets/MQTTBroker.json",
            headers={
                "Origin": "https://unregistered.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        with urlopen(cross_site_request, timeout=5) as response:
            json.load(response)
        still_rejected = _open_websocket(
            self.server.server_port,
            origin="https://unregistered.example",
        )
        self.assertIn(b"403 Forbidden", still_rejected)

        poisoning_request = Request(
            self.base_url + "/unity/StreamingAssets/MQTTBroker.json",
            headers={"Origin": "https://unregistered.example"},
        )
        with urlopen(poisoning_request, timeout=5) as response:
            poisoned_config = json.load(response)
        self.assertEqual(poisoned_config["ws"]["host"], "127.0.0.1")
        still_rejected = _open_websocket(
            self.server.server_port,
            origin="https://unregistered.example",
        )
        self.assertIn(b"403 Forbidden", still_rejected)

        local_origin = f"http://127.0.0.1:{self.server.server_port}"
        request = Request(
            self.base_url + "/unity/StreamingAssets/MQTTBroker.json",
            headers={"Origin": local_origin},
        )
        with urlopen(request, timeout=5) as response:
            json.load(response)

        accepted = _open_websocket(
            self.server.server_port,
            origin=local_origin,
        )
        self.assertIn(b"101 Switching Protocols", accepted)


class CandidateReplayTests(unittest.TestCase):
    def test_initial_candidate_runs_offline_and_records_visual_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker = LocalMQTTBroker()
            manager = CandidateReplayManager(
                environment_root=ENVIRONMENT_ROOT,
                broker=broker,
                candidate_root=ENVIRONMENT_ROOT / "initial",
                runtime_root=Path(temporary),
                viewer_wait_seconds=0,
            )
            self.addCleanup(manager.close)
            candidate = manager.candidate_payload()["candidate"]
            state = manager.start(
                {
                    "candidate": candidate,
                    "options": {
                        "disable_faults": True,
                        "replay_speed": 1,
                        "seed": 123,
                        "simulation_horizon": 2,
                        "time_step": 0.5,
                    },
                }
            )
            self.assertEqual(state["status"], "running")

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = manager.state()
                if state["status"] not in {
                    "running",
                    "waiting_for_viewer",
                    "replaying",
                }:
                    break
                time.sleep(0.05)

            self.assertEqual(state["status"], "completed", state.get("error"))
            self.assertGreater(state["result"]["event_count"], 0)
            self.assertGreater(state["events_published"], 0)
            self.assertIn("total_score", state["result"]["kpi"])
            self.assertFalse(state["result"]["events_truncated"])

    def test_visual_telemetry_clears_agv_before_destination_claims_product(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / "events.jsonl"
            recorder = TelemetryRecorder(events)
            agv_topic = "optpilot_offline/line1/agv/AGV_1/status"
            destination_topic = (
                "optpilot_offline/line1/station/StationA/status"
            )
            recorder.publish(
                agv_topic,
                json.dumps(
                    {
                        "timestamp": 30.5,
                        "source_id": "AGV_1",
                        "status": "interacting",
                        "payload": ["product-1"],
                    }
                ),
                0,
                False,
            )
            recorder.publish(
                destination_topic,
                json.dumps(
                    {
                        "timestamp": 30.5,
                        "source_id": "StationA",
                        "status": "idle",
                        "buffer": ["product-1"],
                    }
                ),
                0,
                False,
            )
            recorder.close()

            records = [
                json.loads(line)
                for line in events.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [record["event_sequence"] for record in records],
                [0, 1, 2],
            )
            self.assertEqual(
                [record["topic"] for record in records],
                [agv_topic, agv_topic, destination_topic],
            )
            self.assertEqual(
                json.loads(records[0]["payload"])["payload"],
                ["product-1"],
            )
            self.assertEqual(json.loads(records[1]["payload"])["payload"], [])
            self.assertEqual(
                json.loads(records[2]["payload"])["buffer"],
                ["product-1"],
            )

    def test_replay_rejects_speeds_that_the_compiled_viewer_cannot_match(
        self,
    ) -> None:
        broker = _RecordingBroker(subscribed=True)
        manager = _replay_only_manager(broker, viewer_wait_seconds=0)
        manager._last_events = Path("unused.jsonl")
        manager._active_locked = lambda: False

        with self.assertRaisesRegex(
            ValueError,
            "replay_speed must be between 1 and 1",
        ):
            manager.replay({"replay_speed": 4})

    def test_replay_waits_for_a_matching_mqtt_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.jsonl"
            events.write_text(
                json.dumps(
                    {
                        "payload": "{}",
                        "simulation_time": 0,
                        "topic": "optpilot_offline/kpi/status",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            broker = _RecordingBroker(subscribed=False)
            manager = _replay_only_manager(broker, viewer_wait_seconds=1)
            stop_event = threading.Event()
            thread = threading.Thread(
                target=manager._replay_events,
                args=("replay-test", events, 100.0, stop_event),
                daemon=True,
            )
            thread.start()
            time.sleep(0.08)
            self.assertEqual(broker.payloads, [])

            broker.subscribed = True
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(broker.payloads), 1)

    def test_stopped_replay_keeps_its_original_stop_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.jsonl"
            records = [
                {
                    "payload": "{}",
                    "simulation_time": timestamp,
                    "topic": "optpilot_offline/kpi/status",
                }
                for timestamp in (0, 100)
            ]
            events.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            broker = _BlockingRecordingBroker()
            manager = _replay_only_manager(broker, viewer_wait_seconds=0)
            stop_event = threading.Event()
            manager._stop_event = stop_event
            thread = threading.Thread(
                target=manager._replay_events,
                args=("replay-test", events, 1.0, stop_event),
                daemon=True,
            )
            thread.start()
            self.assertTrue(broker.first_publish.wait(timeout=1))

            stop_event.set()
            manager._stop_event = threading.Event()
            broker.release_publish.set()
            thread.join(timeout=1)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(broker.payloads), 1)

    def test_replay_does_not_shorten_long_visual_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / "events.jsonl"
            records = [
                {
                    "event_sequence": index,
                    "payload": json.dumps({"timestamp": timestamp}),
                    "simulation_time": timestamp,
                    "topic": "optpilot_offline/kpi/status",
                }
                for index, timestamp in enumerate((0, 3))
            ]
            events.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            broker = _RecordingBroker(subscribed=True)
            manager = _replay_only_manager(broker, viewer_wait_seconds=0)
            stop_event = _RecordingWaitEvent()

            manager._replay_events(
                "replay-test",
                events,
                1.0,
                stop_event,
            )

            self.assertEqual(stop_event.delays, [3.0])
            self.assertEqual(len(broker.payloads), 2)

    def test_worker_stderr_drain_retains_only_its_bounded_tail(self) -> None:
        drain = _BoundedPipeDrain(
            io.BytesIO(b"discarded-" + b"x" * 100 + b"-tail"),
            limit=12,
        )
        drain.join(timeout=1)

        self.assertEqual(drain.text(), "xxxxxxx-tail")

    def test_old_monitor_keeps_its_exact_worker_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker = _RecordingBroker(subscribed=True)
            manager = _replay_only_manager(broker, viewer_wait_seconds=0)
            old_process = _FakeProcess()
            new_process = _FakeProcess()
            manager._process = new_process
            stopped = threading.Event()
            stopped.set()

            manager._monitor_run(
                "old-run",
                root / "old-events.jsonl",
                root / "old-result.json",
                1.0,
                stopped,
                _FakeDrain(),
                old_process,
            )

            self.assertEqual(old_process.wait_count, 1)
            self.assertEqual(new_process.wait_count, 0)
            self.assertIs(manager._process, new_process)


def _get_json(url: str) -> dict:
    with urlopen(url, timeout=5) as response:
        return json.load(response)


def _open_websocket(port: int, *, origin: str | None = None) -> bytes:
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        origin_header = f"Origin: {origin}\r\n" if origin is not None else ""
        request = (
            "GET /mqtt HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: mqtt\r\n"
            f"{origin_header}"
            "\r\n"
        )
        connection.sendall(request.encode("ascii"))
        return _receive_until(connection, b"\r\n\r\n")
    finally:
        connection.close()


class _RecordingBroker:
    client_count = 1

    def __init__(self, *, subscribed: bool) -> None:
        self.subscribed = subscribed
        self.payloads: list[tuple[str, str]] = []

    def has_subscriber(self, _topic: str) -> bool:
        return self.subscribed

    def publish(self, topic: str, payload: str, *, retain: bool) -> int:
        self.payloads.append((topic, payload))
        return 1


class _BlockingRecordingBroker(_RecordingBroker):
    def __init__(self) -> None:
        super().__init__(subscribed=True)
        self.first_publish = threading.Event()
        self.release_publish = threading.Event()

    def publish(self, topic: str, payload: str, *, retain: bool) -> int:
        result = super().publish(topic, payload, retain=retain)
        if len(self.payloads) == 1:
            self.first_publish.set()
            self.release_publish.wait(timeout=1)
        return result


class _RecordingWaitEvent:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def is_set(self) -> bool:
        return False

    def wait(self, delay: float) -> bool:
        self.delays.append(delay)
        return False


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode = 0
        self.wait_count = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_count += 1
        return self.returncode


class _FakeDrain:
    def join(self, *, timeout: float) -> None:
        return

    def text(self) -> str:
        return ""


def _replay_only_manager(
    broker: _RecordingBroker,
    *,
    viewer_wait_seconds: float,
) -> CandidateReplayManager:
    manager = object.__new__(CandidateReplayManager)
    manager.broker = broker
    manager.viewer_wait_seconds = viewer_wait_seconds
    manager._lock = threading.RLock()
    manager._stop_event = threading.Event()
    manager._state = {
        "events_published": 0,
        "message": "",
        "run_id": "replay-test",
        "status": "waiting_for_viewer",
    }
    return manager


def _mqtt_body(packet: bytes) -> bytes:
    multiplier = 1
    remaining = 0
    offset = 1
    while True:
        encoded = packet[offset]
        offset += 1
        remaining += (encoded & 0x7F) * multiplier
        if not encoded & 0x80:
            break
        multiplier *= 128
    if len(packet) != offset + remaining:
        raise AssertionError("MQTT packet length mismatch")
    return packet[offset:]


def _send_client_binary(connection: socket.socket, payload: bytes) -> None:
    mask = b"\x11\x22\x33\x44"
    if len(payload) <= 125:
        header = bytes((0x82, 0x80 | len(payload)))
    else:
        header = bytes((0x82, 0x80 | 126)) + struct.pack("!H", len(payload))
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    connection.sendall(header + mask + masked)


def _receive_server_binary(connection: socket.socket) -> bytes:
    first, second = _receive_exact(connection, 2)
    if first & 0x0F != 2 or second & 0x80:
        raise AssertionError("Expected one unmasked server binary frame")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _receive_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _receive_exact(connection, 8))[0]
    return _receive_exact(connection, length)


def _receive_until(connection: socket.socket, marker: bytes) -> bytes:
    result = bytearray()
    while marker not in result:
        result.extend(connection.recv(4096))
    return bytes(result)


def _receive_exact(connection: socket.socket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = connection.recv(length - len(result))
        if not chunk:
            raise ConnectionError("socket closed")
        result.extend(chunk)
    return bytes(result)


if __name__ == "__main__":
    unittest.main()
