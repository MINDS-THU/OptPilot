import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


BLUEPRINTS = [
    "scenario_blueprint_static_tree.py",
    "scenario_blueprint_custom_hooks.py",
    "scenario_blueprint_multiproduct.py",
]


def load_blueprint(path: Path):
    spec = importlib.util.spec_from_file_location(f"bp_{path.stem}", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_oracle_once(blueprint_path: Path, seed: int, periods: int, cli_kwargs: dict, stdin_payload: str):
    cmd = [
        sys.executable,
        "oracle_runner.py",
        "--blueprint",
        str(blueprint_path),
        "--seed",
        str(seed),
        "--periods",
        str(periods),
    ]
    for k, v in cli_kwargs.items():
        cmd.extend([f"--{k}", str(v)])

    proc = subprocess.run(cmd, input=stdin_payload, text=True, capture_output=True, cwd=str(ROOT))
    if proc.returncode != 0:
        raise RuntimeError(
            f"oracle_runner failed for {blueprint_path.name} seed={seed}:\n{proc.stderr}"
        )

    logs = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        logs.append(json.loads(line))
    return logs


def validate_logs_with_blueprint(bp, logs):
    for checker in getattr(bp, "tier2_checkers", []):
        checker(logs)
    for checker in getattr(bp, "tier3_checkers", []):
        checker(logs)

    if hasattr(bp, "extract_kpis"):
        kpis = bp.extract_kpis(logs)
        if not isinstance(kpis, dict):
            raise TypeError("extract_kpis must return dict.")


def run_smoke_for_blueprint(blueprint_file: str):
    path = ROOT / blueprint_file
    bp = load_blueprint(path)
    cases = getattr(bp, "test_cases", [])
    if not cases:
        raise ValueError(f"{blueprint_file} does not define test_cases.")

    print(f"\n[Smoke] {blueprint_file}")
    total_runs = 0
    for case in cases:
        case_name = case.get("case_name", "unnamed")
        cli_kwargs = dict(case.get("cli_kwargs", {}))
        stdin_payload = str(case.get("stdin_payload", ""))

        # Two seeds per case for basic stochastic coverage.
        for seed in (42, 43):
            logs = run_oracle_once(
                blueprint_path=path,
                seed=seed,
                periods=80,
                cli_kwargs=cli_kwargs,
                stdin_payload=stdin_payload,
            )
            if not isinstance(logs, list):
                raise TypeError("oracle output must parse into a list of logs.")
            validate_logs_with_blueprint(bp, logs)
            total_runs += 1

        print(f"  - case={case_name}: seeds(42,43) passed")

    print(f"  -> {blueprint_file}: {total_runs} oracle runs passed")


def main():
    print("Running oracle smoke tests on extra blueprints...")
    for blueprint in BLUEPRINTS:
        run_smoke_for_blueprint(blueprint)
    print("\nAll extra blueprint smoke tests passed.")


if __name__ == "__main__":
    main()
