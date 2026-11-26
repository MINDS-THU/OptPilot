"""One ordinary Run where the Method calls an Environment capability.

The Environment locks a vendored wheel; the Method locks nothing and reaches
the Environment's callable through ``pythonPath`` because it requires the
Environment's declared capability.  The dependency module name here exists in
no package index and in no host interpreter, so the Method process can only
import it through the Environment's retained dependency layer.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.run_closure import RUN_PREPARED_RUNTIME_ROLE
from optpilot.realm.run_definition import RUN_PREPARED_METHOD_RUNTIME_ROLE
from optpilot.realm_study_runner import run_local_realm_study
from optpilot.runtime_scopes import ENVIRONMENT_PREPARED_PYTHON_SCOPE
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


_WHEEL_NAME = "capability_replay_support-1.0.0-py3-none-any.whl"
_DEPENDENCY_MODULE = "optpilot_capability_layer_test_support"


def _wheel_entry(name: str) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.date_time = (2020, 1, 1, 0, 0, 0)
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = (stat.S_IFREG | 0o644) << 16
    return entry


def _write_pure_python_wheel(path: Path) -> str:
    """Synthesize the dependency artifact; no package index or build tool is used."""

    path.parent.mkdir(parents=True, exist_ok=True)
    support_module = (
        "def replay_score(seed, production_rate):\n"
        "    # A tiny deterministic stand-in for a simulator replay.\n"
        "    return float(seed) + float(production_rate) * 2.0\n"
    ).encode("utf-8")
    wheel_metadata = (
        "Wheel-Version: 1.0\n"
        "Generator: optpilot-capability-e2e\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")
    files = (
        (f"{_DEPENDENCY_MODULE}.py", support_module),
        (
            "capability_replay_support-1.0.0.dist-info/METADATA",
            b"Metadata-Version: 2.1\nName: capability-replay-support\nVersion: 1.0.0\n",
        ),
        ("capability_replay_support-1.0.0.dist-info/WHEEL", wheel_metadata),
        ("capability_replay_support-1.0.0.dist-info/RECORD", b""),
    )
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files:
            archive.writestr(_wheel_entry(name), payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_capability_package(root: Path) -> Path:
    """Create a complete package only inside this test's temporary directory."""

    study = root / "studies" / "capability_replay.yaml"
    environment = root / "environments" / "replay_env" / "environment.yaml"
    method = root / "methods" / "replay_method" / "method.yaml"
    for path in (study, environment, method):
        path.parent.mkdir(parents=True, exist_ok=True)

    study.write_text(
        """\
apiVersion: optpilot.io/v1
config: study
name: capability-dependency-vertical-e2e
description: Exercise a capability-requiring Method through the ordinary Run path.
environmentConfig: ../environments/replay_env/environment.yaml
methodConfig: ../methods/replay_method/method.yaml
objective:
  metric: score
  direction: maximize
budget:
  maxTrials: 1
execution:
  parallelism: 1
  timeoutSeconds: 30
reproducibility:
  seed: 17
""",
        encoding="utf-8",
    )
    environment.write_text(
        """\
apiVersion: optpilot.io/v1
config: environment
id: capability-replay-environment
description: A deterministic evaluator that can also replay one exact seed.
evaluator:
  python: evaluate:evaluate
  pythonPath: [.]
  settings: {}
candidate:
  format: parameters
  description: Factory production rate.
  parameters:
    schema:
      production_rate:
        valueType: float
        min: 1.0
        max: 10.0
metrics:
  source: return
  keys: [score]
capabilities:
  - id: exact_seed_replay
    description: Replay one exact evaluation seed and return its score.
runtime:
  sandbox: process
  setup:
    cache: prepared
    timeoutSeconds: 30
    steps:
      - uses: python-venv
        cwd: .
        requirements: [requirements.lock]
""",
        encoding="utf-8",
    )
    # The Method deliberately declares no dependencies of its own.  Its only
    # route to the replay dependency is the Environment's retained layer.
    method.write_text(
        """\
apiVersion: optpilot.io/v1
config: method
id: capability-replay-method
description: Propose one input scored through the Environment's replay capability.
entrypoint:
  python: method:CapabilityReplayMethod
  pythonPath: [., ../../environments/replay_env]
  protocol: batch
runtime:
  sandbox: process
settings:
  batchSize: 1
accepts:
  formats: [parameters]
  requires:
    context: [candidate.parameters.schema]
    capabilities: [exact_seed_replay]
""",
        encoding="utf-8",
    )

    environment_source = environment.parent
    (environment_source / "evaluate.py").write_text(
        f"from {_DEPENDENCY_MODULE} import replay_score\n"
        "\n"
        "\n"
        "def replay_candidate(seed, production_rate):\n"
        "    \"\"\"The exact_seed_replay capability, called by Environment and Method.\"\"\"\n"
        "\n"
        "    return replay_score(seed, production_rate)\n"
        "\n"
        "\n"
        "def evaluate(candidate, context):\n"
        "    score = replay_candidate(2, candidate['production_rate'])\n"
        "    return {\n"
        "        'score': score,\n"
        "        'event_summary': {'replayed': True},\n"
        "    }\n",
        encoding="utf-8",
    )
    method_source = method.parent
    (method_source / "method.py").write_text(
        "from evaluate import replay_candidate\n"
        "\n"
        "\n"
        "class CapabilityReplayMethod:\n"
        "    def __init__(self, definition, study_spec, rng):\n"
        "        self.definition = definition\n"
        "        self.emitted = False\n"
        "\n"
        "    def propose(self, n_candidates, study_state, evidence_view):\n"
        "        if self.emitted or n_candidates <= 0:\n"
        "            return []\n"
        "        self.emitted = True\n"
        "        # Search by replaying one exact seed inside the Method process.\n"
        "        best = max(\n"
        "            (replay_candidate(2, rate), rate)\n"
        "            for rate in (3.0, 6.0)\n"
        "        )\n"
        "        return [{\n"
        "            'candidate_id': 'capability-replay-candidate',\n"
        "            'format': 'parameters',\n"
        "            'spec': {'production_rate': best[1]},\n"
        "            'lineage': {'parents': []},\n"
        "            'generator': {\n"
        "                'method_id': self.definition['id'],\n"
        "                'strategy': 'exact-seed-replay',\n"
        "            },\n"
        "        }]\n"
        "\n"
        "    def observe(self, observations):\n"
        "        return None\n",
        encoding="utf-8",
    )

    wheel = environment_source / "vendor" / _WHEEL_NAME
    digest = _write_pure_python_wheel(wheel)
    (environment_source / "requirements.lock").write_text(
        f"vendor/{_WHEEL_NAME} --hash=sha256:{digest}\n",
        encoding="utf-8",
    )
    return study


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class CapabilityDependencyVerticalE2ETest(unittest.TestCase):
    def test_capability_method_imports_the_environment_locked_dependency(
        self,
    ) -> None:
        # The dependency is unimportable from the host interpreter, so a Run
        # can only succeed through the Environment's retained layer.
        with self.assertRaises(ImportError):
            __import__(_DEPENDENCY_MODULE)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package_root = root / "capability-package"
        package_root.mkdir()
        study = _write_capability_package(package_root)
        runtime = LocalRealmRuntime.open(
            realm_root=root / "realm",
            actor_principal_id="operator",
        )
        self.addCleanup(runtime.close)

        summary = run_local_realm_study(
            runtime=runtime,
            package_root=package_root,
            study_config_path=study,
            operation_id=f"capability-dependency-vertical-e2e/run/{root.name}",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
            method_start_timeout=20,
            method_request_timeout=20,
        )

        self.assertEqual(summary.run_status, "succeeded")
        self.assertEqual(summary.successful_logical_trials, 1)
        snapshot = runtime.ledger.read_run_snapshot(
            actor_principal_id=runtime.actor_principal_id,
            run_id=summary.run_id,
        )
        observation = snapshot.observations[0].envelope
        self.assertEqual(observation.outcome, "success")
        # 2 + 6.0 * 2: the Method's replay chose 6.0 and the evaluator agrees.
        self.assertEqual(observation.metric_values["score"], 14.0)

        definition = runtime.ledger.read_run_definition(
            actor_principal_id=runtime.actor_principal_id,
            run_id=summary.run_id,
        )
        method_runtime = definition.prepared_method_runtime
        self.assertEqual(len(method_runtime.prepared_layers), 1)
        shared = method_runtime.prepared_layers[0]
        self.assertEqual(shared.scope, ENVIRONMENT_PREPARED_PYTHON_SCOPE)
        self.assertIn(
            {"path": ".", "scope": ENVIRONMENT_PREPARED_PYTHON_SCOPE},
            [dict(item) for item in method_runtime.runtime_settings["import_roots"]],
        )
        refs = definition.content_refs_by_role
        self.assertEqual(refs[RUN_PREPARED_RUNTIME_ROLE], (shared.snapshot_ref,))
        self.assertEqual(
            refs[RUN_PREPARED_METHOD_RUNTIME_ROLE], (shared.snapshot_ref,)
        )


if __name__ == "__main__":
    unittest.main()
