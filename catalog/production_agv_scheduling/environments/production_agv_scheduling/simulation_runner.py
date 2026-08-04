"""Deterministic policy execution for the production-and-AGV simulator."""

from __future__ import annotations

import contextlib
import copy
import gc
import hashlib
import importlib.util
import json
import math
import random
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from commands import DirectCommandHandler
from enhanced_snapshot import StaticFactoryData, create_enhanced_snapshot
from factory_sim.run_multi_line_simulation import MultiLineFactorySimulation
from factory_sim.utils.config_loader import load_factory_config


JsonDict = dict[str, Any]

DEFAULT_SETTINGS: JsonDict = {
    "time_unit": "minutes",
    "simulation_horizon": 500.0,
    "time_step": 0.5,
    "disable_faults": True,
    "order_interval": [10.0, 10.0],
    "fault_interval": [120.0, 180.0],
    "fault_duration": [20.0, 60.0],
    "repeat_runs": 10,
    "seeds": list(range(123, 133)),
    "stability_lambda": 0.35,
}


def normalize_settings(settings: Mapping[str, Any] | None) -> JsonDict:
    """Validate evaluator settings and return one canonical settings object."""

    raw = dict(settings or {})
    normalized = copy.deepcopy(DEFAULT_SETTINGS)
    normalized.update(raw)

    # Accept the names used by the extracted runner while publishing only the
    # clearer canonical names in package configs.
    if "simulation_seconds" in raw and "simulation_horizon" not in raw:
        normalized["simulation_horizon"] = raw["simulation_seconds"]
    if "sim_time_step" in raw and "time_step" not in raw:
        normalized["time_step"] = raw["sim_time_step"]

    horizon = _positive_number(normalized["simulation_horizon"], "simulation_horizon")
    time_step = _positive_number(normalized["time_step"], "time_step")
    if time_step > horizon:
        raise ValueError("time_step cannot exceed simulation_horizon.")
    repeat_runs = normalized["repeat_runs"]
    if isinstance(repeat_runs, bool) or not isinstance(repeat_runs, int) or repeat_runs < 1:
        raise ValueError("repeat_runs must be a positive integer.")
    seeds = normalized.get("seeds")
    if isinstance(seeds, (str, bytes)) or not isinstance(seeds, Sequence):
        raise TypeError("seeds must be a sequence of integers.")
    checked_seeds: list[int] = []
    for index, seed in enumerate(seeds):
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TypeError(f"seeds[{index}] must be an integer.")
        checked_seeds.append(seed)
    if len(checked_seeds) != repeat_runs:
        raise ValueError(
            f"seeds must contain exactly repeat_runs={repeat_runs} values; got {len(checked_seeds)}."
        )
    if len(set(checked_seeds)) != len(checked_seeds):
        raise ValueError("seeds must not contain duplicates.")
    order_interval = _range(normalized["order_interval"], "order_interval", allow_zero=False)
    fault_interval = _range(normalized["fault_interval"], "fault_interval", allow_zero=False)
    fault_duration = _range(normalized["fault_duration"], "fault_duration", allow_zero=False)
    stability_lambda = _nonnegative_number(normalized["stability_lambda"], "stability_lambda")
    if not isinstance(normalized["disable_faults"], bool):
        raise TypeError("disable_faults must be a boolean.")
    if normalized.get("time_unit") != "minutes":
        raise ValueError("time_unit must be 'minutes' for this packaged environment.")

    normalized.update(
        {
            "simulation_horizon": horizon,
            "time_step": time_step,
            "repeat_runs": repeat_runs,
            "seeds": checked_seeds,
            "order_interval": order_interval,
            "fault_interval": fault_interval,
            "fault_duration": fault_duration,
            "stability_lambda": stability_lambda,
        }
    )
    for legacy_name in ("simulation_seconds", "sim_time_step"):
        normalized.pop(legacy_name, None)
    return normalized


def run_policy_once(
    *,
    candidate_dir: str | Path,
    settings: Mapping[str, Any],
    seed: int,
    database_path: str | Path | None,
    telemetry_sink: Callable[[str, Any, int, bool], None] | None = None,
) -> JsonDict:
    """Execute one candidate under one seed and return its full KPI payload.

    ``telemetry_sink`` is an in-process observer used only by the optional
    visual interface.  It does not enable a socket client or otherwise change
    the evaluator's offline execution boundary.
    """

    normalized = normalize_settings_for_single_run(settings)
    candidate_root = Path(candidate_dir).resolve()
    scheduler_path = candidate_root / "scheduler.py"
    estimator_path = candidate_root / "param_estimator.py"
    if not scheduler_path.is_file():
        raise FileNotFoundError(f"Candidate is missing scheduler.py: {candidate_root}")
    if not estimator_path.is_file():
        raise FileNotFoundError(f"Candidate is missing param_estimator.py: {candidate_root}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer.")

    trace_path: Path | None = None
    if database_path is not None:
        trace_path = Path(database_path).resolve()
        if trace_path.name in {"", ".", ".."}:
            raise ValueError("database_path must identify a file.")

    layout = _configured_layout(normalized)
    simulation: MultiLineFactorySimulation | None = None
    with _candidate_import_scope(candidate_root), _deterministic_randomness(seed), _quiet_output():
        policy = None
        factory = None
        command_handler = None
        static_data = None
        result: JsonDict | None = None
        try:
            estimator_module = _load_module(estimator_path, "param_estimator")
            del estimator_module
            scheduler_module = _load_module(
                scheduler_path, "_optpilot_candidate_scheduler"
            )
            simulation = MultiLineFactorySimulation(
                database_path=trace_path,
                layout_config=layout,
            )
            simulation.initialize(
                no_faults=normalized["disable_faults"],
                no_mqtt=True,
                telemetry_sink=telemetry_sink,
            )
            policy, controller_drives_simulation = _create_policy(
                scheduler_module, simulation, normalized
            )
            factory = simulation.factory
            if factory is None:
                raise RuntimeError("Factory initialization did not produce a factory.")
            horizon = normalized["simulation_horizon"]
            if controller_drives_simulation:
                policy.run(horizon)
                if not math.isclose(float(factory.env.now), horizon, rel_tol=0.0, abs_tol=1e-7):
                    raise RuntimeError(
                        "create_controller(...).run(until) must advance the simulation "
                        f"to {horizon}; stopped at {factory.env.now}."
                    )
            else:
                command_handler = DirectCommandHandler(simulation)
                static_data = StaticFactoryData(factory.layout)
                time_step = normalized["time_step"]
                while factory.env.now < horizon:
                    snapshot = create_enhanced_snapshot(simulation, static_data)
                    commands = policy.run(snapshot)
                    if commands is not None:
                        command_handler.dispatch_many(commands)
                    current = float(factory.env.now)
                    target = min(current + time_step, horizon)
                    if target <= current:
                        raise RuntimeError(
                            f"Simulation failed to advance at time {current}; time_step={time_step}."
                        )
                    factory.run(until=target)
            kpi = factory.kpi_calculator.get_final_score()
            result = _numeric_kpi_payload(kpi)
            collect_metrics = getattr(policy, "collect_metrics", None)
            if callable(collect_metrics):
                diagnostics = collect_metrics()
                if not isinstance(diagnostics, Mapping):
                    raise TypeError("policy.collect_metrics() must return an object.")
                # Fail early if a candidate returns values that cannot be
                # retained in metrics.json evidence.
                json.dumps(diagnostics, allow_nan=False)
                result["policy_diagnostics"] = copy.deepcopy(dict(diagnostics))
        finally:
            try:
                if simulation is not None:
                    simulation.shutdown()
            finally:
                # SimPy processes form reference cycles.  Finalize them while
                # verbose legacy output is still redirected, not later during an
                # unrelated evaluator step or interpreter shutdown.
                policy = None
                command_handler = None
                static_data = None
                factory = None
                simulation = None
                gc.collect()
        if result is None:
            raise RuntimeError("Simulation completed without a KPI result.")
        return result


def normalize_settings_for_single_run(settings: Mapping[str, Any]) -> JsonDict:
    """Normalize settings without requiring a particular repetition list."""

    raw = dict(settings)
    seeds = raw.get("seeds", [0])
    if isinstance(seeds, Sequence) and not isinstance(seeds, (str, bytes)) and seeds:
        representative_seed = seeds[0]
    else:
        representative_seed = 0
    raw["repeat_runs"] = 1
    raw["seeds"] = [representative_seed]
    return normalize_settings(raw)


def _configured_layout(settings: Mapping[str, Any]) -> JsonDict:
    layout = copy.deepcopy(load_factory_config("factory_layout_multi.yml"))
    order_config = layout.setdefault("order_generator", {})
    order_config["generation_interval_range"] = list(settings["order_interval"])
    for line in layout.get("production_lines", []):
        fault_config = line.setdefault("fault_system", {})
        fault_config["fault_injection_interval"] = list(settings["fault_interval"])
        fault_config["fault_duration_range"] = list(settings["fault_duration"])
    return layout


def _create_policy(
    scheduler_module: ModuleType,
    simulation: MultiLineFactorySimulation,
    settings: Mapping[str, Any],
) -> tuple[Any, bool]:
    create_controller = getattr(scheduler_module, "create_controller", None)
    create_scheduler = getattr(scheduler_module, "create_scheduler", None)
    has_controller = callable(create_controller)
    has_scheduler = callable(create_scheduler)
    if has_controller and has_scheduler:
        raise ValueError(
            "scheduler.py must expose exactly one policy factory: "
            "create_scheduler() for snapshot policies or "
            "create_controller(simulation, settings) for simulation-bound baselines, "
            "not both."
        )
    if has_controller:
        policy = create_controller(simulation, dict(settings))
        controller_drives_simulation = True
    elif has_scheduler:
        policy = create_scheduler()
        controller_drives_simulation = False
    else:
        raise AttributeError(
            "scheduler.py must define create_scheduler() or create_controller(simulation, settings)."
        )
    if not callable(getattr(policy, "run", None)):
        raise TypeError("The created policy must define run(snapshot).")
    return policy, controller_drives_simulation


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to create an import specification for {path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


@contextlib.contextmanager
def _candidate_import_scope(candidate_root: Path) -> Iterator[None]:
    """Expose one candidate while preventing module leakage between trials."""

    candidate_root = candidate_root.resolve()
    controlled_names = {
        name
        for name in sys.modules
        if name == "param_estimator"
        or name == "policy"
        or name.startswith("policy.")
        or name == "_optpilot_candidate_scheduler"
    }
    saved = {name: sys.modules[name] for name in controlled_names}
    for name in controlled_names:
        sys.modules.pop(name, None)
    original_path = list(sys.path)
    sys.path.insert(0, str(candidate_root))
    try:
        yield
    finally:
        sys.path[:] = original_path
        for name, module in list(sys.modules.items()):
            if (
                name == "param_estimator"
                or name == "policy"
                or name.startswith("policy.")
                or name == "_optpilot_candidate_scheduler"
                or _module_is_below(module, candidate_root)
            ):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


def _module_is_below(module: object, root: Path) -> bool:
    value = getattr(module, "__file__", None)
    if not isinstance(value, str):
        return False
    try:
        Path(value).resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


@contextlib.contextmanager
def _deterministic_randomness(seed: int) -> Iterator[None]:
    """Isolate the upstream global RNG and make UUID-based IDs reproducible."""

    old_random_state = random.getstate()
    old_uuid4 = uuid.uuid4
    uuid_rng = random.Random(seed ^ 0xA6_51_90_2D)

    def deterministic_uuid4() -> uuid.UUID:
        return uuid.UUID(int=uuid_rng.getrandbits(128), version=4)

    random.seed(seed)
    uuid.uuid4 = deterministic_uuid4
    try:
        yield
    finally:
        uuid.uuid4 = old_uuid4
        random.setstate(old_random_state)


@contextlib.contextmanager
def _quiet_output() -> Iterator[None]:
    """Keep verbose legacy telemetry out of bounded evaluator logs."""

    sink = _DiscardWriter()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        yield


class _DiscardWriter:
    """File-like text sink whose memory use is independent of output volume."""

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        pass


def _numeric_kpi_payload(payload: Mapping[str, Any]) -> JsonDict:
    required = ("total_score", "efficiency_score", "quality_cost_score", "agv_score")
    result = copy.deepcopy(dict(payload))
    for key in required:
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Simulator KPI {key!r} must be a finite number; got {value!r}.")
        result[key] = float(value)
    return result


def candidate_fingerprint(candidate_dir: str | Path) -> str:
    """Return a stable digest of all allowed candidate source files."""

    root = Path(candidate_dir).resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().encode("utf-8")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _range(value: Any, name: str, *, allow_zero: bool) -> list[float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
        raise TypeError(f"{name} must be a two-number sequence.")
    lower = _nonnegative_number(value[0], f"{name}[0]") if allow_zero else _positive_number(value[0], f"{name}[0]")
    upper = _nonnegative_number(value[1], f"{name}[1]") if allow_zero else _positive_number(value[1], f"{name}[1]")
    if upper < lower:
        raise ValueError(f"{name}[1] must be greater than or equal to {name}[0].")
    return [lower, upper]


def _positive_number(value: Any, name: str) -> float:
    number = _nonnegative_number(value, name)
    if number <= 0.0:
        raise ValueError(f"{name} must be greater than zero.")
    return number


def _nonnegative_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number.")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{name} must be a finite non-negative number.")
    return number
