"""The Assistant's advertised tools must equal its executable tools.

An advertised-but-unexecutable tool is worse than a missing one: the model
calls it, no result ever comes back, and the turn hangs. These assertions make
any future drift a visible test failure instead of a silent hang.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from optpilot_studio import agent


class AssistantToolDriftTest(unittest.TestCase):
    def test_native_openhands_tools_never_bypass_workspace_file_policy(self) -> None:
        self.assertEqual(agent.DEFAULT_OPENHANDS_NATIVE_TOOLS, ("task_tracker",))
        self.assertEqual(agent.ALLOWED_OPENHANDS_NATIVE_TOOLS, {"task_tracker"})

    def test_every_advertised_tool_is_executable_and_vice_versa(self) -> None:
        source = Path(agent.__file__).read_text(encoding="utf-8")
        advertised = set(re.findall(r'"name":\s*"(optpilot_[a-z_]+)"', source))
        executable = set(agent.OPTPILOT_AGENT_TOOLS)
        self.assertEqual(
            advertised - executable,
            set(),
            "advertised to the model but never executed: the turn would hang",
        )
        self.assertEqual(
            executable - advertised,
            set(),
            "executable but never advertised: dead dispatch code",
        )

    def test_execution_tool_descriptions_match_the_approval_policy(self) -> None:
        descriptions = {
            str(spec.get("name")): str(spec.get("description") or "")
            for spec in agent.OPTPILOT_AGENT_TOOL_SPECS
        }
        for name in ("optpilot_shell_run", "optpilot_terminal"):
            with self.subTest(tool=name):
                self.assertIn(
                    "every command requires explicit studio approval",
                    descriptions[name].lower(),
                )
                self.assertNotIn("Risky", descriptions[name])
                self.assertNotIn("risky", descriptions[name])
        self.assertIn("explicit approval", descriptions["optpilot_smoke_test_study"])

    def _prompt_paths(self) -> list[Path]:
        """Both copies of the guidance file.

        A source checkout's runtime prefers the .agents copy while the
        published one ships from the package, so a rule taught in only one
        place is a rule the Assistant may not actually follow. A separate
        test asserts they are byte-identical; this one asserts each is
        independently honest, so neither can teach a tool that does not exist.
        """

        packaged = (
            Path(agent.__file__).parent / "assistant_assets" / "prompts" / "system.md"
        )
        source = (
            Path(agent.__file__).resolve().parents[3]
            / ".agents"
            / "optpilot-assistant"
            / "prompts"
            / "system.md"
        )
        return [path for path in (packaged, source) if path.is_file()]

    def test_the_prompt_only_teaches_tools_that_exist(self) -> None:
        paths = self._prompt_paths()
        self.assertTrue(paths, "no guidance file found")
        for path in paths:
            with self.subTest(prompt=str(path)):
                self._assert_prompt_is_honest(path)

    def _assert_prompt_is_honest(self, path: Path) -> None:
        prompt = path.read_text(encoding="utf-8")
        self.assertIn(
            "Use native OpenHands planning or task-tracking tools only",
            prompt,
        )
        self.assertIn("Do not bypass Studio's", prompt)
        self.assertIn("every shell command", prompt)
        self.assertIn("every smoke-test execution", prompt)
        self.assertNotIn("risky shell", prompt)
        self.assertNotIn("Smoke tests do not", prompt)
        taught = set(re.findall(r"`(optpilot_[a-z_]+)`", prompt))
        # The prompt also names the optpilot_configs/ directory in backticks;
        # it shares the prefix but is a folder, not a tool.
        taught.discard("optpilot_configs")
        unknown = taught - set(agent.OPTPILOT_AGENT_TOOLS)
        self.assertEqual(
            unknown,
            set(),
            "the prompt teaches tools the Assistant cannot call",
        )


if __name__ == "__main__":
    unittest.main()
