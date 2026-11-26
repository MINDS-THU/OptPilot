import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devs_display.backend.interface_outputs import (
    OUTPUT_ROOT_ENV,
    OUTPUT_SCHEMA,
    OUTPUTS_FILE_ENV,
    InterfaceOutputPublisher,
    stable_tree_digest,
)


class InterfaceOutputPublisherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output_root = self.root / "output"
        self.control = self.root / "control" / "outputs.jsonl"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _project(self, name="supply_chain"):
        project = self.workspace / name
        (project / "devs_project" / "_analysis_logs").mkdir(parents=True)
        (project / "run.py").write_text("print('ready')\n", encoding="utf-8")
        (project / "simulation.json").write_text(
            json.dumps(
                {
                    "schema_version": "devs.simulation.v1",
                    "entrypoint": "run.py",
                    "timeout_seconds": 30,
                    "arguments": [],
                    "result_files": [],
                }
            ),
            encoding="utf-8",
        )
        (project / "README.md").write_text("# Ready\n", encoding="utf-8")
        (project / "devs_project" / "model.py").write_text(
            "class Model: pass\n", encoding="utf-8"
        )
        return {
            "project_id": "proj_supply_chain",
            "display_name": "Supply chain",
            "path": name,
            "version": 3,
        }

    def test_environment_contract_is_optional_but_handles_are_paired(self):
        self.assertIsNone(InterfaceOutputPublisher.from_environment({}))
        with self.assertRaisesRegex(ValueError, "supplied together"):
            InterfaceOutputPublisher.from_environment(
                {OUTPUT_ROOT_ENV: str(self.output_root)}
            )

    def test_publishes_complete_bundle_with_one_private_committed_line(self):
        publisher = InterfaceOutputPublisher(self.output_root, self.control)
        record = publisher.publish_ready_project(
            session_id="sess_01",
            request_id="req_01",
            workspace=self.workspace,
            project=self._project(),
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["schema_version"], OUTPUT_SCHEMA)
        self.assertEqual(record["kind"], "tree")
        self.assertEqual(record["root"], "output")
        self.assertRegex(record["id"], r"^devs-[0-9a-f]{32}$")
        self.assertEqual(record["path"], f"generations/{record['id']}")
        generation = self.output_root / record["path"]
        self.assertTrue((generation / "run.py").is_file())
        self.assertTrue((generation / "simulation.json").is_file())
        self.assertTrue((generation / "README.md").is_file())
        self.assertTrue((generation / "devs_project").is_dir())

        payload = self.control.read_bytes()
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(payload.count(b"\n"), 1)
        self.assertEqual(json.loads(payload), record)
        self.assertNotIn(str(self.root), payload.decode("utf-8"))
        self.assertEqual(stat.S_IMODE(self.control.stat().st_mode), 0o600)

    def test_visualizer_marker_path_publishes_its_runnable_parent(self):
        project = self._project()
        project["path"] = f"{project['path']}/devs_project"
        publisher = InterfaceOutputPublisher(self.output_root, self.control)

        record = publisher.publish_ready_project(
            session_id="sess_01",
            request_id="req_01",
            workspace=self.workspace,
            project=project,
        )

        self.assertIsNotNone(record)
        assert record is not None
        generation = self.output_root / record["path"]
        self.assertTrue((generation / "run.py").is_file())
        self.assertTrue((generation / "README.md").is_file())
        self.assertTrue((generation / "devs_project" / "model.py").is_file())

    def test_incomplete_bundle_is_not_announced(self):
        project = self._project("incomplete")
        (self.workspace / "incomplete" / "README.md").unlink()
        publisher = InterfaceOutputPublisher(self.output_root, self.control)

        result = publisher.publish_ready_project(
            session_id="sess_01",
            request_id="req_01",
            workspace=self.workspace,
            project=project,
        )

        self.assertIsNone(result)
        self.assertEqual(self.control.read_bytes(), b"")
        self.assertEqual(list((self.output_root / "generations").iterdir()), [])

    def test_publication_is_fenced_by_the_validated_content_digest(self):
        project = self._project()
        bundle = self.workspace / project["path"]
        expected_digest = stable_tree_digest(bundle)
        publisher = InterfaceOutputPublisher(self.output_root, self.control)

        record = publisher.publish_ready_project(
            session_id="sess_01",
            request_id="req_01",
            workspace=self.workspace,
            project=project,
            expected_content_digest=expected_digest,
        )
        self.assertIsNotNone(record)

        changed = self._project("changed")
        changed_bundle = self.workspace / changed["path"]
        stale_digest = stable_tree_digest(changed_bundle)
        (changed_bundle / "run.py").write_text("print('changed')\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "successful validation"):
            publisher.publish_ready_project(
                session_id="sess_02",
                request_id="req_02",
                workspace=self.workspace,
                project=changed,
                expected_content_digest=stale_digest,
            )

    def test_replay_is_idempotent_and_does_not_append_a_second_line(self):
        project = self._project()
        publisher = InterfaceOutputPublisher(self.output_root, self.control)
        first = publisher.publish_ready_project(
            session_id="sess_01",
            request_id="req_01",
            workspace=self.workspace,
            project=project,
        )
        second = publisher.publish_ready_project(
            session_id="sess_01",
            request_id="a-later-manual-run",
            workspace=self.workspace,
            project=project,
        )

        self.assertEqual(second, first)
        self.assertEqual(self.control.read_bytes().count(b"\n"), 1)

        restarted = InterfaceOutputPublisher(self.output_root, self.control)
        third = restarted.publish_ready_project(
            session_id="sess_01",
            request_id="req_01",
            workspace=self.workspace,
            project=project,
        )
        self.assertEqual(third, first)
        self.assertEqual(self.control.read_bytes().count(b"\n"), 1)

    def test_source_permissions_are_normalized_without_losing_executable_intent(self):
        project = self._project()
        source_utils = (
            self.workspace / project["path"] / "devs_project" / "devs_utils"
        )
        source_utils.mkdir()
        source_module = source_utils / "runtime.py"
        source_module.write_text("VALUE = 1\n", encoding="utf-8")
        source_runner = source_utils / "run-model"
        source_runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(source_module, 0o400)
        os.chmod(source_runner, 0o555)
        os.chmod(source_utils, 0o500)

        publisher = InterfaceOutputPublisher(self.output_root, self.control)
        generation_utils = None
        try:
            first = publisher.publish_ready_project(
                session_id="sess_01",
                request_id="req_01",
                workspace=self.workspace,
                project=project,
            )
            second = publisher.publish_ready_project(
                session_id="sess_01",
                request_id="req_01",
                workspace=self.workspace,
                project=project,
            )

            self.assertEqual(second, first)
            assert first is not None
            generation_utils = (
                self.output_root
                / first["path"]
                / "devs_project"
                / "devs_utils"
            )
            self.assertEqual(stat.S_IMODE(source_utils.stat().st_mode), 0o500)
            self.assertEqual(stat.S_IMODE(source_module.stat().st_mode), 0o400)
            self.assertEqual(stat.S_IMODE(source_runner.stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE(generation_utils.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((generation_utils / "runtime.py").stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE((generation_utils / "run-model").stat().st_mode),
                0o700,
            )
            self.assertEqual(self.control.read_bytes().count(b"\n"), 1)
        finally:
            os.chmod(source_utils, 0o700)
            os.chmod(source_module, 0o600)
            os.chmod(source_runner, 0o600)
            if generation_utils is not None and generation_utils.exists():
                os.chmod(generation_utils, 0o700)
                os.chmod(generation_utils / "runtime.py", 0o600)
                os.chmod(generation_utils / "run-model", 0o600)

    def test_generation_byte_limit_is_checked_before_publication(self):
        project = self._project()
        publisher = InterfaceOutputPublisher(self.output_root, self.control)
        with patch(
            "devs_display.backend.interface_outputs._MAX_GENERATION_BYTES", 4
        ):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                publisher.publish_ready_project(
                    session_id="sess_01",
                    request_id="req_01",
                    workspace=self.workspace,
                    project=project,
                )
        self.assertEqual(self.control.read_bytes(), b"")

    def test_rejects_symlink_and_traversing_project_paths(self):
        project = self._project()
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (self.workspace / project["path"] / "devs_project" / "escape").symlink_to(
            outside
        )
        publisher = InterfaceOutputPublisher(self.output_root, self.control)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            publisher.publish_ready_project(
                session_id="sess_01",
                request_id="req_01",
                workspace=self.workspace,
                project=project,
            )
        with self.assertRaisesRegex(ValueError, "traverse"):
            publisher.publish_ready_project(
                session_id="sess_01",
                request_id="req_02",
                workspace=self.workspace,
                project={**project, "path": "../outside"},
            )

    def test_existing_control_file_is_appended_without_truncation(self):
        self.control.parent.mkdir()
        prior = {
            "schema_version": OUTPUT_SCHEMA,
            "id": "prior-output",
            "label": "Prior output",
            "kind": "file",
            "root": "output",
            "path": "prior.txt",
        }
        prior_payload = json.dumps(prior, separators=(",", ":")) + "\n"
        self.control.write_text(prior_payload, encoding="utf-8")
        os.chmod(self.control, 0o644)

        InterfaceOutputPublisher(self.output_root, self.control)

        self.assertEqual(self.control.read_text(encoding="utf-8"), prior_payload)
        self.assertEqual(stat.S_IMODE(self.control.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
