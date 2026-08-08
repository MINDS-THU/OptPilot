"""devs.simulation.v2 metrics: static declaration, derivation, validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from devs_tools.devs_construct_recon.tools.simulation.result_summary_contract import (
    declared_metrics,
)

from . import simulation_execution as se


_REFERENCE_RUNNER = (
    Path(__file__).resolve().parents[2]
    / "devs_tools"
    / "devs_construct_recon"
    / "materials"
    / "devs_project"
    / "runner_example.py"
).read_text(encoding="utf-8")

_CALLSITE_RUNNER = _REFERENCE_RUNNER.replace(
    "metrics={},",
    'metrics={"completed_items": completed, "utilization": busy_share},',
)

_EXPLICIT_RUNNER = (
    'OPTPILOT_METRICS = {\n'
    '    "completed_items": {"direction": "maximize",\n'
    '                        "description": "Items finished in the horizon."},\n'
    '    "utilization": None,\n'
    "}\n" + _CALLSITE_RUNNER
)


class DeclaredMetricsTest(unittest.TestCase):
    def test_explicit_declaration_wins_and_carries_direction(self):
        declared = declared_metrics(_EXPLICIT_RUNNER)
        self.assertEqual(
            declared,
            (
                {
                    "name": "completed_items",
                    "direction": "maximize",
                    "description": "Items finished in the horizon.",
                },
                {"name": "utilization"},
            ),
        )

    def test_call_site_keys_are_reported_names_only(self):
        self.assertEqual(
            declared_metrics(_CALLSITE_RUNNER),
            ({"name": "completed_items"}, {"name": "utilization"}),
        )

    def test_malformed_declaration_falls_back_to_call_site(self):
        malformed = _EXPLICIT_RUNNER.replace('"maximize"', '"upward"')
        self.assertEqual(
            declared_metrics(malformed),
            ({"name": "completed_items"}, {"name": "utilization"}),
        )

    def test_without_summary_contract_nothing_is_declared(self):
        self.assertEqual(declared_metrics("OPTPILOT_METRICS = {'x': None}\n"), ())


class DerivedManifestMetricsTest(unittest.TestCase):
    def _bundle(self, tmp_path: Path, runner: str) -> Path:
        root = tmp_path / "bundle"
        (root / "devs_project").mkdir(parents=True)
        (root / "devs_project" / "__init__.py").write_text("", encoding="utf-8")
        (root / "devs_project" / "runner_gen.py").write_text(
            runner, encoding="utf-8"
        )
        (root / "run.py").write_text(
            'SIM_MODULE = "devs_project.runner_gen"\n', encoding="utf-8"
        )
        return root

    def test_derive_metrics_builds_the_v2_block(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = self._bundle(Path(tmp_dir), _EXPLICIT_RUNNER)
            self.assertEqual(
                se._derive_metrics(root),
                {
                    "keys": ["completed_items", "utilization"],
                    "descriptions": {
                        "completed_items": "Items finished in the horizon."
                    },
                    "objective": {
                        "metric": "completed_items",
                        "direction": "maximize",
                    },
                },
            )

    def test_derive_metrics_is_none_without_declarations(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = self._bundle(Path(tmp_dir), _REFERENCE_RUNNER)
            self.assertIsNone(se._derive_metrics(root))


class ManifestMetricsValidationTest(unittest.TestCase):
    def _write_manifest(self, tmp_path: Path, document: dict) -> Path:
        path = tmp_path / "simulation.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def _base_document(self, **overrides):
        document = {
            "schema_version": se.SIMULATION_SCHEMA,
            "entrypoint": "run.py",
            "timeout_seconds": 30,
            "arguments": [],
            "result_files": ["summary.json"],
        }
        document.update(overrides)
        return document

    def test_v2_manifest_with_metrics_loads(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_manifest(
                Path(tmp_dir),
                self._base_document(
                    metrics={
                        "keys": ["throughput"],
                        "objective": {
                            "metric": "throughput",
                            "direction": "maximize",
                        },
                    }
                ),
            )
            parsed = se._load_manifest(path, maximum_timeout_seconds=86400, validate_runtime_files=False)
            self.assertEqual(parsed.schema_version, se.SIMULATION_SCHEMA)
            self.assertEqual(parsed.metrics["keys"], ["throughput"])

    def test_v1_manifest_still_loads_without_metrics(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_manifest(
                Path(tmp_dir),
                self._base_document(schema_version=se.SIMULATION_SCHEMA_V1),
            )
            parsed = se._load_manifest(path, maximum_timeout_seconds=86400, validate_runtime_files=False)
            self.assertEqual(parsed.schema_version, se.SIMULATION_SCHEMA_V1)
            self.assertIsNone(parsed.metrics)

    def test_metrics_on_v1_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_manifest(
                Path(tmp_dir),
                self._base_document(
                    schema_version=se.SIMULATION_SCHEMA_V1,
                    metrics={"keys": ["throughput"]},
                ),
            )
            with self.assertRaises(se.SimulationManifestError):
                se._load_manifest(path, maximum_timeout_seconds=86400, validate_runtime_files=False)

    def test_objective_must_name_a_declared_metric(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_manifest(
                Path(tmp_dir),
                self._base_document(
                    metrics={
                        "keys": ["throughput"],
                        "objective": {
                            "metric": "latency",
                            "direction": "minimize",
                        },
                    }
                ),
            )
            with self.assertRaises(se.SimulationManifestError):
                se._load_manifest(path, maximum_timeout_seconds=86400, validate_runtime_files=False)


if __name__ == "__main__":
    unittest.main()
