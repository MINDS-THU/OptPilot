"""The vendored Factorio evaluation core keeps upstream semantics (W4 item 1).

`catalog/factorio_design_benchmark/environments/factory_design/fd_core/` is a
copy of the benchmark's dependency-light evaluation subset, edited only so it
runs under pydantic 1.10.x — the newest pydantic line whose whole dependency
closure has pure-Python wheels, which is what OptPilot's locked process
runtime accepts (`locked_python_runtime._validate_wheel_tags`).

These tests pin the properties that make the copy trustworthy:

* it imports and validates with no third-party dependency beyond pydantic,
* the 32 task configs load byte-for-byte identically to upstream (the BOM
  handling matters: 24 of the 32 files carry one, and the upstream registry
  is fail-open — a config that fails to load is silently replaced by a
  programmatic task whose target rate differs by 3-10x),
* validation verdicts match upstream over a corpus of good and broken designs.

The upstream tree is untracked research scratch. When it is absent, the
differential comparison is skipped and the self-contained checks still run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_ROOT = (
    _REPO_ROOT
    / "catalog"
    / "factorio_design_benchmark"
    / "environments"
    / "factory_design"
)
_FD_CORE = _ENV_ROOT / "fd_core"
_UPSTREAM = (
    _REPO_ROOT.parent.parent.parent
    / "resource"
    / "Factorio-Design-Benchmark-main"
)


def _probe(source: str, *, path_root: Path, package: str, pydantic_spec: str) -> dict:
    """Run a probe against one copy of the core in an isolated interpreter."""

    script = textwrap.dedent(
        f"""
        import json, sys
        sys.path.insert(0, {str(path_root)!r})
        from {package}.validation import validate_production_line
        from {package}.schemas import EXAMPLE_PRODUCTION_LINE
        from {package}.tasks import get_task, list_tasks
        """
    ) + textwrap.dedent(source)
    completed = subprocess.run(
        ["uv", "run", "--with", pydantic_spec, "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(_REPO_ROOT),
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"probe failed ({package}):\n{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


_CORPUS_PROBE = """
    example = EXAMPLE_PRODUCTION_LINE
    if not isinstance(example, dict):
        example = json.loads(
            example.json() if hasattr(example, "json") else example.model_dump_json()
        )

    def mutate(fn):
        d = json.loads(json.dumps(example))
        fn(d)
        return d

    def break_entity(d):
        list(d["blocks"].values())[0]["machines"][0]["type"] = "not-a-machine"

    def move_off_map(d):
        list(d["blocks"].values())[0]["machines"][0]["position"] = {"x": 9999, "y": 9999}

    def overlap(d):
        block = list(d["blocks"].values())[0]
        if len(block["machines"]) > 1:
            block["machines"][1]["position"] = dict(block["machines"][0]["position"])

    corpus = {
        "example_good": example,
        "bad_entity_type": mutate(break_entity),
        "off_map": mutate(move_off_map),
        "no_power": mutate(lambda d: d.__setitem__("global_entities", [])),
        "overlapping": mutate(overlap),
        "missing_blocks": mutate(lambda d: d.pop("blocks", None)),
        "not_a_dict": "[]",
        "bad_json": "{not json",
        "json_string_good": json.dumps(example),
    }

    out = {"tasks": {}, "validation": {}}
    for tid in sorted(list_tasks()):
        task = get_task(tid)
        payload = task.dict() if hasattr(task, "dict") else task.model_dump()
        out["tasks"][tid] = {
            key: payload.get(key)
            for key in ("task_id", "target_product", "target_rate_per_minute", "capacity_level", "level")
        }
        config = task.to_validation_config()
        for name, design in corpus.items():
            try:
                result = validate_production_line(design, config)
                out["validation"][f"{tid}::{name}"] = {
                    "passed": result.passed,
                    "failed_checks": sorted(result.failed_checks),
                    "warnings": sorted(result.warnings),
                    "cost": (result.metrics.get("cost") or {}).get("total_cost"),
                }
            except Exception as error:  # noqa: BLE001 - contract of the probe
                out["validation"][f"{tid}::{name}"] = {"exception": type(error).__name__}
    print(json.dumps(out, sort_keys=True, default=str))
"""


class FactorioVendoredCoreTest(unittest.TestCase):
    def test_vendored_core_runs_on_pydantic_v1_only(self) -> None:
        result = _probe(
            """
            print(json.dumps({"tasks_loaded": len(list_tasks())}))
            """,
            path_root=_ENV_ROOT,
            package="fd_core",
            pydantic_spec="pydantic==1.10.22",
        )

        self.assertEqual(result["tasks_loaded"], 32)

    def test_every_task_config_survives_its_byte_order_mark(self) -> None:
        configs = sorted((_FD_CORE / "tasks" / "configs").glob("*.json"))
        self.assertEqual(len(configs), 32)
        with_bom = [p for p in configs if p.read_bytes()[:3] == b"\xef\xbb\xbf"]
        # Upstream ships 24 BOM'd configs; the loader must read them as
        # utf-8-sig or the fail-open registry silently substitutes a
        # programmatic task with a different target rate.
        self.assertEqual(len(with_bom), 24)
        registry = (_FD_CORE / "tasks" / "task_registry.py").read_text(encoding="utf-8")
        self.assertIn("utf-8-sig", registry)

    def test_vendored_core_declares_no_dependency_beyond_pydantic(self) -> None:
        third_party = set()
        for module in _FD_CORE.rglob("*.py"):
            for line in module.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    root = stripped.split()[1].split(".")[0]
                    if root not in {
                        "__future__", "json", "math", "typing", "dataclasses", "enum",
                        "pathlib", "collections", "itertools", "functools", "re", "os",
                        "sys", "copy", "abc", "warnings",
                    } and not stripped.startswith("from ."):
                        third_party.add(root)
        self.assertEqual(third_party, {"pydantic"})

    def test_locked_runtime_pins_only_pure_python_wheels(self) -> None:
        lock = (_ENV_ROOT / "runtime_dependencies" / "requirements.lock").read_text(
            encoding="utf-8"
        )
        lines = [line for line in lock.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            wheel, _, digest = line.partition(" --hash=sha256:")
            self.assertTrue(wheel.endswith("-py3-none-any.whl"), wheel)
            self.assertEqual(len(digest.strip()), 64)
            self.assertTrue((_ENV_ROOT / "runtime_dependencies" / wheel).is_file(), wheel)

    @unittest.skipUnless(_UPSTREAM.is_dir(), "upstream research tree is not present")
    def test_vendored_core_matches_upstream_verdicts(self) -> None:
        vendored = _probe(
            _CORPUS_PROBE,
            path_root=_ENV_ROOT,
            package="fd_core",
            pydantic_spec="pydantic==1.10.22",
        )
        upstream = _probe(
            _CORPUS_PROBE,
            path_root=_UPSTREAM,
            package="fd_benchmark",
            pydantic_spec="pydantic>=2",
        )

        self.assertEqual(vendored["tasks"], upstream["tasks"])
        self.assertEqual(len(upstream["validation"]), 32 * 9)
        self.assertEqual(vendored["validation"], upstream["validation"])
        # The corpus must actually exercise the checks, not merely agree.
        exercised = {
            check
            for case in upstream["validation"].values()
            for check in (case.get("failed_checks") or [])
        }
        self.assertGreaterEqual(len(exercised), 10)


if __name__ == "__main__":
    unittest.main()
