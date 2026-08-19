from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.refs import BlobRef, SnapshotRef
from optpilot.realm.run_records import RUN_CANDIDATE_ROLE
from optpilot.realm_study_runner import run_local_realm_study
from optpilot.retained_batch_runtime import RetainedPythonBatchRuntime
from optpilot.retained_file_candidates import FileCandidateDraft
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


_ORIGINAL_SOLVER = "VALUE = 7\n"
_MUTATED_SOLVER = "VALUE = -1\n"


def _write_file_candidate_package(root: Path) -> Path:
    study = root / "configs" / "studies" / "study.yaml"
    environment = root / "configs" / "environments" / "environment.yaml"
    method = root / "configs" / "methods" / "method.yaml"
    for path in (study, environment, method):
        path.parent.mkdir(parents=True, exist_ok=True)

    study.write_text(
        """\
apiVersion: optpilot.io/v1
config: study
name: retained-file-vertical-e2e
description: Exercise the complete retained file-candidate path.
environmentConfig: ../environments/environment.yaml
methodConfig: ../methods/method.yaml
objective:
  metric: score
  direction: maximize
budget:
  maxTrials: 2
execution:
  parallelism: 1
  timeoutSeconds: 30
reproducibility:
  seed: 11
""",
        encoding="utf-8",
    )
    environment.write_text(
        """\
apiVersion: optpilot.io/v1
config: environment
id: retained-file-vertical-environment
description: Mutate each trial-private candidate after proving it is fresh.
evaluator:
  python: local_package.evaluate:evaluate
  pythonPath: [../..]
  settings: {}
candidate:
  format: files
  description: One Python solver file.
  materialize:
    root: candidate
  files:
    editable:
      - path: solver.py
    required: [solver.py]
    allow: [solver.py]
metrics:
  source: return
  keys: [score]
""",
        encoding="utf-8",
    )
    method.write_text(
        """\
apiVersion: optpilot.io/v1
config: method
id: retained-file-vertical-method
description: Stage two distinct candidates with byte-identical trees.
entrypoint:
  python: local_package.method:RetainedMethod
  pythonPath: [../..]
  protocol: batch
settings:
  batchSize: 2
accepts:
  formats: [files]
  requires:
    context: [candidate.files.editable]
""",
        encoding="utf-8",
    )

    package = root / "local_package"
    template = package / "candidate_template"
    template.mkdir(parents=True)
    (template / "solver.py").write_text(_ORIGINAL_SOLVER, encoding="utf-8")
    (package / "evaluate.py").write_text(
        "from pathlib import Path\n"
        f"EXPECTED = {_ORIGINAL_SOLVER!r}\n"
        f"MUTATED = {_MUTATED_SOLVER!r}\n"
        "def evaluate(candidate, context):\n"
        "    solver = Path(candidate['candidateRoot']) / 'solver.py'\n"
        "    before = solver.read_text(encoding='utf-8')\n"
        "    if before != EXPECTED:\n"
        "        raise RuntimeError('candidate trial did not start fresh')\n"
        "    solver.write_text(MUTATED, encoding='utf-8')\n"
        "    after = solver.read_text(encoding='utf-8')\n"
        "    return {\n"
        "        'score': 1.0,\n"
        "        'event_summary': {\n"
        "            'fresh_before_mutation': before == EXPECTED,\n"
        "            'mutated_in_trial': after == MUTATED,\n"
        "        },\n"
        "    }\n",
        encoding="utf-8",
    )
    (package / "method.py").write_text(
        "from pathlib import Path\n"
        "from optpilot.candidate_staging import CandidateBundleStager\n"
        "class RetainedMethod:\n"
        "    def __init__(self, definition, study_spec, rng):\n"
        "        self.definition = definition\n"
        "        self.source = Path(__file__).with_name('candidate_template')\n"
        "        self.emitted = False\n"
        "    def propose(self, n_candidates, study_state, evidence_view):\n"
        "        if self.emitted:\n"
        "            return []\n"
        "        self.emitted = True\n"
        "        staging = study_state['runtime_context']['candidate_staging_dir']\n"
        "        stager = CandidateBundleStager(staging)\n"
        "        return [\n"
        "            stager.stage_directory(\n"
        "                self.source,\n"
        "                candidate_id=f'candidate-{index}',\n"
        "                lineage={'parents': []},\n"
        "                generator={\n"
        "                    'method_id': self.definition['id'],\n"
        "                    'strategy': 'vertical_e2e',\n"
        "                },\n"
        "            )\n"
        "            for index in range(n_candidates)\n"
        "        ]\n"
        "    def observe(self, observations):\n"
        "        return None\n",
        encoding="utf-8",
    )
    return study


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class RetainedFileCandidateVerticalE2ETest(unittest.TestCase):
    def test_staged_identical_trees_are_atomically_admitted_and_each_attempt_is_fresh(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package_root = root / "package"
        package_root.mkdir()
        study = _write_file_candidate_package(package_root)
        runtime = LocalRealmRuntime.open(
            realm_root=root / "realm",
            actor_principal_id="operator",
        )
        self.addCleanup(runtime.close)

        frozen_candidates: list[dict[str, object]] = []
        original_request = RetainedPythonBatchRuntime.request

        def capture_request(handle, exchange_id, operation, payload, **kwargs):
            response = original_request(
                handle,
                exchange_id,
                operation,
                payload,
                **kwargs,
            )
            if operation == "propose":
                result = response.to_dict()["result"]
                frozen_candidates.extend(result["candidates"])
            return response

        with mock.patch.object(
            RetainedPythonBatchRuntime,
            "request",
            new=capture_request,
        ):
            summary = run_local_realm_study(
                runtime=runtime,
                package_root=package_root,
                study_config_path=study,
                operation_id="retained-file-vertical-e2e/run",
                controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
                attempt_ttl_seconds=60,
                method_start_timeout=20,
                method_request_timeout=20,
            )

        self.assertEqual(summary.run_status, "succeeded")
        self.assertEqual(summary.candidate_count, 2)
        self.assertEqual(summary.accepted_logical_trials, 2)
        self.assertEqual(summary.successful_logical_trials, 2)
        self.assertEqual(summary.attempt_count, 2)
        self.assertEqual(summary.observations_by_outcome["success"], 2)

        self.assertEqual(len(frozen_candidates), 2)
        drafts = tuple(
            FileCandidateDraft.from_candidate(candidate)
            for candidate in frozen_candidates
        )
        self.assertEqual(
            tuple(draft.draft.selection for draft in drafts),
            ("candidate-00000000/files", "candidate-00000001/files"),
        )
        self.assertEqual(
            {draft.candidate_id for draft in drafts},
            {"candidate-0", "candidate-1"},
        )
        self.assertEqual(len({draft.draft.token for draft in drafts}), 2)
        self.assertTrue(
            all(draft.draft.token.startswith("draft-v1-") for draft in drafts)
        )
        for candidate in frozen_candidates:
            rendered = repr(candidate)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("bundleRef", rendered)
            self.assertNotIn("contentRef", rendered)

        snapshot = runtime.ledger.read_run_snapshot(
            actor_principal_id=runtime.actor_principal_id,
            run_id=summary.run_id,
        )
        self.assertEqual(len(snapshot.candidates), 2)
        candidate_snapshots = tuple(
            candidate.admission.envelope.content_refs[0]
            for candidate in snapshot.candidates
        )
        self.assertTrue(
            all(isinstance(item, SnapshotRef) for item in candidate_snapshots)
        )
        self.assertEqual(len(set(candidate_snapshots)), 1)
        self.assertEqual(
            len({candidate.candidate_ref for candidate in snapshot.candidates}),
            1,
        )

        proposal = next(
            item
            for item in snapshot.method_exchange_completions
            if item.kind == "proposal"
        )
        self.assertEqual(proposal.outcome, "admitted")
        admission_txn_ids = {
            *(item.accepted_txn_id for item in snapshot.candidates),
            *(item.accepted_txn_id for item in snapshot.logical_trials),
        }
        self.assertEqual(admission_txn_ids, {proposal.completed_txn_id})

        memberships = runtime.ledger.list_owner_memberships(
            actor_principal_id=runtime.actor_principal_id,
            owner_id=snapshot.run.owner_id,
        )
        candidate_memberships = tuple(
            item for item in memberships if item.role == RUN_CANDIDATE_ROLE
        )
        self.assertEqual(len(candidate_memberships), 1)
        self.assertEqual(
            candidate_memberships[0].content_ref,
            candidate_snapshots[0],
        )

        self.assertEqual(len(snapshot.execution_bindings), 2)
        self.assertEqual(len(snapshot.execution_launch_intents), 2)
        self.assertEqual(len(snapshot.execution_terminal_evidence), 2)
        self.assertEqual(len(snapshot.execution_cleanup_authorizations), 2)
        self.assertEqual(
            len({item.launch_token for item in snapshot.execution_launch_intents}),
            2,
        )
        self.assertTrue(
            all(
                item.started and item.disposition == "exited"
                for item in snapshot.execution_terminal_evidence
            )
        )
        self.assertTrue(
            all(
                binding.portable_spec.file_materialization is not None
                for binding in snapshot.execution_bindings
            )
        )
        self.assertEqual(len(snapshot.observations), 2)
        for observation in snapshot.observations:
            self.assertEqual(observation.envelope.outcome, "success")
            self.assertTrue(
                observation.envelope.event_summary["fresh_before_mutation"]
            )
            self.assertTrue(
                observation.envelope.event_summary["mutated_in_trial"]
            )
            self.assertFalse(
                observation.envelope.materialization["metadata"]["copy_performed"]
            )
        # The second evaluator process would fail before returning metrics if
        # the first process's trial-local write leaked into the shared input.
        self.assertTrue(
            snapshot.observations[1].envelope.event_summary[
                "fresh_before_mutation"
            ]
        )

        manifest = runtime.content_store.verify_tree(candidate_snapshots[0])
        solver_entry = next(
            entry for entry in manifest.entries if entry.path == "solver.py"
        )
        self.assertEqual(
            solver_entry.blob_ref,
            BlobRef.from_bytes(_ORIGINAL_SOLVER.encode("utf-8")),
        )
        self.assertEqual(
            (
                package_root
                / "local_package"
                / "candidate_template"
                / "solver.py"
            ).read_text(encoding="utf-8"),
            _ORIGINAL_SOLVER,
        )


if __name__ == "__main__":
    unittest.main()
