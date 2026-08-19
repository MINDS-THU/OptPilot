"""The Assistant cannot read the files that exist to hold credentials.

Anything an Assistant tool reads is put into the conversation and sent to
whichever model provider the person configured. So a single "read .env" turned
a local credentials file into a credential the provider now holds. This is not
hypothetical for this project: eleven live API keys were published from .env
files carried inside a committed archive, and the same files sit inside every
workspace someone might attach.

Reading is refused rather than redacted, because redaction has to be right
every time and a refusal has to be right once. Writing is refused too -- not a
leak, but overwriting someone's credentials is not the Assistant's to do.

Shell commands are handled differently and deliberately so: a command can read
a file in more ways than any list can enumerate, so instead of pretending to
block them, naming a credential file forces the approval prompt even for
someone who allowed unattended commands.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    UiState,
    _attach_agent_workspace,
    _create_agent_session,
    _create_ui_workspace,
    _execute_agent_tool,
    _is_secret_file,
    _shell_needs_approval,
    _update_agent_settings,
)

_SECRET = "sk-this-must-never-reach-the-model"


class SecretFileRecognitionTest(unittest.TestCase):
    def test_the_usual_credential_files_are_recognised(self) -> None:
        for name in (
            ".env",
            ".ENV",
            ".env.local",
            ".env.production",
            ".envrc",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".git-credentials",
            "credentials",
            "id_rsa",
            "id_rsa.bak",
            "id_ed25519",
            "server.pem",
            "app.key",
            "store.p12",
            "secrets.yaml",
            "secrets.json",
        ):
            with self.subTest(name=name):
                self.assertTrue(_is_secret_file(Path("/w") / name), name)

    def test_anything_inside_a_dot_ssh_folder_counts(self) -> None:
        self.assertTrue(_is_secret_file(Path("/w/.ssh/config")))
        self.assertTrue(_is_secret_file(Path("/w/nested/.ssh/known_hosts")))

    def test_ordinary_project_files_are_not_caught(self) -> None:
        for name in (
            "README.md",
            "study.yaml",
            "environment.yaml",
            "evaluator.py",
            "keys.md",
            "env.py",
            "public.pem.md",
        ):
            with self.subTest(name=name):
                self.assertFalse(_is_secret_file(Path("/w") / name), name)


class SecretFileRefusalTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        workspace_root = self.root / "workspace"
        (workspace_root / ".ssh").mkdir(parents=True)
        for name in (".env", "id_rsa", "server.pem"):
            (workspace_root / name).write_text(
                f"TOKEN={_SECRET}\n", encoding="utf-8"
            )
        (workspace_root / ".ssh" / "config").write_text(
            f"IdentityFile {_SECRET}\n", encoding="utf-8"
        )
        (workspace_root / "study.yaml").write_text("kind: study\n", encoding="utf-8")

        self.state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.addCleanup(self.state.close_coordination)
        for name in (
            "sessions_dir",
            "agent_sessions_dir",
            "jobs_dir",
            "workspaces_dir",
            "runtime_dir",
        ):
            setattr(self.state, name, self.root / name)
            getattr(self.state, name).mkdir(parents=True, exist_ok=True)
        self.state.settings_path = self.root / "settings.json"
        _update_agent_settings(self.state, {"openhands": {"enabled": False}})
        self.session = _create_agent_session(self.state, {"title": "Secrets"})
        workspace = _create_ui_workspace(
            self.state,
            {"title": "W", "root": str(workspace_root), "editable": True},
        )
        _attach_agent_workspace(
            self.state, self.session["id"], workspace["id"], select=True
        )

    def _call(self, tool: str, arguments: dict) -> dict:
        return _execute_agent_tool(self.state, self.session["id"], tool, arguments)

    def test_no_tool_returns_a_credential_files_contents(self) -> None:
        for target in (".env", "id_rsa", "server.pem", ".ssh/config"):
            for tool, arguments in (
                ("optpilot_file_read", {"path": target}),
                ("optpilot_file_diff", {"path": target, "content": "x"}),
                ("optpilot_file_editor", {"command": "view", "path": target}),
            ):
                with self.subTest(target=target, tool=tool):
                    with self.assertRaises(PermissionError):
                        self._call(tool, arguments)

    def test_the_refusal_says_why_and_what_to_do_instead(self) -> None:
        with self.assertRaises(PermissionError) as caught:
            self._call("optpilot_file_read", {"path": ".env"})
        message = str(caught.exception)
        self.assertIn("credentials", message)
        self.assertIn("model provider", message)
        self.assertIn("Settings", message)

    def test_a_credential_file_cannot_be_overwritten_either(self) -> None:
        with self.assertRaises(PermissionError):
            self._call(
                "optpilot_file_write", {"path": ".env", "content": "TOKEN=x\n"}
            )
        self.assertIn(
            _SECRET,
            (Path(self.root) / "workspace" / ".env").read_text(encoding="utf-8"),
        )

    def test_ordinary_files_are_unaffected(self) -> None:
        result = self._call("optpilot_file_read", {"path": "study.yaml"})
        self.assertTrue(result["ok"], result)
        self.assertIn("kind: study", result["data"]["content"])

    def test_no_refusal_message_quotes_the_secret(self) -> None:
        for target in (".env", "id_rsa"):
            with self.subTest(target=target):
                try:
                    result = self._call("optpilot_file_read", {"path": target})
                except PermissionError as error:
                    self.assertNotIn(_SECRET, str(error))
                else:
                    self.assertNotIn(_SECRET, json.dumps(result))


class SecretShellCommandTest(unittest.TestCase):
    def test_naming_a_credential_file_forces_the_prompt(self) -> None:
        for command in (
            ["cat", ".env"],
            ["grep", "KEY", ".env.production"],
            ["cp", "id_rsa", "/tmp/x"],
            ["sh", "-c", "cat .env"],
        ):
            with self.subTest(command=command):
                self.assertTrue(_shell_needs_approval(command), command)

    def test_ordinary_commands_are_still_unattended(self) -> None:
        for command in (
            ["cat", "README.md"],
            ["ls", "-la"],
            ["python", "-c", "print(1)"],
        ):
            with self.subTest(command=command):
                self.assertFalse(_shell_needs_approval(command), command)


if __name__ == "__main__":
    unittest.main()
