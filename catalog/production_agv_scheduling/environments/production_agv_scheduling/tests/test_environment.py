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

from evaluator import (  # noqa: E402
    _aggregate,
    _assert_canonical_kpi_equal,
    _policy_fallback_replans,
    _validate_trace_database,
    evaluate,
)
from factory_sim.config.schemas import DeviceStatus  # noqa: E402
from factory_sim.game_logic.fault_system import FaultType  # noqa: E402
from factory_sim.run_multi_line_simulation import MultiLineFactorySimulation  # noqa: E402
from factory_sim.simulation.entities.agv import AGV  # noqa: E402
from factory_sim.simulation.entities.base import Device  # noqa: E402
from factory_sim.utils.sqlite_db import SimulationDatabaseError  # noqa: E402
from replay import replay_candidate  # noqa: E402
from simulation_runner import (  # noqa: E402
    _candidate_import_scope,
    _create_policy,
    run_policy_once,
)
import simpy  # noqa: E402


class EnvironmentIntegrationTests(unittest.TestCase):
    def test_partial_simulation_initialization_is_shut_down(self) -> None:
        settings = json.loads(
            (ENVIRONMENT_ROOT / "settings" / "smoke.json").read_text(
                encoding="utf-8"
            )
        )
        with (
            patch(
                "simulation_runner.MultiLineFactorySimulation.initialize",
                side_effect=RuntimeError("injected initialize failure"),
            ),
            patch(
                "simulation_runner.MultiLineFactorySimulation.shutdown"
            ) as shutdown,
            self.assertRaisesRegex(RuntimeError, "injected initialize failure"),
        ):
            run_policy_once(
                candidate_dir=ENVIRONMENT_ROOT / "initial",
                settings=settings,
                seed=settings["seeds"][0],
                database_path=None,
            )
        shutdown.assert_called_once_with()

    def test_trace_write_and_close_failures_fail_the_replay(self) -> None:
        settings = json.loads(
            (ENVIRONMENT_ROOT / "settings" / "smoke.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_path = Path(temporary_directory) / "write-failure.db"
            with (
                patch(
                    "factory_sim.utils.sqlite_db.SimulationDatabase._execute_insert",
                    side_effect=sqlite3.OperationalError("injected disk full"),
                ),
                self.assertRaisesRegex(
                    SimulationDatabaseError, "injected disk full"
                ),
            ):
                replay_candidate(
                    candidate_dir=ENVIRONMENT_ROOT / "initial",
                    settings=settings,
                    seed=settings["seeds"][0],
                    database_path=trace_path,
                )

            simulation = MultiLineFactorySimulation(
                database_path=Path(temporary_directory) / "close-failure.db"
            )
            simulation.initialize(no_faults=True, no_mqtt=True)
            assert simulation.database is not None
            with (
                patch.object(
                    simulation.database,
                    "_close_connection",
                    side_effect=OSError("injected close failure"),
                ),
                self.assertRaisesRegex(
                    SimulationDatabaseError, "injected close failure"
                ),
            ):
                simulation.shutdown()
            self.assertIsNone(simulation.database)
            self.assertIsNone(simulation.mqtt_client)

    def test_trace_validation_rejects_missing_empty_and_corrupt_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            missing = root / "missing.db"
            with sqlite3.connect(missing) as connection:
                connection.execute("CREATE TABLE kpi (value REAL)")
                connection.execute("INSERT INTO kpi VALUES (1.0)")
            with self.assertRaisesRegex(RuntimeError, "missing required tables"):
                _validate_trace_database(missing)

            empty = root / "empty.db"
            with sqlite3.connect(empty) as connection:
                connection.execute("CREATE TABLE kpi (value REAL)")
                connection.execute('CREATE TABLE "order" (value REAL)')
            with self.assertRaisesRegex(RuntimeError, "has no rows"):
                _validate_trace_database(empty)

            corrupt = root / "corrupt.db"
            corrupt.write_bytes(b"not a sqlite database")
            with self.assertRaisesRegex(RuntimeError, "unreadable or corrupt"):
                _validate_trace_database(corrupt)

    def test_candidate_source_mutation_fails_before_another_replication(self) -> None:
        settings = json.loads(
            (ENVIRONMENT_ROOT / "settings" / "smoke.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory) / "trial"
            candidate = workspace / "candidate"
            candidate.mkdir(parents=True)
            (candidate / "param_estimator.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
            (candidate / "scheduler.py").write_text(
                "from pathlib import Path\n"
                "Path(__file__).with_name('param_estimator.py').write_text('VALUE = 2\\n')\n"
                "class Scheduler:\n"
                "    def run(self, snapshot):\n"
                "        return []\n"
                "def create_scheduler():\n"
                "    return Scheduler()\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RuntimeError, "Candidate bundle changed during evaluation"
            ):
                evaluate(
                    {"workspace": str(workspace), "candidateRoot": str(candidate)},
                    {"workspace": str(workspace), "settings": settings},
                )

    def test_policy_factory_contract_rejects_ambiguous_modules(self) -> None:
        policy = SimplePolicy()
        scheduler_module = types.SimpleNamespace(
            create_scheduler=lambda: policy,
            create_controller=lambda _simulation, _settings: policy,
        )

        with self.assertRaisesRegex(ValueError, "exactly one policy factory"):
            _create_policy(scheduler_module, object(), {})

    def test_policy_factory_contract_preserves_each_single_entrypoint(self) -> None:
        snapshot_policy = SimplePolicy()
        selected, drives_simulation = _create_policy(
            types.SimpleNamespace(create_scheduler=lambda: snapshot_policy),
            object(),
            {},
        )
        self.assertIs(selected, snapshot_policy)
        self.assertFalse(drives_simulation)

        controller_policy = SimplePolicy()
        simulation = object()
        selected, drives_simulation = _create_policy(
            types.SimpleNamespace(
                create_controller=lambda received, settings: (
                    controller_policy
                    if received is simulation and settings == {"marker": 1}
                    else None
                )
            ),
            simulation,
            {"marker": 1},
        )
        self.assertIs(selected, controller_policy)
        self.assertTrue(drives_simulation)

    def test_device_instances_do_not_share_default_interaction_points(self) -> None:
        env = simpy.Environment()
        first = Device(env, "first", (0, 0))
        second = Device(env, "second", (1, 0))

        first.interacting_points.append("P1")

        self.assertEqual(first.interacting_points, ["P1"])
        self.assertEqual(second.interacting_points, [])

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


class SimplePolicy:
    def run(self, _input) -> list:
        return []


class AggregationTests(unittest.TestCase):
    def test_full_canonical_kpi_replay_comparison(self) -> None:
        first = {
            "total_score": 70.0,
            "components": {"quality": 21.0, "counts": [1, 2]},
            "label": "complete",
            "fallback": False,
        }
        replayed = json.loads(json.dumps(first))
        replayed["components"]["quality"] += 1e-10
        _assert_canonical_kpi_equal(first, replayed)

        replayed["components"]["quality"] = 21.5
        with self.assertRaisesRegex(RuntimeError, r"\$\.components\.quality"):
            _assert_canonical_kpi_equal(first, replayed)

        replayed = json.loads(json.dumps(first))
        replayed["extra"] = 1
        with self.assertRaisesRegex(RuntimeError, "keys differ"):
            _assert_canonical_kpi_equal(first, replayed)

        replayed = json.loads(json.dumps(first))
        replayed["total_score"] = math.nan
        with self.assertRaisesRegex(RuntimeError, "non-finite"):
            _assert_canonical_kpi_equal(first, replayed)

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
