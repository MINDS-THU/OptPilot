import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

from xdevs.models import Atomic, Coupled, Port
from xdevs.sim import Coordinator, SimulationClock

from devs_tools.devs_construct_recon.materials.devs_project.devs_utils.event_trace import (
    TRACE_RESULT_FILE,
    TRACE_SCHEMA,
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
            self.assertEqual(
                [item["record_type"] for item in partial_records],
                ["event", "event"],
            )
            simulation.exit()

            records = _read_jsonl(Path(tmp) / TRACE_RESULT_FILE)
            events = [item for item in records if item["record_type"] == "event"]
            self.assertEqual(
                [(item["component"], item["port"]) for item in events],
                [("root.a", "events"), ("root.b", "events")],
            )
            self.assertEqual([item["sequence"] for item in events], [1, 2])
            self.assertEqual([item["simulation_time"] for item in events], [1.0, 1.0])
            self.assertNotIn("root.sink", {item["component"] for item in events})
            self.assertEqual(
                records[-1],
                {
                    "schema_version": TRACE_SCHEMA,
                    "record_type": "summary",
                    "recorded_events": 2,
                    "dropped_events": 0,
                    "truncated": False,
                },
            )

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
                        "record_type": "summary",
                        "recorded_events": 0,
                        "dropped_events": 0,
                        "truncated": False,
                    }
                ],
            )

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
            self.assertGreater(footer["dropped_events"], 0)
            self.assertTrue(footer["truncated"])
            self.assertLessEqual(huge.highest_requested_index, 64)
            first_value = records[0]["value"]
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
            event = records[0]
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

    def test_component_path_parent_cycle_is_bounded(self):
        component = Coupled("loop")
        component.parent = component
        self.assertEqual(_component_path(component), "<cycle>.loop")


if __name__ == "__main__":
    unittest.main()
