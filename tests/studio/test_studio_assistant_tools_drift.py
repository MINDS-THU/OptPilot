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

    def test_the_prompt_only_teaches_tools_that_exist(self) -> None:
        prompt = (
            Path(agent.__file__).parent
            / "assistant_assets"
            / "prompts"
            / "system.md"
        ).read_text(encoding="utf-8")
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
