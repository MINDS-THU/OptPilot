"""Assistant tools never cross into Studio's project-local authority state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    STUDIO_CONTROL_DIRECTORY_NAME,
    UiState,
    _attach_agent_workspace,
    _create_agent_session,
    _create_ui_workspace,
    _execute_agent_tool,
    _prepare_transient_interface_runtime_handles,
    _update_agent_settings,
)


_SECRET = "sk-control-path-regression"


class AssistantControlPathTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.addCleanup(self.state.close_coordination)
        _update_agent_settings(
            self.state,
            {
                "openhands": {
                    "enabled": False,
                    "api_key": _SECRET,
                }
            },
        )
        self.session = _create_agent_session(self.state, {"title": "Control paths"})
        self.workspace = _create_ui_workspace(
            self.state,
            {
                "id": "project",
                "title": "Project",
                "root": str(self.root),
                "mode": "editable",
                "initialize_if_empty": False,
            },
        )
        _attach_agent_workspace(
            self.state, self.session["id"], self.workspace["id"], select=True
        )

    def _call(self, tool: str, arguments: dict) -> dict:
        return _execute_agent_tool(
            self.state, self.session["id"], tool, arguments
        )

    def test_production_settings_cannot_be_read_written_diffed_or_edited(self) -> None:
        target = f"{STUDIO_CONTROL_DIRECTORY_NAME}/settings.json"
        calls = (
            ("optpilot_file_read", {"path": target}),
            ("optpilot_file_write", {"path": target, "content": "{}\n"}),
            ("optpilot_file_diff", {"path": target, "content": "{}\n"}),
            (
                "optpilot_file_editor",
                {"command": "view", "path": target},
            ),
        )
        for tool, arguments in calls:
            with self.subTest(tool=tool):
                with self.assertRaises(PermissionError) as caught:
                    self._call(tool, arguments)
                self.assertIn("Studio control state", str(caught.exception))
                self.assertNotIn(_SECRET, str(caught.exception))
        self.assertIn(
            _SECRET, self.state.settings_path.read_text(encoding="utf-8")
        )

    def test_control_tree_is_neither_addressable_nor_listed(self) -> None:
        with self.assertRaises(PermissionError):
            self._call(
                "optpilot_file_tree", {"path": STUDIO_CONTROL_DIRECTORY_NAME}
            )

        result = self._call("optpilot_file_tree", {"path": "."})
        listed = [str(item.get("path") or "") for item in result["data"]["files"]]
        self.assertFalse(
            any(
                path == STUDIO_CONTROL_DIRECTORY_NAME
                or path.startswith(STUDIO_CONTROL_DIRECTORY_NAME + "/")
                for path in listed
            ),
            listed,
        )

    def test_config_tools_cannot_bypass_the_selected_workspace_guard(self) -> None:
        # Config tools accept an implicit path relative to the selected
        # Workspace. That convenience path must enforce the same control-state
        # and credential-file rules as the ordinary file tools.
        with self.assertRaises(PermissionError):
            self._call(
                "optpilot_config_validate",
                {"path": f"{STUDIO_CONTROL_DIRECTORY_NAME}/settings.json"},
            )

        credential = self.root / ".env"
        credential.write_text("TOKEN=do-not-send\n", encoding="utf-8")
        with self.assertRaises(PermissionError):
            self._call("optpilot_config_validate", {"path": ".env"})

    def test_a_symlink_cannot_turn_a_project_path_into_a_control_path(self) -> None:
        alias = self.root / "settings-link.json"
        alias.symlink_to(self.state.settings_path)
        for tool, arguments in (
            ("optpilot_file_read", {"path": alias.name}),
            ("optpilot_file_write", {"path": alias.name, "content": "{}\n"}),
        ):
            with self.subTest(tool=tool):
                with self.assertRaises(PermissionError):
                    self._call(tool, arguments)

    def test_recursive_tree_hides_control_symlinks_and_does_not_loop(self) -> None:
        control_alias = self.root / "control-alias"
        control_alias.symlink_to(
            self.root / STUDIO_CONTROL_DIRECTORY_NAME,
            target_is_directory=True,
        )
        loop = self.root / "project-loop"
        loop.symlink_to(self.root, target_is_directory=True)

        result = self._call("optpilot_file_tree", {"path": ".", "max_files": 500})
        paths = [str(item.get("path") or "") for item in result["data"]["files"]]

        self.assertNotIn("control-alias", paths)
        self.assertNotIn("project-loop", paths)
        self.assertFalse(any("settings.json" in path for path in paths), paths)

    def test_control_roots_cannot_be_created_or_attached_by_the_assistant(self) -> None:
        with self.assertRaises(PermissionError):
            self._call(
                "optpilot_workspace_create",
                {
                    "id": "bad-create",
                    "title": "Bad",
                    "root": str(self.root / STUDIO_CONTROL_DIRECTORY_NAME),
                    "initialize_if_empty": False,
                },
            )

        legacy = _create_ui_workspace(
            self.state,
            {
                "id": "legacy-control",
                "title": "Legacy control",
                "root": str(self.root / STUDIO_CONTROL_DIRECTORY_NAME),
                "initialize_if_empty": False,
            },
        )
        with self.assertRaises(PermissionError):
            _attach_agent_workspace(
                self.state, self.session["id"], legacy["id"], select=True
            )

    def test_a_relocated_control_file_inside_a_workspace_fails_closed(self) -> None:
        # Embedders can relocate Settings. Unlike the standard `.optpilot-ui`
        # layout, an arbitrary filename cannot be covered by the fixed runtime
        # mask, so a containing Workspace must not be attached.
        relocated = self.root / "studio-authority.json"
        relocated.write_text(_SECRET, encoding="utf-8")
        self.state.settings_path = relocated
        other_session = _create_agent_session(
            self.state, {"title": "Relocated authority"}
        )
        with self.assertRaises(PermissionError):
            _attach_agent_workspace(
                self.state,
                other_session["id"],
                self.workspace["id"],
                select=True,
            )

    def test_explicit_shell_and_terminal_control_reads_stop_before_runtime(self) -> None:
        for tool, arguments in (
            (
                "optpilot_shell_run",
                {"command": ["cat", ".optpilot-ui/settings.json"]},
            ),
            (
                "optpilot_terminal",
                {"command": "cat .optpilot-ui/settings.json"},
            ),
        ):
            with self.subTest(tool=tool):
                with self.assertRaises(PermissionError):
                    self._call(tool, arguments)

    def test_workspace_container_masks_indirect_shell_access(self) -> None:
        command = self.state.workspace_runtime._container_run_command(
            "docker", self.workspace, "optpilot-test", 18765
        )
        tmpfs_mounts = [
            command[index + 1]
            for index, item in enumerate(command[:-1])
            if item == "--tmpfs"
        ]
        self.assertTrue(
            any(
                mount.startswith(
                    str(
                        (self.root / STUDIO_CONTROL_DIRECTORY_NAME).resolve()
                    )
                    + ":"
                )
                for mount in tmpfs_mounts
            ),
            command,
        )
        # Runtime metadata/claim files stay host-side; only explicitly safe
        # content subdirectories are mounted into the container.
        runtime_root_mount = (
            f"{self.state.workspace_runtime._workspace_runtime_dir('project')}:"
            f"{self.state.workspace_runtime._workspace_runtime_dir('project')}:rw"
        )
        self.assertNotIn(runtime_root_mount, command)

    def test_workspace_container_mounts_only_launch_owned_interface_content(
        self,
    ) -> None:
        runtime = self.state.workspace_runtime
        launch_workspace = {
            **self.workspace,
            "id": "interface-launch-test",
            "source_type": "catalog-interface",
            "mode": "read-only",
            "launch_id": "launch-test",
        }
        handles = _prepare_transient_interface_runtime_handles(
            self.state,
            launch_workspace,
            outputs_enabled=True,
            output_actions_enabled=True,
        )
        runtime_dir = runtime._workspace_runtime_dir(launch_workspace["id"])
        launch_content = {
            Path(handles["OPTPILOT_INTERFACE_RUNTIME_ROOT"]),
            Path(handles["OPTPILOT_INTERFACE_OUTPUT_ROOT"]),
            Path(handles["OPTPILOT_INTERFACE_OUTPUTS_FILE"]).parent,
            Path(handles["OPTPILOT_INTERFACE_OUTPUT_ACTION_ROOT"]),
        }

        command = runtime._container_run_command(
            "docker", launch_workspace, "optpilot-interface-test", 18765
        )
        mounts = [
            command[index + 1]
            for index, item in enumerate(command[:-1])
            if item == "-v"
        ]
        for path in launch_content:
            with self.subTest(path=path.name):
                self.assertEqual(mounts.count(f"{path}:{path}:rw"), 1)

        # The parent still holds claim and lifecycle authority and must never
        # be exposed merely to make the launch-owned children writable.
        self.assertNotIn(f"{runtime_dir}:{runtime_dir}:rw", mounts)
        self.assertFalse(
            any(".interface-output-executions" in mount for mount in mounts),
            mounts,
        )

    def test_workspace_runtime_environment_uses_mounted_content_child(self) -> None:
        runtime = self.state.workspace_runtime
        runtime_dir = runtime._workspace_runtime_dir("project")
        environment = runtime._runtime_env(self.workspace)
        workspace_data = runtime_dir / "workspace-data"
        self.assertEqual(
            environment["OPTPILOT_WORKSPACE_RUNTIME_DIR"], str(workspace_data)
        )

        command = runtime._container_run_command(
            "docker", self.workspace, "optpilot-runtime-data-test", 18765
        )
        self.assertIn(f"{workspace_data}:{workspace_data}:rw", command)
        self.assertNotIn(f"{runtime_dir}:{runtime_dir}:rw", command)

    def test_workspace_container_rejects_linked_interface_content(self) -> None:
        runtime = self.state.workspace_runtime
        runtime_dir = runtime._ensure_workspace_runtime_dir("project")
        external = self.root / "external-control"
        external.mkdir()
        (runtime_dir / "control").symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(
            RuntimeError, "runtime content mount must be a directory"
        ):
            runtime._container_run_command(
                "docker", self.workspace, "optpilot-interface-test", 18765
            )

    def test_workspace_container_rejects_linked_fixed_runtime_content(self) -> None:
        runtime = self.state.workspace_runtime
        for workspace_id, relative in (
            ("linked-home", ("home",)),
            ("linked-cache", ("cache",)),
        ):
            with self.subTest(path="/".join(relative)):
                runtime_dir = runtime._ensure_workspace_runtime_dir(workspace_id)
                external = self.root / f"external-{workspace_id}"
                external.mkdir()
                runtime_dir.joinpath(*relative).symlink_to(
                    external, target_is_directory=True
                )
                workspace = {**self.workspace, "id": workspace_id}

                with self.assertRaisesRegex(
                    RuntimeError, "runtime content path must be a directory"
                ):
                    runtime._container_run_command(
                        "docker", workspace, "optpilot-linked-test", 18765
                    )
                self.assertEqual(list(external.iterdir()), [])

    def test_studio_managed_workspace_content_remains_usable(self) -> None:
        created = self._call(
            "optpilot_workspace_create", {"id": "managed", "title": "Managed"}
        )
        self.assertTrue(created["ok"], created)
        written = self._call(
            "optpilot_file_write", {"path": "notes.txt", "content": "safe\n"}
        )
        self.assertTrue(written["ok"], written)
        read = self._call("optpilot_file_read", {"path": "notes.txt"})
        self.assertEqual(read["data"]["content"], "safe\n")


if __name__ == "__main__":
    unittest.main()
