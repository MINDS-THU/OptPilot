from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from optpilot.realm._validation import thaw_json
from optpilot.realm.layered_volume_realization import (
    compile_local_layered_volume_plan,
    realize_local_layered_volume_plan,
)
from optpilot.realm.local_attempt_protocol import (
    ATTEMPT_REQUEST_FILE,
    ATTEMPT_RESULT_FILE,
    LocalAttemptWorkerRequest,
    LocalAttemptWorkerResult,
    publish_exact_record,
)
from optpilot.realm.manifests import TreeEntry, TreeManifest
from optpilot.realm.refs import BlobRef
from optpilot.realm.run_records import NormalizedCandidateEnvelope
from optpilot.retained_file_candidates import sealed_file_candidate_spec
from optpilot.runtime_binding import (
    CONTROL_SCOPE,
    ENVIRONMENT_SOURCE_SCOPE,
    TRIAL_SCOPE,
    CandidateRuntimeInput,
    LayeredVolumeScopeSource,
    compile_retained_process_attempt_runtime,
)
from tests.core.test_runtime_binding import (
    _file_definition_and_candidate_input,
    _provider,
)


@unittest.skipUnless(os.name == "posix", "local attempt process is POSIX-only")
class RetainedFileAttemptRuntimeTest(unittest.TestCase):
    def test_sealed_tree_projects_and_runs_without_second_materialization_copy(self) -> None:
        (
            definition,
            evaluation_spec,
            _old_candidate_input,
            solver,
            helper,
        ) = _file_definition_and_candidate_input()
        manifest = TreeManifest.build(
            (
                TreeEntry.directory("lib"),
                TreeEntry.file(
                    "lib/helper.py",
                    blob_ref=BlobRef.from_bytes(helper),
                    size=len(helper),
                    executable=False,
                ),
                TreeEntry.file(
                    "solver.py",
                    blob_ref=BlobRef.from_bytes(solver),
                    size=len(solver),
                    executable=False,
                ),
            )
        )
        candidate_spec = sealed_file_candidate_spec(
            manifest,
            definition.evaluation_closure.environment_revision.candidate_contract,
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="files",
            spec=candidate_spec,
            content_refs=(manifest.snapshot_ref,),
        )
        candidate = thaw_json(evaluation_spec.candidate)
        candidate["spec"] = candidate_spec
        evaluation_spec = replace(
            evaluation_spec,
            candidate=candidate,
            candidate_ref=str(envelope.candidate_ref),
        )
        runtime_spec = compile_retained_process_attempt_runtime(
            owner_id="run-owner-a",
            run_definition=definition,
            evaluation_spec=evaluation_spec,
            provider=_provider(),
            candidate_input=CandidateRuntimeInput.from_envelope(envelope),
        )
        trial_scope = next(
            item for item in runtime_spec.scopes if item.name == TRIAL_SCOPE
        )
        self.assertIsInstance(trial_scope.source, LayeredVolumeScopeSource)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            projection_root = root / "projection"
            environment_root = projection_root / "environment"
            import_root = environment_root / runtime_spec.python_import_roots[0].relative_path
            candidate_root = projection_root / "candidate"
            control_root = root / CONTROL_SCOPE
            trial_root = root / TRIAL_SCOPE
            import_root.mkdir(parents=True)
            (candidate_root / "lib").mkdir(parents=True)
            control_root.mkdir()
            trial_root.mkdir()
            (candidate_root / "solver.py").write_bytes(solver)
            (candidate_root / "lib" / "helper.py").write_bytes(helper)
            (import_root / "env_impl.py").write_text(
                "from pathlib import Path\n"
                "def evaluate(candidate, context):\n"
                "    root = Path(candidate['candidateRoot'])\n"
                "    assert candidate['entrypoint'] == 'solver.py'\n"
                "    assert candidate['options']['mode'] == 'safe'\n"
                "    assert not candidate['files'][0].get('contentRef')\n"
                "    print(candidate['candidateRoot'])\n"
                "    solver = root / 'solver.py'\n"
                "    score = float('7' in solver.read_text())\n"
                "    helper = float('VALUE = 3' in "
                "(root / 'lib' / 'helper.py').read_text())\n"
                "    solver.write_text('MUTATED BY EVALUATOR\\n')\n"
                "    return {'throughput': score + helper, 'cycle_time': 1.0}\n",
                encoding="utf-8",
            )

            plan = compile_local_layered_volume_plan(
                projection_root,
                trial_scope.source.lower_layers,
                next(
                    item.quota
                    for item in runtime_spec.writable_volumes
                    if item.name == TRIAL_SCOPE
                ),
            )
            def realize(trial: Path) -> None:
                trial_fd = os.open(
                    trial,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    realize_local_layered_volume_plan(
                        projection_root, trial_fd, plan
                    )
                finally:
                    os.close(trial_fd)

            realize(trial_root)

            request = LocalAttemptWorkerRequest(
                attempt_id="attempt-file-a",
                binding_id="binding-file-a",
                launch_token="launch-file-a",
                evidence_fingerprint="d" * 64,
                evaluation_spec=evaluation_spec,
                portable_spec_digest=runtime_spec.digest,
                entrypoint=runtime_spec.entrypoint,
                python_import_roots=runtime_spec.python_import_roots,
                evaluator_settings=runtime_spec.evaluator_settings,
                declared_metric_names=runtime_spec.declared_metric_names,
                file_materialization=runtime_spec.file_materialization,
            )
            publish_exact_record(
                control_root / ATTEMPT_REQUEST_FILE, request.canonical_bytes
            )
            encoded_request = request.canonical_bytes
            self.assertNotIn(b"tree:sha256:", encoded_request)
            self.assertNotIn(b"blob:sha256:", encoded_request)
            self.assertNotIn(b"contentRef", encoded_request)
            self.assertNotIn(b"snapshotRef", encoded_request)
            self.assertNotIn(b"storeId", encoded_request)
            self.assertNotIn(str(root).encode("utf-8"), encoded_request)

            tampered_candidate = thaw_json(evaluation_spec.candidate)
            tampered_candidate["spec"]["files"][0]["sizeBytes"] += 1
            with self.assertRaisesRegex(
                ValueError, "typed retained materialization"
            ):
                replace(
                    request,
                    evaluation_spec=replace(
                        evaluation_spec, candidate=tampered_candidate
                    ),
                )

            worker_environment = {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": os.pathsep.join(
                    str(
                        environment_root
                        if item.relative_path == "."
                        else environment_root.joinpath(
                            *item.relative_path.split("/")
                        )
                    )
                    for item in runtime_spec.python_import_roots
                ),
            }
            def run_worker(
                control: Path,
                trial: Path,
                worker_request: LocalAttemptWorkerRequest,
            ) -> LocalAttemptWorkerResult:
                publish_exact_record(
                    control / ATTEMPT_REQUEST_FILE,
                    worker_request.canonical_bytes,
                )
                completed = subprocess.run(
                    (
                        sys.executable,
                        "-m",
                        "optpilot.realm._local_attempt_worker",
                        str(control),
                        str(trial),
                        str(environment_root),
                        worker_request.digest,
                    ),
                    cwd=trial,
                    env=worker_environment,
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    (completed.stdout + completed.stderr).decode(
                        "utf-8", errors="replace"
                    ),
                )
                return LocalAttemptWorkerResult.from_dict(
                    json.loads(
                        (control / ATTEMPT_RESULT_FILE).read_text(
                            encoding="utf-8"
                        )
                    )
                )

            result = run_worker(control_root, trial_root, request)

            self.assertEqual(result.envelope.outcome, "success")
            self.assertEqual(
                dict(result.envelope.metric_values),
                {"cycle_time": 1.0, "throughput": 2.0},
            )
            self.assertFalse(
                result.envelope.event_summary["materialization"][
                    "copy_performed"
                ]
            )
            self.assertEqual(len(result.logs), 1)
            self.assertIn(b"[runtime-scope-", result.logs[0].content)
            self.assertNotIn(str(root).encode("utf-8"), result.logs[0].content)
            result_bytes = (control_root / ATTEMPT_RESULT_FILE).read_bytes()
            self.assertNotIn(str(root).encode("utf-8"), result_bytes)
            self.assertEqual(
                (trial_root / "candidate" / "solver.py").read_text(
                    encoding="utf-8"
                ),
                "MUTATED BY EVALUATOR\n",
            )
            self.assertEqual(
                (candidate_root / "solver.py").read_bytes(), solver
            )

            second_control = root / "control-second"
            second_trial = root / "trial-second"
            second_control.mkdir()
            second_trial.mkdir()
            realize(second_trial)
            second_request = replace(
                request,
                attempt_id="attempt-file-b",
                binding_id="binding-file-b",
                launch_token="launch-file-b",
            )
            second = run_worker(
                second_control, second_trial, second_request
            )
            self.assertEqual(second.envelope.outcome, "success")
            self.assertEqual(
                dict(second.envelope.metric_values),
                {"cycle_time": 1.0, "throughput": 2.0},
            )
            self.assertEqual(
                (candidate_root / "solver.py").read_bytes(), solver
            )


if __name__ == "__main__":
    unittest.main()
