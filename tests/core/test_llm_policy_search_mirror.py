"""The general method and the AGV flagship share byte-identical code.

catalog/llm_policy_search is the source of truth for the trace-aware
policy-search implementation; catalog/production_agv_scheduling's
process_aware_llm is its flagship instantiation. Package boundaries
forbid cross-package imports in retained runs, so the shared files are
mirrored — this test keeps the mirror honest, exactly like the docs
mirror test does for docs_assets.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERAL = REPO_ROOT / "catalog" / "llm_policy_search" / "methods" / "llm_policy_search"
FLAGSHIP = (
    REPO_ROOT
    / "catalog"
    / "production_agv_scheduling"
    / "methods"
    / "process_aware_llm"
)

MIRRORED_FILES = (
    "method.py",
    "replay_worker.py",
    "prompts/manager.md",
    "prompts/editor.md",
)


class LlmPolicySearchMirrorTest(unittest.TestCase):
    def test_shared_method_files_are_byte_identical(self) -> None:
        for relative in MIRRORED_FILES:
            with self.subTest(file=relative):
                general = (GENERAL / relative).read_bytes()
                flagship = (FLAGSHIP / relative).read_bytes()
                self.assertEqual(
                    general,
                    flagship,
                    f"{relative} diverged between llm_policy_search and "
                    "production_agv_scheduling; edit one and copy to the "
                    "other (llm_policy_search is the source of truth).",
                )


if __name__ == "__main__":
    unittest.main()
