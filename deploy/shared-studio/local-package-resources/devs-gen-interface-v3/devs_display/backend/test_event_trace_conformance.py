"""Trace-conformance validation: real generated traces pass, tampering fails."""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from devs_tools.devs_construct_recon.tools.simulation.event_trace_conformance import (
    TRACE_SCHEMA,
    validate_event_trace_lines,
    validate_event_trace_text,
)

_MATERIALS = (
    Path(__file__).resolve().parents[2]
    / "devs_tools"
    / "devs_construct_recon"
    / "materials"
)

try:  # The real writer needs the simulation kernel.
    import xdevs  # noqa: F401

    _HAS_XDEVS = True
except ImportError:
    _HAS_XDEVS = False


def _real_trace_lines(tmp_path: Path) -> list[str]:
    """Produce a genuine trace by simulating a minimal model with the writer."""

    if str(_MATERIALS) not in sys.path:
        sys.path.insert(0, str(_MATERIALS))
    for name in [m for m in sys.modules if m.startswith("devs_project")]:
        del sys.modules[name]
    event_trace = importlib.import_module("devs_project.devs_utils.event_trace")

    from xdevs.models import Atomic, Coupled, Port
    from xdevs.sim import Coordinator

    class Ticker(Atomic):
        def __init__(self, name):
            super().__init__(name)
            self.out = Port(int, "out")
            self.add_out_port(self.out)
            self.ticks = 0
            self.sigma = 1.0
            self.phase = "active"

        def initialize(self):
            self.sigma = 1.0

        def deltint(self):
            self.ticks += 1
            self.sigma = 1.0 if self.ticks < 3 else float("inf")

        def deltext(self, e):
            pass

        def lambdaf(self):
            self.out.add(self.ticks)

        def exit(self):
            pass

    class Root(Coupled):
        def __init__(self):
            super().__init__("Root")
            self.add_component(Ticker("ticker"))

    model = Root()
    simulator = Coordinator(model)
    trace = event_trace.attach_event_trace(
        simulator, model, result_root=tmp_path
    )
    assert trace is not None
    simulator.initialize()
    simulator.simulate_time(10.0)
    simulator.exit()
    trace_path = tmp_path / "event_trace.jsonl"
    return trace_path.read_text(encoding="utf-8").splitlines()


class RealTraceConformanceTest(unittest.TestCase):
    @unittest.skipUnless(_HAS_XDEVS, "xdevs runtime is not installed")
    def test_generated_trace_is_conformant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            lines = _real_trace_lines(Path(tmp_dir))
            self.assertGreaterEqual(len(lines), 2)
            self.assertEqual(validate_event_trace_lines(lines), ())

    @unittest.skipUnless(_HAS_XDEVS, "xdevs runtime is not installed")
    def test_tampered_real_trace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            lines = _real_trace_lines(Path(tmp_dir))
            without_footer = lines[:-1]
            self.assertTrue(validate_event_trace_lines(without_footer))
            wrong_counts = list(lines)
            footer = json.loads(wrong_counts[-1])
            footer["recorded_records"] = footer["recorded_records"] + 5
            wrong_counts[-1] = json.dumps(footer)
            self.assertTrue(
                any(
                    "recorded_records" in error
                    for error in validate_event_trace_lines(wrong_counts)
                )
            )


def _synthetic_trace() -> list[str]:
    rows = [
        {"schema_version": TRACE_SCHEMA, "record_type": "header"},
        {
            "schema_version": TRACE_SCHEMA,
            "record_type": "event",
            "event_kind": "output",
            "sequence": 1,
            "record_sequence": 1,
            "simulation_time": 0.5,
            "observation_cycle": 0,
            "component_id": "root/ticker",
            "component": "Root.ticker",
            "port": "out",
            "value": 1,
        },
        {
            "schema_version": TRACE_SCHEMA,
            "record_type": "state",
            "observation": "post_transition",
            "sequence": 1,
            "record_sequence": 2,
            "simulation_time": 0.5,
            "observation_cycle": 0,
            "component_id": "root/ticker",
            "component": "Root.ticker",
            "phase": "active",
            "sigma": 1.0,
            "sigma_infinite": False,
        },
        {
            "schema_version": TRACE_SCHEMA,
            "record_type": "summary",
            "recorded_events": 1,
            "dropped_events": 0,
            "recorded_states": 1,
            "dropped_states": 0,
            "recorded_records": 2,
            "dropped_records": 0,
            "events_truncated": False,
            "states_truncated": False,
            "truncated": False,
        },
    ]
    return [json.dumps(row) for row in rows]


class SyntheticTraceConformanceTest(unittest.TestCase):
    def test_reference_shape_passes(self) -> None:
        self.assertEqual(validate_event_trace_lines(_synthetic_trace()), ())

    def test_text_helper_matches(self) -> None:
        self.assertEqual(
            validate_event_trace_text("\n".join(_synthetic_trace())), ()
        )

    def test_missing_header_is_rejected(self) -> None:
        errors = validate_event_trace_lines(_synthetic_trace()[1:])
        self.assertTrue(any("header" in error for error in errors))

    def test_non_monotonic_record_sequence_is_rejected(self) -> None:
        lines = _synthetic_trace()
        state = json.loads(lines[2])
        state["record_sequence"] = 1
        lines[2] = json.dumps(state)
        errors = validate_event_trace_lines(lines)
        self.assertTrue(any("strictly increasing" in error for error in errors))

    def test_negative_time_is_rejected(self) -> None:
        lines = _synthetic_trace()
        event = json.loads(lines[1])
        event["simulation_time"] = -1.0
        lines[1] = json.dumps(event)
        errors = validate_event_trace_lines(lines)
        self.assertTrue(any("simulation_time" in error for error in errors))

    def test_unknown_record_type_is_rejected(self) -> None:
        lines = _synthetic_trace()
        lines.insert(2, json.dumps({"record_type": "mystery"}))
        errors = validate_event_trace_lines(lines)
        self.assertTrue(any("record_type" in error for error in errors))

    def test_invalid_json_line_is_rejected(self) -> None:
        lines = _synthetic_trace()
        lines.insert(1, "{not json")
        errors = validate_event_trace_lines(lines)
        self.assertTrue(any("not valid JSON" in error for error in errors))


class AssessTraceConformanceTest(unittest.TestCase):
    def test_missing_trace_is_not_an_error(self) -> None:
        from . import simulation_execution as se

        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertEqual(se.assess_trace_conformance(tmp_dir), ())

    def test_conformant_and_broken_traces_are_distinguished(self) -> None:
        from . import simulation_execution as se

        with tempfile.TemporaryDirectory() as tmp_dir:
            trace_path = Path(tmp_dir) / "event_trace.jsonl"
            trace_path.write_text(
                "\n".join(_synthetic_trace()) + "\n", encoding="utf-8"
            )
            self.assertEqual(se.assess_trace_conformance(tmp_dir), ())
            trace_path.write_text(
                "\n".join(_synthetic_trace()[1:]) + "\n", encoding="utf-8"
            )
            errors = se.assess_trace_conformance(tmp_dir)
            self.assertTrue(any("header" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
