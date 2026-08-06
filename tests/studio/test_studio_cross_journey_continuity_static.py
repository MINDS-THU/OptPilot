"""Focused contracts for cross-journey Studio continuity and recovery.

These checks intentionally cover the seams between Conversations, Workspaces,
interfaces, and Open work.  The individual screens can look correct
while these hand-offs still lose work or imply the wrong lifecycle.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"
_APP = _STATIC / "app.js"


def _function_source(source: str, name: str) -> str:
    """Return one top-level JavaScript function without naming its successor."""

    match = re.search(
        rf"(?m)^(?:async\s+)?function\s+{re.escape(name)}\s*\(",
        source,
    )
    if match is None:
        raise AssertionError(f"JavaScript function {name!r} was not found")
    successor = re.search(
        r"(?m)^(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(",
        source[match.end() :],
    )
    end = len(source) if successor is None else match.end() + successor.start()
    return source[match.start() : end]


class StudioCrossJourneyContinuityStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_first_message_creates_a_durable_conversation_before_local_mutation(
        self,
    ) -> None:
        send = _function_source(self.source, "sendAgentMessage")
        create = _function_source(self.source, "createAgentSessionForSurface")

        creation = send.index(
            "await createAgentSessionForSurface({ navigate: false })"
        )
        local_message = send.index(
            "pushAssistantMessage(userMessage, { persist: false })"
        )
        clear_composer = send.index('els.agentInput.value = ""')
        self.assertLess(creation, local_message)
        self.assertLess(creation, clear_composer)
        self.assertIn("if (!session)", send[creation:local_message])
        self.assertIn("els.agentInput.value = message", send[creation:local_message])
        self.assertIn('postJson("/api/agent-sessions"', create)
        self.assertIn("return null", create)
        self.assertNotIn("`agent-session-${Date.now()", create)
        self.assertNotIn("state.agentSessions = [session", create)

    def test_conversation_workspace_creation_returns_to_the_conversation(self) -> None:
        create = _function_source(self.source, "createBlankSession")

        attach = create.index(
            "await attachWorkspaceToAgentSession(session.id, originatingConversationId)"
        )
        conversation = create.index(
            'openConversationSurface({ history: "replace" })',
            attach,
        )
        workspace = create.index('setView("workspace")', conversation)
        self.assertLess(attach, conversation)
        self.assertLess(conversation, workspace)
        self.assertIn(
            "if (attachToConversation && conversationShellEnabled())",
            create[attach:workspace],
        )
        self.assertIn("renderAssistant()", create[conversation:workspace])

    def test_conversation_local_folder_connection_returns_to_the_conversation(
        self,
    ) -> None:
        connect = _function_source(self.source, "connectLocalFolder")

        attach = connect.index(
            "await attachWorkspaceToAgentSession(session.id, originatingConversationId)"
        )
        conversation = connect.index(
            'openConversationSurface({ history: "replace" })',
            attach,
        )
        workspace = connect.index('setView("workspace")', conversation)
        self.assertLess(attach, conversation)
        self.assertLess(conversation, workspace)
        self.assertIn(
            "if (attachToConversation && conversationShellEnabled())",
            connect[attach:workspace],
        )
        self.assertIn("renderAssistant()", connect[conversation:workspace])

    def test_workspace_attachment_failure_is_not_optimistically_hidden(self) -> None:
        attach = _function_source(self.source, "attachWorkspaceToAgentSession")
        panel = _function_source(self.source, "renderConversationWorkspaceAccess")

        request = attach.index("/attach-workspace")
        local_mutation = attach.index(
            "state.agentWorkspaceAttachments[agentSession.id] = attached"
        )
        self.assertLess(request, local_mutation)
        self.assertIn("state.conversationWorkspaceError", attach)
        self.assertIn("throw error", attach)
        self.assertNotIn("Keep the optimistic attachment", attach)
        self.assertIn("state.conversationWorkspaceError", panel)
        self.assertIn('role="alert"', panel)

    def test_only_make_default_changes_the_conversation_default_and_rolls_back_visibly(self) -> None:
        select = _function_source(self.source, "selectSession")
        local = _function_source(self.source, "setSelectedWorkspace")
        sync = _function_source(self.source, "syncSelectedWorkspaceToBackend")
        actions = _function_source(self.source, "handleConversationWorkspaceAction")
        panel = _function_source(self.source, "renderConversationWorkspaceAccess")

        self.assertNotIn("/select-workspace", select)
        self.assertNotIn("syncSelectedWorkspaceToBackend", select)
        self.assertNotIn("syncSelectedWorkspaceToBackend", local)
        self.assertEqual(sync.count("/select-workspace"), 1)
        self.assertIn("await updateAgentSessionFromPayload(payload.session)", sync)
        self.assertIn("state.selectedWorkspaceByAgentSession[agentSession.id] = previousWorkspaceId", sync)
        self.assertIn("state.conversationWorkspaceError = boundedPublicActionError", sync)
        self.assertIn('title: "Default Workspace was not changed"', sync)
        self.assertIn('action === "current"', actions)
        self.assertIn("await syncSelectedWorkspaceToBackend(workspaceId", actions)
        self.assertIn("conversationWorkspaceList.innerHTML", panel)
        self.assertIn('role="alert"', panel)

    def test_full_stage_interface_outputs_drawer_is_wired_to_live_outputs(self) -> None:
        cache = _function_source(self.source, "cacheElements")
        events = _function_source(self.source, "bindEvents")
        render_session = _function_source(self.source, "renderInterfaceSession")
        render_outputs = _function_source(
            self.source,
            "renderInterfaceSessionOutputs",
        )
        roots = _function_source(self.source, "interfaceOutputControlRoots")

        for element_id in (
            "interfaceSessionOutputsButton",
            "interfaceSessionOutputsCount",
            "interfaceSessionOutputsScrim",
            "interfaceSessionOutputsDrawer",
            "interfaceSessionOutputsCloseButton",
            "interfaceSessionOutputsBody",
            "interfaceSessionOutputsEmpty",
        ):
            self.assertIn(f'"{element_id}"', cache)
        self.assertIn("interfaceSessionOutputsButton", events)
        self.assertIn("interfaceSessionOutputsCloseButton", events)
        self.assertIn("interfaceSessionOutputsScrim", events)
        self.assertIn("setInterfaceSessionOutputsOpen", events)
        self.assertIn("renderInterfaceSessionOutputs(model)", render_session)
        self.assertIn("interfaceSessionOutputsCount", render_outputs)
        self.assertIn("interfaceSessionOutputsBody", render_outputs)
        self.assertIn("renderInterfaceOutputList", render_outputs)
        self.assertIn("bindInterfaceOutputControls", render_outputs)
        self.assertIn("els.interfaceSessionOutputsBody", roots)

    def test_saved_interface_output_uses_explicit_workspace_language(self) -> None:
        card = _function_source(self.source, "renderInterfaceOutputCard")

        self.assertIn('"Saved as Workspace"', card)
        self.assertIn("Saved as Workspace:", card)
        self.assertIn("Open Workspace", card)
        self.assertIn("Set up for Catalog", card)
        self.assertNotIn("Workspace created:", card)

    def test_needs_attention_is_not_counted_as_active_work(self) -> None:
        render = _function_source(self.source, "renderOpenWork")

        self.assertRegex(
            render,
            r"const\s+activeCount\s*=\s*items\.filter\(\(item\)\s*=>\s*item\.active\)\.length",
        )
        self.assertNotRegex(
            render,
            r"activeCount[^;]*Needs attention",
        )
        self.assertIn("attentionCount", render)
        self.assertIn("Needs attention", render)


if __name__ == "__main__":
    unittest.main()
