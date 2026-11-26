"""End-to-end retained execution of a command-protocol batch method (F3).

This intentionally runs the real public path — package on disk, local Realm
runtime, retained compile, supervised method worker, command subprocess per
exchange — with no driver or worker mocks.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm_study_runner import run_local_realm_study
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


_ENVIRONMENT = """\
apiVersion: optpilot.io/v1
config: environment
id: command-local-environment
description: Environment for the command batch method end-to-end fixture
evaluator:
  python: local_package.evaluate:evaluate
  pythonPath: [../..]
  settings: {}
candidate:
  format: parameters
  description: Parameter accepted by the command fixture evaluator.
  parameters:
    schema:
      x:
        valueType: float
        min: 0.0
        max: 1.0
metrics:
  source: return
  keys: [score]
"""

_METHOD = """\
apiVersion: optpilot.io/v1
config: method
id: command-local-method
description: Command batch method executed by the retained slice
entrypoint:
  command: [python, propose.py]
  protocol: batch
  exchangeTimeoutSeconds: 30
settings:
  batchSize: 1
accepts:
  formats: [parameters]
  requires:
    context: [candidate.parameters.schema]
"""

_PROPOSE = """\
import json
import sys

request = json.load(sys.stdin)
assert request["protocol"] == "optpilot.method.batch.v1"
assert request["objective"]["primaryMetric"]["name"] == "score"
sequence = int(request["exchange_sequence"])
value = min(1.0, 0.125 * sequence)
json.dump(
    {
        "candidates": [
            {
                "candidate_id": f"command-candidate-{sequence}",
                "format": "parameters",
                "spec": {"x": value},
            }
        ]
    },
    sys.stdout,
)
"""

_STUDY = """\
apiVersion: optpilot.io/v1
config: study
name: command-local-study
description: Command batch method end-to-end fixture
environmentConfig: ../environments/environment.yaml
methodConfig: ../methods/method.yaml
objective:
  metric: score
  direction: maximize
budget:
  maxTrials: 2
execution:
  parallelism: 1
  timeoutSeconds: 60
reproducibility:
  seed: 7
"""


def _write_command_package(root: Path) -> Path:
    study = root / "configs" / "studies" / "study.yaml"
    environment = root / "configs" / "environments" / "environment.yaml"
    method = root / "configs" / "methods" / "method.yaml"
    for path in (study, environment, method):
        path.parent.mkdir(parents=True, exist_ok=True)
    study.write_text(_STUDY, encoding="utf-8")
    environment.write_text(_ENVIRONMENT, encoding="utf-8")
    method.write_text(_METHOD, encoding="utf-8")
    (method.parent / "propose.py").write_text(_PROPOSE, encoding="utf-8")
    source = root / "local_package"
    source.mkdir()
    (source / "evaluate.py").write_text(
        "def evaluate(candidate, context):\n    return {'score': candidate['x']}\n",
        encoding="utf-8",
    )
    return study


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class CommandMethodRealmRunE2ETest(unittest.TestCase):
    def test_command_batch_method_drives_a_full_retained_run(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package_root = root / "package"
        package_root.mkdir()
        study = _write_command_package(package_root)
        runtime = LocalRealmRuntime.open(
            realm_root=root / "realm",
            actor_principal_id="operator",
        )
        self.addCleanup(runtime.close)

        summary = run_local_realm_study(
            runtime=runtime,
            package_root=package_root,
            study_config_path=study,
            operation_id="command-method-e2e/run",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
            method_start_timeout=20,
        )

        self.assertEqual(summary.run_status, "succeeded")
        self.assertEqual(summary.successful_logical_trials, 2)
        snapshot = runtime.ledger.read_run_snapshot(
            actor_principal_id=runtime.actor_principal_id,
            run_id=summary.run_id,
        )
        self.assertEqual(snapshot.run.state, "succeeded")
        candidate_ids = {
            candidate.candidate_id for candidate in snapshot.candidates
        }
        self.assertTrue(
            all(item.startswith("command-candidate-") for item in candidate_ids),
            candidate_ids,
        )
        self.assertEqual(len(candidate_ids), 2)
        self.assertGreaterEqual(len(snapshot.method_exchange_completions), 2)


if __name__ == "__main__":
    unittest.main()
