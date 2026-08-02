"""Focused regression coverage for deterministic environment evaluation."""

from __future__ import annotations

import importlib
import json
import math
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ENVIRONMENT_ROOT = Path(__file__).resolve().parents[1]
if str(ENVIRONMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(ENVIRONMENT_ROOT))

from evaluator import _aggregate, _policy_fallback_replans, evaluate  # noqa: E402
from factory_sim.config.schemas import DeviceStatus  # noqa: E402
from factory_sim.game_logic.fault_system import FaultType  # noqa: E402
from factory_sim.run_multi_line_simulation import MultiLineFactorySimulation  # noqa: E402
from factory_sim.simulation.entities.agv import AGV  # noqa: E402
from replay import replay_candidate  # noqa: E402
from simulation_runner import _candidate_import_scope  # noqa: E402
import simpy  # noqa: E402


class EnvironmentIntegrationTests(unittest.TestCase):
    def test_charge_rechecks_battery_after_moving_to_charger(self) -> None:
        env = simpy.Environment()
        agv = AGV(
            env=env,
            id="AGV1",
            position=(0, 0),
            path_points={"P1": (0, 0), "P10": (1, 0)},
            speed_mps=1.0,
            battery_level=40.0,
            charging_point="P10",
            charging_speed=2.0,
        )

        def move_and_receive_concurrent_charge(target_point: str):
            yield env.timeout(1.0)
            agv.current_point = target_point
            agv.position = agv.path_points[target_point]
            agv.battery_level = 55.0
            return True, f"Arrived at {target_point}"

        agv.move_to = move_and_receive_concurrent_charge  # type: ignore[method-assign]
        charge = env.process(agv.charge_battery(target_level=50.0))

        env.run()

        self.assertEqual(charge.value, (True, "battery level is enough (55.0%)"))
        self.assertEqual(agv.battery_level, 55.0)
        self.assertEqual(agv.stats["total_charge_time"], 0.0)

    def test_smoke_evaluation_and_replay_are_deterministic_and_traced(self) -> None:
        settings = json.loads(
            (ENVIRONMENT_ROOT / "settings" / "smoke.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "trial"
            candidate = workspace / "candidate"
            shutil.copytree(
                ENVIRONMENT_ROOT / "initial",
                candidate,
                copy_function=shutil.copy,
            )

            result = evaluate(
                {"workspace": str(workspace), "candidateRoot": str(candidate)},
                {"workspace": str(workspace), "settings": settings},
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["metric_values"]["replication_count"], 1)
            for key in (
                "total_score",
                "mean_efficiency_score",
                "mean_quality_cost_score",
                "mean_agv_score",
            ):
                self.assertTrue(math.isfinite(float(result["metric_values"][key])))

            report = json.loads((workspace / "metrics.json").read_text(encoding="utf-8"))
            trace = workspace / "worst_run.db"
            self.assertEqual(report["worst_run"]["seed"], settings["seeds"][0])
            self.assertTrue(trace.is_file())

            with sqlite3.connect(trace) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("kpi", tables)
                self.assertIn("order", tables)
                self.assertGreater(connection.execute("SELECT COUNT(*) FROM kpi").fetchone()[0], 0)
                self.assertGreater(
                    connection.execute('SELECT COUNT(*) FROM "order"').fetchone()[0],
                    0,
                )

            replay_one = replay_candidate(
                candidate_dir=candidate,
                settings=settings,
                seed=settings["seeds"][0],
                database_path=root / "replay-one.db",
            )
            replay_two = replay_candidate(
                candidate_dir=candidate,
                settings=settings,
                seed=settings["seeds"][0],
                database_path=root / "replay-two.db",
            )
            self.assertEqual(replay_one, replay_two)
            self.assertEqual(replay_one, report["worst_run"]["kpi"])

    def test_candidate_policy_imports_do_not_leak_between_trials(self) -> None:
        sentinel = types.ModuleType("policy")
        previous_policy = sys.modules.get("policy")
        previous_marker = sys.modules.get("policy.marker")
        sys.modules["policy"] = sentinel
        sys.modules.pop("policy.marker", None)
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                candidates = []
                for label in ("first", "second"):
                    candidate = root / label
                    policy = candidate / "policy"
                    policy.mkdir(parents=True)
                    (policy / "__init__.py").write_text("", encoding="utf-8")
                    (policy / "marker.py").write_text(
                        f"VALUE = {label!r}\n",
                        encoding="utf-8",
                    )
                    candidates.append(candidate)

                for candidate, expected in zip(candidates, ("first", "second"), strict=True):
                    with _candidate_import_scope(candidate):
                        marker = importlib.import_module("policy.marker")
                        self.assertEqual(marker.VALUE, expected)
                    self.assertIs(sys.modules.get("policy"), sentinel)
                    self.assertNotIn("policy.marker", sys.modules)
        finally:
            if previous_policy is None:
                sys.modules.pop("policy", None)
            else:
                sys.modules["policy"] = previous_policy
            if previous_marker is None:
                sys.modules.pop("policy.marker", None)
            else:
                sys.modules["policy.marker"] = previous_marker

    def test_busy_agv_pending_fault_triggers_when_it_becomes_idle(self) -> None:
        simulation = MultiLineFactorySimulation()
        simulation.initialize(no_faults=False, no_mqtt=True)
        try:
            self.assertIsNotNone(simulation.factory)
            line = next(iter(simulation.factory.lines.values()))
            fault_system = line.fault_system
            self.assertIsNotNone(fault_system)
            agv = next(iter(line.agvs.values()))
            self.assertIs(agv.fault_system, fault_system)

            agv.set_status(DeviceStatus.MOVING)
            fault_system.inject_random_fault(
                target_device=agv.id,
                fault_type=FaultType.AGV_FAULT,
            )
            self.assertIn(agv.id, fault_system.pending_agv_faults)
            self.assertNotIn(agv.id, fault_system.active_faults)

            agv.set_status(DeviceStatus.IDLE)
            with patch(
                "factory_sim.game_logic.fault_system.random.uniform",
                return_value=30.0,
            ):
                self.assertTrue(agv._check_and_trigger_pending_fault())
            self.assertNotIn(agv.id, fault_system.pending_agv_faults)
            self.assertIn(agv.id, fault_system.active_faults)
            self.assertEqual(fault_system.active_faults[agv.id].duration, 30.0)
            self.assertEqual(agv.status, DeviceStatus.FAULT)
        finally:
            simulation.shutdown()


class AggregationTests(unittest.TestCase):
    def test_aggregate_uses_sample_standard_deviation_and_policy_diagnostics(self) -> None:
        records = [
            {"seed": 11, "kpi": self._kpi(1.0, fallback_replans=4)},
            {"seed": 12, "kpi": self._kpi(3.0)},
        ]

        metrics = _aggregate(records, {"stability_lambda": 0.5})

        expected_std = math.sqrt(2.0)
        self.assertAlmostEqual(metrics["std_total_score"], expected_std)
        self.assertAlmostEqual(metrics["stability_fitness"], 2.0 - 0.5 * expected_std)
        self.assertAlmostEqual(metrics["mean_policy_fallback_replans"], 2.0)
        self.assertEqual(metrics["policy_fallback_replication_count"], 1)

    def test_policy_fallback_diagnostics_are_validated(self) -> None:
        self.assertEqual(_policy_fallback_replans({}), 0.0)
        self.assertEqual(
            _policy_fallback_replans(
                {"policy_diagnostics": {"engine": {"heuristic_fallback_replans": 2}}}
            ),
            2.0,
        )
        with self.assertRaises(TypeError):
            _policy_fallback_replans(
                {"policy_diagnostics": {"engine": {"heuristic_fallback_replans": True}}}
            )
        with self.assertRaises(ValueError):
            _policy_fallback_replans(
                {"policy_diagnostics": {"engine": {"heuristic_fallback_replans": -1}}}
            )

    @staticmethod
    def _kpi(total_score: float, *, fallback_replans: int | None = None) -> dict:
        payload = {
            "total_score": total_score,
            "efficiency_score": total_score,
            "quality_cost_score": total_score,
            "agv_score": total_score,
            "efficiency_components": {
                "order_completion": total_score,
                "production_cycle": total_score,
                "device_utilization": total_score,
            },
            "quality_cost_components": {
                "first_pass_rate": total_score,
                "cost_efficiency": total_score,
            },
            "agv_components": {
                "charge_strategy": total_score,
                "energy_efficiency": total_score,
                "utilization": total_score,
            },
        }
        if fallback_replans is not None:
            payload["policy_diagnostics"] = {
                "engine": {"heuristic_fallback_replans": fallback_replans}
            }
        return payload


if __name__ == "__main__":
    unittest.main()
