from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import time
import unittest
import zipfile
from http import HTTPStatus
from pathlib import Path

from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.run_closure import (
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
)
from optpilot.realm.run_definition import (
    RUN_METHOD_SOURCE_ROLE,
    RUN_PREPARED_METHOD_RUNTIME_ROLE,
)
from optpilot.realm_study_runner import run_local_realm_study
from optpilot.retained_study_compiler import RetainedStudyCompileError
from optpilot.runtime_scopes import (
    ENVIRONMENT_PREPARED_PYTHON_SCOPE,
    METHOD_PREPARED_PYTHON_SCOPE,
)
from optpilot_studio.ui.server import (
    STUDY_LAUNCH_REQUEST_SCHEMA,
    UiState,
    _apply_package_plan,
    _catalog_payload,
    _execute_study_launch_request,
    _keep_selection_as_ui_workspace,
    _prepare_package_plan,
    _smoke_package_plan,
    _validate_package_plan,
)
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


_WHEEL_NAME = "generated_devs_support-1.0.0-py3-none-any.whl"
_DEPENDENCY_MODULE = "optpilot_generated_devs_test_support"


def _wheel_entry(name: str, payload: bytes) -> zipfile.ZipInfo:
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
        "def proposed_production_rate():\n"
        "    return 6.0\n"
        "\n"
        "def simulated_throughput(production_rate):\n"
        "    # A tiny deterministic generated-DEVS-like simulation result.\n"
        "    return float(production_rate) * 2.0\n"
    ).encode("utf-8")
    wheel_metadata = (
        "Wheel-Version: 1.0\n"
        "Generator: optpilot-vertical-e2e\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")
    files = (
        (f"{_DEPENDENCY_MODULE}.py", support_module),
        (
            "generated_devs_support-1.0.0.dist-info/METADATA",
            b"Metadata-Version: 2.1\nName: generated-devs-support\nVersion: 1.0.0\n",
        ),
        (
            "generated_devs_support-1.0.0.dist-info/WHEEL",
            wheel_metadata,
        ),
        ("generated_devs_support-1.0.0.dist-info/RECORD", b""),
    )
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files:
            archive.writestr(_wheel_entry(name, payload), payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_generated_devs_like_package(
    root: Path,
    *,
    valid_lock: bool = True,
) -> Path:
    """Create a complete package only inside this test's temporary directory."""

    study = root / "studies" / "generated_devs.yaml"
    environment = root / "environments" / "generated_devs" / "environment.yaml"
    method = root / "methods" / "generated_devs" / "method.yaml"
    for path in (study, environment, method):
        path.parent.mkdir(parents=True, exist_ok=True)

    study.write_text(
        """\
apiVersion: optpilot.io/v1
config: study
name: generated-devs-dependency-vertical-e2e
description: Exercise exact dependency preparation through the ordinary Run path.
environmentConfig: ../environments/generated_devs/environment.yaml
methodConfig: ../methods/generated_devs/method.yaml
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
    shared_runtime = """\
runtime:
  sandbox: process
  setup:
    cache: prepared
    timeoutSeconds: 30
    steps:
      - uses: python-venv
        cwd: .
        requirements: [requirements.lock]
"""
    environment.write_text(
        """\
apiVersion: optpilot.io/v1
config: environment
id: generated-devs-dependency-environment
description: A deterministic generated simulation evaluator.
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
"""
        + shared_runtime,
        encoding="utf-8",
    )
    method.write_text(
        """\
apiVersion: optpilot.io/v1
config: method
id: generated-devs-dependency-method
description: Propose one deterministic simulator input.
entrypoint:
  python: method:GeneratedDevsMethod
  pythonPath: [.]
  protocol: batch
settings:
  batchSize: 1
accepts:
  formats: [parameters]
  requires:
    context: [candidate.parameters.schema]
"""
        + shared_runtime,
        encoding="utf-8",
    )

    environment_source = environment.parent
    (environment_source / "evaluate.py").write_text(
        f"from {_DEPENDENCY_MODULE} import simulated_throughput\n"
        "\n"
        "def evaluate(candidate, context):\n"
        "    score = simulated_throughput(candidate['production_rate'])\n"
        "    return {\n"
        "        'score': score,\n"
        "        'event_summary': {'dependency_imported': True},\n"
        "    }\n",
        encoding="utf-8",
    )
    method_source = method.parent
    (method_source / "method.py").write_text(
        f"from {_DEPENDENCY_MODULE} import proposed_production_rate\n"
        "\n"
        "class GeneratedDevsMethod:\n"
        "    def __init__(self, definition, study_spec, rng):\n"
        "        self.definition = definition\n"
        "        self.emitted = False\n"
        "\n"
        "    def propose(self, n_candidates, study_state, evidence_view):\n"
        "        if self.emitted or n_candidates <= 0:\n"
        "            return []\n"
        "        self.emitted = True\n"
        "        return [{\n"
        "            'candidate_id': 'generated-devs-candidate',\n"
        "            'format': 'parameters',\n"
        "            'spec': {\n"
        "                'production_rate': proposed_production_rate(),\n"
        "            },\n"
        "            'lineage': {'parents': []},\n"
        "            'generator': {\n"
        "                'method_id': self.definition['id'],\n"
        "                'strategy': 'deterministic-generated-devs',\n"
        "            },\n"
        "        }]\n"
        "\n"
        "    def observe(self, observations):\n"
        "        return None\n",
        encoding="utf-8",
    )

    for component_root in (environment_source, method_source):
        wheel = component_root / "vendor" / _WHEEL_NAME
        digest = _write_pure_python_wheel(wheel)
        lock = component_root / "requirements.lock"
        lock.write_text(
            (
                f"vendor/{_WHEEL_NAME} --hash=sha256:{digest}\n"
                if valid_lock
                else "generated-devs-support==1.0.0\n"
            ),
            encoding="utf-8",
        )
    return study


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class RetainedDependencyVerticalE2ETest(unittest.TestCase):
    def _new_runtime(self, root: Path) -> LocalRealmRuntime:
        runtime = LocalRealmRuntime.open(
            realm_root=root / "realm",
            actor_principal_id="operator",
        )
        self.addCleanup(runtime.close)
        return runtime

    def test_locked_wheel_is_used_by_method_and_environment_in_ordinary_run(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package_root = root / "generated-package"
        package_root.mkdir()
        study = _write_generated_devs_like_package(package_root)
        runtime = self._new_runtime(root)

        summary = run_local_realm_study(
            runtime=runtime,
            package_root=package_root,
            study_config_path=study,
            operation_id=f"retained-dependency-vertical-e2e/run/{root.name}",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            attempt_ttl_seconds=60,
            method_start_timeout=20,
            method_request_timeout=20,
        )

        self.assertEqual(summary.run_status, "succeeded")
        self.assertEqual(summary.candidate_count, 1)
        self.assertEqual(summary.successful_logical_trials, 1)
        snapshot = runtime.ledger.read_run_snapshot(
            actor_principal_id=runtime.actor_principal_id,
            run_id=summary.run_id,
        )
        self.assertEqual(len(snapshot.observations), 1)
        observation = snapshot.observations[0].envelope
        self.assertEqual(observation.outcome, "success")
        self.assertEqual(observation.metric_values["score"], 12.0)
        self.assertTrue(observation.event_summary["dependency_imported"])

        definition = runtime.ledger.read_run_definition(
            actor_principal_id=runtime.actor_principal_id,
            run_id=summary.run_id,
        )
        environment_layers = (
            definition.evaluation_closure.prepared_runtime.prepared_layers
        )
        method_layers = definition.prepared_method_runtime.prepared_layers
        self.assertEqual(len(environment_layers), 1)
        self.assertEqual(len(method_layers), 1)
        environment_layer = environment_layers[0]
        method_layer = method_layers[0]
        self.assertEqual(
            environment_layer.scope,
            ENVIRONMENT_PREPARED_PYTHON_SCOPE,
        )
        self.assertEqual(method_layer.scope, METHOD_PREPARED_PYTHON_SCOPE)
        for layer in (environment_layer, method_layer):
            self.assertEqual(layer.source_subpath, "site-packages")
            self.assertEqual(layer.destination_subpath, ".")
            self.assertEqual(layer.precedence, 0)

        refs = definition.content_refs_by_role
        self.assertEqual(
            refs[RUN_PREPARED_RUNTIME_ROLE],
            (environment_layer.snapshot_ref,),
        )
        self.assertEqual(
            refs[RUN_PREPARED_METHOD_RUNTIME_ROLE],
            (method_layer.snapshot_ref,),
        )

    def test_unlocked_dependency_fails_before_a_canonical_run_is_created(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package_root = root / "generated-package"
        package_root.mkdir()
        study = _write_generated_devs_like_package(
            package_root,
            valid_lock=False,
        )
        runtime = self._new_runtime(root)

        with self.assertRaises(RetainedStudyCompileError) as raised:
            run_local_realm_study(
                runtime=runtime,
                package_root=package_root,
                study_config_path=study,
                operation_id=(
                    f"retained-dependency-vertical-e2e/unlocked/{root.name}"
                ),
            )

        self.assertEqual(raised.exception.code, "dependency_lock_unsupported")
        self.assertEqual(runtime.run_reader.list_runs(limit=10).items, ())

    def test_registered_catalog_study_is_runnable_and_launches_its_exact_revision(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        output_root = root / "interface-outputs"
        output_root.mkdir()
        package_root = output_root / "generated-package"
        package_root.mkdir()
        _write_generated_devs_like_package(package_root)
        runtime = self._new_runtime(root)
        studio_root = root / "studio"
        studio_root.mkdir()
        state = UiState(
            cwd=studio_root,
            catalog_roots=[],
            run_roots=[],
            realm_runtime=runtime,
        )
        self.addCleanup(state.close_catalog_projections)
        self.addCleanup(state.close_coordination)

        output_session = runtime.interface_outputs.create_session(
            operation_id="retained-dependency-vertical-e2e/output-session",
            launch_id="generated-devs-interface-launch",
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        generation = runtime.interface_outputs.capture_tree_selection(
            handle=output_session,
            label="Generated DEVS project",
            relative_path="generated-package",
            root_handle="output",
            root_path=output_root,
        )
        self.assertIsNotNone(generation.ready_generation)
        assert generation.ready_generation is not None
        selection = generation.ready_generation.selection
        self.assertIsNotNone(selection)
        assert selection is not None
        kept = _keep_selection_as_ui_workspace(
            state,
            operation_id="retained-dependency-vertical-e2e/keep-output",
            selection=selection,
            title="Generated DEVS project",
            description="Generated project kept for Catalog setup",
        )
        runtime.interface_outputs.retire_session(
            operation_id="retained-dependency-vertical-e2e/retire-output-session",
            handle=output_session,
        )
        workspace = kept["workspace"]
        workspace_id = workspace["id"]
        self.assertEqual(workspace["ownership"], "realm-managed")
        self.assertEqual(
            len(runtime.editable_workspaces.list_workspaces()),
            1,
        )

        plan = _prepare_package_plan(
            state,
            workspace_id,
            {"package_id": "generated-devs-package"},
        )["package_plan"]
        checked = _validate_package_plan(state, workspace_id, plan["id"])
        plan = checked["package_plan"]
        self.assertTrue(plan["validation"]["valid"], plan)
        checked_artifact_ref = plan["artifact"]["content_ref"]
        self.assertEqual(
            plan["validation"]["artifact_ref"],
            checked_artifact_ref,
        )
        tested = _smoke_package_plan(
            state,
            workspace_id,
            plan["id"],
            {"max_trials": 1, "timeout_seconds": 60},
        )
        self.assertTrue(tested["smoke"]["valid"], tested)
        self.assertEqual(
            tested["smoke"]["artifact_ref"],
            checked_artifact_ref,
        )
        registered = _apply_package_plan(state, workspace_id, plan["id"])
        self.assertTrue(registered["applied"], registered)
        self.assertEqual(registered["setup"]["state"], "registered")
        self.assertEqual(
            registered["package_plan"]["artifact"]["content_ref"],
            checked_artifact_ref,
        )
        self.assertEqual(
            len(runtime.editable_workspaces.list_workspaces()),
            1,
        )

        head = runtime.catalog.read_head(package_id="generated-devs-package")
        self.assertIsNotNone(head)
        assert head is not None
        published_revision = runtime.catalog.read_revision(
            package_id="generated-devs-package",
            revision=head.revision,
        )
        self.assertEqual(str(published_revision.root_ref), checked_artifact_ref)
        catalog = _catalog_payload(state)
        self.assertEqual(len(catalog["studies"]), 1)
        listed = catalog["studies"][0]
        self.assertEqual(listed["ref"]["source_kind"], "realm-catalog")
        self.assertEqual(listed["ref"]["source_revision"], head.revision)
        self.assertTrue(listed["validation"]["launch"]["eligible"], listed)
        self.assertEqual(listed["validation"]["launch"]["code"], "ready")

        response, status = _execute_study_launch_request(
            state,
            {
                "schema": STUDY_LAUNCH_REQUEST_SCHEMA,
                "request_id": "e4c27498-a94d-45d5-8095-8c7653ddf73e",
                "study_ref": listed["ref"],
            },
        )

        self.assertEqual(status, HTTPStatus.CREATED, response)
        launch_id = response["launch"]["launch_id"]
        deadline = time.monotonic() + 30
        summary = None
        while time.monotonic() < deadline:
            launch = runtime.study_launches.read(launch_id=launch_id)
            if launch.run_id is not None:
                candidate = runtime.run_reader.summary(run_id=launch.run_id)
                if candidate.run_status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    summary = candidate
                    break
            if launch.job.state.terminal and launch.run_id is None:
                self.fail(f"Catalog Study launch failed before Run handoff: {launch}")
            time.sleep(0.05)
        self.assertIsNotNone(summary, "Catalog Study launch did not become terminal.")
        assert summary is not None
        self.assertEqual(summary.run_status, "succeeded")

        definition = runtime.ledger.read_run_definition(
            actor_principal_id=runtime.actor_principal_id,
            run_id=summary.run_id,
        )
        refs = definition.content_refs_by_role
        self.assertEqual(
            refs[RUN_ENVIRONMENT_SOURCE_ROLE],
            (published_revision.root_ref,),
        )
        self.assertEqual(
            refs[RUN_METHOD_SOURCE_ROLE],
            (published_revision.root_ref,),
        )
        self.assertEqual(
            refs[RUN_PREPARED_RUNTIME_ROLE],
            (
                definition.evaluation_closure.prepared_runtime.prepared_layers[
                    0
                ].snapshot_ref,
            ),
        )
        self.assertEqual(
            refs[RUN_PREPARED_METHOD_RUNTIME_ROLE],
            (definition.prepared_method_runtime.prepared_layers[0].snapshot_ref,),
        )


if __name__ == "__main__":
    unittest.main()
