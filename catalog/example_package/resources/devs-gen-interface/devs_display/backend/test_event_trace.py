import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

from xdevs.models import Atomic, Coupled, Port
from xdevs.sim import Coordinator, SimulationClock

from devs_tools.devs_construct_recon.materials.devs_project.devs_utils.event_trace import (
    TRACE_CAPABILITIES,
    TRACE_RESULT_FILE,
    TRACE_SCHEMA,
    _component_id,
    _component_path,
    attach_event_trace,
)


class _Emitter(Atomic):
    def __init__(self, name, payloads):
        super().__init__(name)
        self.payloads = payloads
        self.events = Port(name="events")
        self.add_out_port(self.events)

    def initialize(self):
        self.hold_in("emit", 1.0)

    def deltint(self):
        self.passivate()

    def deltext(self, e):
        self.continuef(e)

    def lambdaf(self):
        for payload in self.payloads:
            self.events.add(payload)

    def exit(self):
        pass


class _Sink(Atomic):
    def __init__(self, name):
        super().__init__(name)
        self.events = Port(name="events")
        self.add_in_port(self.events)

    def initialize(self):
        self.passivate()

    def deltint(self):
        self.passivate()

    def deltext(self, _e):
        self.passivate()

    def lambdaf(self):
        pass

    def exit(self):
        pass


class _DelayedEmitter(Atomic):
    """Perform many state transitions before one late output event."""

    def __init__(self, name, transitions):
        super().__init__(name)
        self.transitions = transitions
        self.step = 0
        self.events = Port(name="events")
        self.add_out_port(self.events)

    def initialize(self):
        self.step = 0
        self.hold_in("waiting", 1.0)

    def deltint(self):
        self.step += 1
        if self.step < self.transitions:
            self.hold_in("waiting", 1.0)
        else:
            self.passivate()

    def deltext(self, e):
        self.continuef(e)

    def lambdaf(self):
        if self.step == self.transitions - 1:
            self.events.add({"late": True})

    def exit(self):
        pass


class _HugeSequence(Sequence):
    """Expose a huge logical value without allocating it in the test."""

    def __init__(self):
        self.highest_requested_index = -1

    def __len__(self):
        return 10_000_000

    def __getitem__(self, index):
        if index < 0:
            raise IndexError(index)
        self.highest_requested_index = max(self.highest_requested_index, index)
        if index >= len(self):
            raise IndexError(index)
        return index


class _ThrowingMapping(Mapping):
    """A hostile user value whose contents cannot be inspected safely."""

    def __getitem__(self, _key):
        raise RuntimeError("private payload failure")

    def __iter__(self):
        raise RuntimeError("private payload failure")

    def __len__(self):
        return 1


class _ThrowingLabel:
    def __str__(self):
        raise RuntimeError("private label failure")


class _ProjectedSink(_Sink):
    def trace_state(self):
        return {"status": "idle", "served": 3}


class _CountingSink(_Sink):
    def __init__(self, name):
        super().__init__(name)
        self.received = 0

    def initialize(self):
        self.received = 0
        self.passivate()

    def deltext(self, _e):
        self.received += sum(1 for _ in self.events.values)
        self.passivate()

    def trace_state(self):
        return {"received": self.received}


class _TwoCycleEmitter(_Emitter):
    """Emit twice through consecutive zero-delay coordinator cycles."""

    def __init__(self, name):
        super().__init__(name, [])

    def initialize(self):
        self.hold_in("first", 0.0)

    def deltint(self):
        if self.phase == "first":
            self.hold_in("second", 0.0)
        else:
            self.passivate()

    def lambdaf(self):
        self.events.add({"phase": self.phase})


class _HostileProjectionSink(_Sink):
    def trace_state(self):
        return _ThrowingMapping()


class _ThrowingProjectionSink(_Sink):
    def trace_state(self):
        raise RuntimeError("private projection failure")


def _read_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class EventTraceTests(unittest.TestCase):
    def test_atomic_output_events_are_deterministic_and_not_duplicated_at_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Coupled("root")
            emitter_b = _Emitter("b", [{"item": "second"}])
            emitter_a = _Emitter("a", [{"item": "first"}])
            sink = _Sink("sink")
            # Reverse insertion proves trace ordering does not depend on model
            # traversal order.
            root.add_component(emitter_b)
            root.add_component(emitter_a)
            root.add_component(sink)
            root.add_coupling(emitter_a.events, sink.events)
            root.add_coupling(emitter_b.events, sink.events)

            simulation = Coordinator(root, SimulationClock())
            attach_event_trace(simulation, root, result_root=tmp)
            simulation.initialize()
            simulation.simulate_time(2.0)

            # Completed event rows are flushed at each timestamp. If a managed
            # run is timed out or stopped before ``exit``, useful completed rows
            # remain readable even though the final summary is necessarily absent.
            partial_records = _read_jsonl(Path(tmp) / TRACE_RESULT_FILE)
            self.assertEqual(partial_records[0]["record_type"], "header")
            self.assertEqual(
                partial_records[0]["capabilities"],
                list(TRACE_CAPABILITIES),
            )
            self.assertNotIn(
                "summary", {item["record_type"] for item in partial_records}
            )
            simulation.exit()

            records = _read_jsonl(Path(tmp) / TRACE_RESULT_FILE)
            events = [item for item in records if item["record_type"] == "event"]
            self.assertEqual(
                [(item["component"], item["port"]) for item in events],
                [("root.a", "events"), ("root.b", "events")],
            )
            self.assertEqual(
                [item["component_id"] for item in events],
                ["root/a", "root/b"],
            )
            self.assertEqual(
                [item["event_kind"] for item in events],
                ["output", "output"],
            )
            self.assertEqual([item["sequence"] for item in events], [1, 2])
            self.assertEqual([item["simulation_time"] for item in events], [1.0, 1.0])
            self.assertEqual([item["observation_cycle"] for item in events], [1, 1])
            self.assertNotIn("root.sink", {item["component"] for item in events})

            states = [item for item in records if item["record_type"] == "state"]
            initialized = [
                item for item in states if item["observation"] == "initialized"
            ]
            transitioned = [
                item
                for item in states
                if item["observation"] == "post_transition"
            ]
            expected_ids = ["root/a", "root/b", "root/sink"]
            self.assertEqual(
                [item["component_id"] for item in initialized], expected_ids
            )
            self.assertEqual(
                [item["simulation_time"] for item in initialized],
                [0.0, 0.0, 0.0],
            )
            self.assertEqual(
                [item["observation_cycle"] for item in initialized],
                [0, 0, 0],
            )
            self.assertEqual(
                [item["component_id"] for item in transitioned], expected_ids
            )
            self.assertEqual(
                [item["observation_cycle"] for item in transitioned],
                [1, 1, 1],
            )
            self.assertTrue(
                all(item["phase"] == "passive" for item in transitioned)
            )
            self.assertTrue(
                all(item["sigma"] is None for item in transitioned)
            )
            self.assertTrue(
                all(item["sigma_infinite"] is True for item in transitioned)
            )
            self.assertTrue(all("domain_state" not in item for item in states))

            footer = records[-1]
            self.assertEqual(footer["schema_version"], TRACE_SCHEMA)
            self.assertEqual(footer["record_type"], "summary")
            self.assertEqual(footer["recorded_events"], 2)
            self.assertEqual(footer["dropped_events"], 0)
            self.assertEqual(footer["recorded_states"], 6)
            self.assertEqual(footer["dropped_states"], 0)
            self.assertEqual(footer["recorded_records"], 8)
            self.assertEqual(footer["dropped_records"], 0)
            self.assertFalse(footer["truncated"])

    def test_empty_model_still_writes_a_trace_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Coupled("empty")
            simulation = Coordinator(root, SimulationClock())
            attach_event_trace(simulation, root, result_root=tmp)
            simulation.initialize()
            simulation.exit()

            self.assertEqual(
                _read_jsonl(Path(tmp) / TRACE_RESULT_FILE),
                [
                    {
                        "schema_version": TRACE_SCHEMA,
                        "record_type": "header",
                        "capabilities": list(TRACE_CAPABILITIES),
                        "component_id_format": "root/<child-instance>/...",
                        "legacy_event_schema": "devs.event-trace.v1",
                    },
                    {
                        "schema_version": TRACE_SCHEMA,
                        "record_type": "summary",
                        "recorded_events": 0,
                        "dropped_events": 0,
                        "recorded_states": 0,
                        "dropped_states": 0,
                        "recorded_records": 0,
                        "dropped_records": 0,
                        "events_truncated": False,
                        "states_truncated": False,
                        "truncated": False,
                    }
                ],
            )

    def test_initial_state_uses_the_coordinator_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Coupled("root")
            root.add_component(_Sink("sink"))
            simulation = Coordinator(root, SimulationClock(7.5))
            attach_event_trace(simulation, root, result_root=tmp)
            simulation.initialize()
            simulation.exit()

            initialized = next(
                item
                for item in _read_jsonl(Path(tmp) / TRACE_RESULT_FILE)
                if item.get("record_type") == "state"
                and item.get("observation") == "initialized"
            )
            self.assertEqual(initialized["simulation_time"], 7.5)

    def test_state_budget_preserves_a_late_output_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Coupled("root")
            root.add_component(_DelayedEmitter("source", transitions=40))
            simulation = Coordinator(root, SimulationClock())
            attach_event_trace(
                simulation,
                root,
                result_root=tmp,
                max_bytes=4096,
            )
            simulation.initialize()
            simulation.simulate_time(41.0)
            simulation.exit()

            records = _read_jsonl(Path(tmp) / TRACE_RESULT_FILE)
            events = [
                item for item in records if item.get("record_type") == "event"
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["value"], {"late": True})
            footer = records[-1]
            self.assertEqual(footer["recorded_events"], 1)
            self.assertEqual(footer["dropped_events"], 0)
            self.assertFalse(footer["events_truncated"])
            self.assertGreater(footer["dropped_states"], 0)
            self.assertTrue(footer["states_truncated"])
            self.assertTrue(footer["truncated"])

    def test_trace_and_payload_are_bounded_and_json_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            huge = _HugeSequence()
            cyclic = {"not_finite": float("nan"), "binary": b"abc"}
            cyclic["self"] = cyclic
            payloads = [huge, cyclic] + ["x" * 10_000 for _ in range(100)]
            root = Coupled("root")
            root.add_component(_Emitter("source", payloads))
            simulation = Coordinator(root, SimulationClock())
            attach_event_trace(
                simulation,
                root,
                result_root=tmp,
                max_bytes=2048,
            )
            simulation.initialize()
            simulation.simulate_time(2.0)
            simulation.exit()

            trace_path = Path(tmp) / TRACE_RESULT_FILE
            self.assertLessEqual(trace_path.stat().st_size, 2048)
            records = _read_jsonl(trace_path)
            footer = records[-1]
            self.assertEqual(footer["record_type"], "summary")
            self.assertGreater(footer["dropped_records"], 0)
            self.assertTrue(footer["truncated"])
            self.assertLessEqual(huge.highest_requested_index, 64)
            first_value = next(
                item["value"]
                for item in records
                if item["record_type"] == "event"
            )
            self.assertEqual(first_value[-1], {"truncated": True})
            # Parsing every line with the strict stdlib decoder proves NaN and
            # custom Python values did not leak into the JSONL syntax.
            self.assertTrue(all(item["schema_version"] == TRACE_SCHEMA for item in records))

    def test_hostile_payload_and_labels_do_not_fail_the_simulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Coupled("root")
            emitter = _Emitter("source", [_ThrowingMapping()])
            root.add_component(emitter)
            emitter.name = _ThrowingLabel()
            emitter.events.name = "p" * 10_000

            simulation = Coordinator(root, SimulationClock())
            attach_event_trace(simulation, root, result_root=tmp)
            simulation.initialize()
            simulation.simulate_time(2.0)
            simulation.exit()

            trace_path = Path(tmp) / TRACE_RESULT_FILE
            text = trace_path.read_text(encoding="utf-8")
            records = _read_jsonl(trace_path)
            event = next(
                item for item in records if item["record_type"] == "event"
            )
            self.assertEqual(event["record_type"], "event")
            self.assertEqual(
                event["value"],
                {
                    "type": f"{__name__}._ThrowingMapping",
                    "unavailable": True,
                },
            )
            self.assertEqual(
                event["component"],
                f"root.<{__name__}._ThrowingLabel>",
            )
            self.assertLessEqual(len(event["port"]), 257)
            self.assertTrue(event["port"].endswith("…"))
            self.assertNotIn("private payload failure", text)
            self.assertNotIn("private label failure", text)
            self.assertEqual(records[-1]["recorded_events"], 1)
            self.assertEqual(records[-1]["dropped_events"], 0)

    def test_explicit_domain_state_is_opt_in_bounded_and_failure_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Coupled("root")
            emitter = _Emitter("source", [{"customer": 1}])
            projected = _ProjectedSink("projected")
            hostile = _HostileProjectionSink("hostile")
            throwing = _ThrowingProjectionSink("throwing")
            for component in (emitter, projected, hostile, throwing):
                root.add_component(component)
            for sink in (projected, hostile, throwing):
                root.add_coupling(emitter.events, sink.events)

            simulation = Coordinator(root, SimulationClock())
            attach_event_trace(simulation, root, result_root=tmp)
            simulation.initialize()
            simulation.simulate_time(2.0)
            simulation.exit()

            records = _read_jsonl(Path(tmp) / TRACE_RESULT_FILE)
            states = {
                item["component_id"]: item
                for item in records
                if item.get("record_type") == "state"
                and item.get("observation") == "post_transition"
            }
            self.assertNotIn("domain_state", states["root/source"])
            self.assertEqual(
                states["root/projected"]["domain_state"],
                {"served": 3, "status": "idle"},
            )
            self.assertEqual(
                states["root/hostile"]["domain_state"],
                {
                    "type": f"{__name__}._ThrowingMapping",
                    "unavailable": True,
                },
            )
            self.assertEqual(
                states["root/throwing"]["domain_state"],
                {"unavailable": True},
            )
            trace_text = (Path(tmp) / TRACE_RESULT_FILE).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("private payload failure", trace_text)
            self.assertNotIn("private projection failure", trace_text)

    def test_recipient_projection_records_a_before_and_after_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Coupled("root")
            emitter = _Emitter("source", [{"customer": 1}])
            sink = _CountingSink("sink")
            root.add_component(emitter)
            root.add_component(sink)
            root.add_coupling(emitter.events, sink.events)

            simulation = Coordinator(root, SimulationClock())
            attach_event_trace(simulation, root, result_root=tmp)
            simulation.initialize()
            simulation.simulate_time(2.0)
            simulation.exit()

            records = _read_jsonl(Path(tmp) / TRACE_RESULT_FILE)
            sink_states = [
                item
                for item in records
                if item.get("record_type") == "state"
                and item.get("component_id") == "root/sink"
            ]
            self.assertEqual(
                [item["domain_state"] for item in sink_states],
                [{"received": 0}, {"received": 1}],
            )
            event = next(
                item for item in records if item.get("record_type") == "event"
            )
            self.assertEqual(event["observation_cycle"], 1)
            self.assertEqual(sink_states[-1]["observation_cycle"], 1)

    def test_zero_delay_transitions_at_the_same_time_have_distinct_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Coupled("root")
            root.add_component(_TwoCycleEmitter("source"))
            simulation = Coordinator(root, SimulationClock())
            attach_event_trace(simulation, root, result_root=tmp)
            simulation.initialize()
            simulation.simulate_time(1.0)
            simulation.exit()

            events = [
                item
                for item in _read_jsonl(Path(tmp) / TRACE_RESULT_FILE)
                if item.get("record_type") == "event"
            ]
            self.assertEqual(
                [item["simulation_time"] for item in events],
                [0.0, 0.0],
            )
            self.assertEqual(
                [item["observation_cycle"] for item in events],
                [1, 2],
            )

    def test_component_path_parent_cycle_is_bounded(self):
        component = Coupled("loop")
        component.parent = component
        self.assertEqual(_component_path(component), "<cycle>.loop")
        self.assertEqual(_component_id(component), "root/<cycle>/loop")


if __name__ == "__main__":
    unittest.main()
