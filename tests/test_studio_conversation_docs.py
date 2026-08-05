"""Documentation contracts for the conversation-first Studio vocabulary."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _ROOT / "docs"
_PACKAGED = (
    _ROOT / "studio" / "src" / "optpilot_studio" / "docs_assets"
)


class StudioConversationDocsTest(unittest.TestCase):
    def _source(self, name: str) -> str:
        return (_DOCS / name).read_text(encoding="utf-8")

    def test_changed_pages_match_the_packaged_documentation(self) -> None:
        for name in (
            "ui.md",
            "assistant.md",
            "catalog.md",
            "concepts.md",
            "evidence.md",
            "getting-started.md",
            "how-it-works.md",
            "operations.md",
            "studio-workspaces.md",
        ):
            self.assertEqual(
                self._source(name),
                (_PACKAGED / name).read_text(encoding="utf-8"),
                name,
            )

    def test_ui_documents_the_complete_conversation_working_loop(self) -> None:
        ui = self._source("ui.md")
        for phrase in (
            "Conversation is the default surface",
            "three durable OptPilot",
            "Open work",
            "Source Viewer And Workspace Editor",
            "Study",
            "Ask from this page",
            "full main area",
            "Existing bookmarks",
            "Workspaces in this conversation",
            "no files are copied",
        ):
            self.assertIn(phrase, ui)

    def test_study_is_named_consistently_across_ui_and_contracts(self) -> None:
        combined = "\n".join(
            self._source(name)
            for name in ("ui.md", "assistant.md", "getting-started.md")
        )
        self.assertNotIn("Run setup", combined)
        self.assertIn("Study configuration", combined)
        self.assertIn("schema", combined)
        self.assertIn("API", combined)
        self.assertIn("Workspace", combined)

    def test_assistant_actions_are_explicit_and_cards_are_trusted(self) -> None:
        assistant = self._source("assistant.md")
        self.assertIn("exact object identities", assistant)
        self.assertIn("small allowlist", assistant)
        self.assertIn("Markdown is explanatory only", assistant)
        self.assertIn("approval-aware", assistant)
        self.assertIn("right-hand **Workspaces in this", assistant)
        self.assertIn("does not delete it", assistant)

    def test_user_facing_terms_match_the_visible_controls(self) -> None:
        ui = self._source("ui.md")
        workspaces = self._source("studio-workspaces.md")
        candidate_docs = "\n".join(
            (self._source("how-it-works.md"), self._source("evidence.md"))
        )
        concepts = self._source("concepts.md")

        self.assertIn("**Link local folder**", ui)
        self.assertIn("Link local folder", workspaces)
        self.assertNotIn("Open local folder", ui + workspaces)
        self.assertIn("| Configure Study |", ui)
        self.assertNotIn("| Use in Study |", ui)
        for visible_label in (
            "**Trials**",
            "**Trial attempts**",
            "**Trial results**",
            "**Saved files**",
            "**Event history**",
        ):
            self.assertIn(visible_label, ui)
        self.assertNotIn("Candidate **Inspect**", ui)
        self.assertNotIn("**Inspect** reads", candidate_docs)
        self.assertIn("| Code editor |", ui)
        self.assertIn("| Assistant |", ui)
        self.assertIn("| Shortlist |", concepts)
        self.assertNotIn("| Review Collection |", concepts)


if __name__ == "__main__":
    unittest.main()
