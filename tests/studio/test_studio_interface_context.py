"""Interface identity in Ask-from-this-page context (U5)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    UiState,
    _agent_context_packet,
    _assistant_selected_interface,
    _create_agent_session,
)


_APP_JS = (
    Path(__file__).resolve().parents[2]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)


def _launch_context(**overrides) -> dict:
    base = {
        "launch_id": "launch-exact-1",
        "status": "ready",
        "launch_scope": "catalog",
        "profile_id": "console",
        "label": "COOPA Solve Console",
        "source": {"kind": "method", "uid": "cref_method_exact", "key": "method:cref_method_exact"},
    }
    base.update(overrides)
    return base


class AssistantSelectedInterfaceBoundsTest(unittest.TestCase):
    def test_full_stage_selection_keeps_exact_coordinates_only(self) -> None:
        bounded = _assistant_selected_interface(
            _launch_context(
                preview_url="http://127.0.0.1:9999/secret",
                token="presentation-token",
            ),
            current_page="interface",
        )
        self.assertEqual(
            bounded,
            {
                "launch_id": "launch-exact-1",
                "status": "ready",
                "launch_scope": "catalog",
                "profile_id": "console",
                "label": "COOPA Solve Console",
                "source": {
                    "kind": "method",
                    "uid": "cref_method_exact",
                    "key": "method:cref_method_exact",
                },
            },
        )

    def test_workspace_transient_source_is_bounded_too(self) -> None:
        bounded = _assistant_selected_interface(
            _launch_context(
                launch_scope="workspace-transient",
                source={
                    "workspace_id": "workspace-1",
                    "workspace_title": "Draft",
                    "host_path": "/Users/someone/project",
                },
            ),
            current_page="editor",
        )
        assert bounded is not None
        self.assertEqual(
            bounded["source"],
            {"workspace_id": "workspace-1", "workspace_title": "Draft"},
        )

    def test_selection_requires_interface_page_and_launch_id(self) -> None:
        self.assertIsNone(
            _assistant_selected_interface(
                _launch_context(), current_page="catalog"
            )
        )
        self.assertIsNone(
            _assistant_selected_interface(
                _launch_context(launch_id=""), current_page="interface"
            )
        )
        self.assertIsNone(
            _assistant_selected_interface("not-a-dict", current_page="interface")
        )

    def test_values_are_truncated_to_a_bounded_length(self) -> None:
        bounded = _assistant_selected_interface(
            _launch_context(label="x" * 5000), current_page="interface"
        )
        assert bounded is not None
        self.assertEqual(len(bounded["label"]), 256)


class ContextPacketInterfaceTest(unittest.TestCase):
    def test_packet_carries_selected_interface_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Interface context"})
            context = _agent_context_packet(
                state,
                session,
                {
                    "current_page": "interface",
                    "selected_interface": _launch_context(),
                },
            )

        self.assertEqual(context["current_page"], "interface")
        self.assertEqual(
            context["selected_interface"]["launch_id"], "launch-exact-1"
        )
        self.assertEqual(
            context["selected_interface"]["source"]["uid"], "cref_method_exact"
        )
        self.assertNotIn("selected_interface", context["visible_state"])

    def test_packet_omits_interface_selection_off_the_interface_surfaces(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Catalog page"})
            context = _agent_context_packet(
                state,
                session,
                {
                    "current_page": "catalog",
                    "selected_interface": _launch_context(),
                },
            )

        self.assertIsNone(context["selected_interface"])


class ClientInterfaceContextSourceTest(unittest.TestCase):
    def test_visible_context_includes_bounded_interface_identity(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        start = source.index("function assistantVisibleContext()")
        end = source.index("registration_menu:", start)
        body = source[start:end]

        self.assertIn(
            "selected_interface: isViewingActiveInterface() && state.interfaceLaunch",
            body,
        )
        self.assertIn("launch_id: String(state.interfaceLaunch.launch_id", body)
        self.assertIn('launch_scope === "workspace-transient"', body)
        # Identity only: never preview URLs or presentation tokens.
        self.assertNotIn("preview", body[body.index("selected_interface:"):])
        self.assertNotIn("token", body[body.index("selected_interface:"):])


if __name__ == "__main__":
    unittest.main()
