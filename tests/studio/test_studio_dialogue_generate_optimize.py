"""The generate-then-optimize story runs end to end in the conversation.

Two links were browser-only. The step that turns a generated simulator bundle
into a policy-search-ready environment lived solely behind the "Set up for
Catalog" button -- its one call site was that page's route, so the Assistant
could carry a person to either side of registration but not through it. And
drafting a new Run setup demanded exact pinned references, the byte-perfect
echo a language model reliably fumbles.

Now the same wizard is a tool, gated like registration, and the Study Builder
accepts a human-readable name -- resolved with a preference for the REGISTERED
copy, because the same id often exists both as a registered entry and as its
filesystem source, and only the registered one is a valid pinning target.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import optpilot_studio.ui.server as server
from optpilot_studio.agent import OPTPILOT_AGENT_TOOLS, OPTPILOT_AGENT_TOOL_SPECS
from optpilot_studio.ui.server import (
    CatalogEntryRef,
    UiState,
    _approve_agent_action,
    _create_agent_session,
    _execute_agent_tool,
    _read_agent_approvals,
    _realm_catalog_ref_for_readable_id,
)


def _ref(source_kind: str, entry_id: str, kind: str = "environment") -> CatalogEntryRef:
    return CatalogEntryRef(
        source_kind=source_kind,
        source_id="import_" + "a" * 40 if source_kind != "realm-catalog" else "pkg",
        source_revision=None if source_kind != "realm-catalog" else 3,
        source_digest=None if source_kind != "realm-catalog" else "b" * 64,
        kind=kind,
        entry_id=entry_id,
        focus_path=f"environments/{entry_id}/environment.yaml",
        # left empty so the ref computes its own digest, as real ones do
        ref_digest="",
    )


def _state(tmp: Path) -> UiState:
    state = UiState(cwd=tmp, catalog_roots=[], run_roots=[])
    for name in (
        "sessions_dir", "agent_sessions_dir", "jobs_dir",
        "workspaces_dir", "runtime_dir",
    ):
        setattr(state, name, tmp / name)
        getattr(state, name).mkdir(parents=True, exist_ok=True)
    state.settings_path = tmp / "settings.json"
    return state


class CatalogSetupToolTest(unittest.TestCase):
    def test_the_tool_is_advertised(self) -> None:
        self.assertIn("optpilot_catalog_setup", OPTPILOT_AGENT_TOOLS)
        spec = next(
            s for s in OPTPILOT_AGENT_TOOL_SPECS
            if s["name"] == "optpilot_catalog_setup"
        )
        self.assertEqual(
            spec["parameters"]["required"], ["workspace_id", "role"]
        )

    def test_it_asks_before_writing_anything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            session = _create_agent_session(state, {"title": "setup"})
            with mock.patch.object(
                server, "_configure_workspace_catalog_role"
            ) as wizard:
                result = _execute_agent_tool(
                    state,
                    session["id"],
                    "optpilot_catalog_setup",
                    {"workspace_id": "ws_x", "role": "environment"},
                )
            wizard.assert_not_called()
        self.assertTrue(result["data"].get("approval_required"))
        self.assertIn("nothing is registered yet", result["summary"].lower())

    def test_approval_runs_the_same_wizard_as_the_button(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            session = _create_agent_session(state, {"title": "setup"})
            with mock.patch.object(
                server,
                "_configure_workspace_catalog_role",
                return_value={
                    "workspace": {},
                    "configuration": {
                        "role": "environment",
                        "id": "restaurant",
                        "created_paths": ["environment.yaml", "policy.py"],
                        "needs_editing": False,
                        "next_action": "check",
                        "detected_simulation": {"schema_version": "devs.simulation.v2"},
                    },
                },
            ) as wizard:
                _execute_agent_tool(
                    state,
                    session["id"],
                    "optpilot_catalog_setup",
                    {
                        "workspace_id": "ws_x",
                        "role": "environment",
                        "id": "restaurant",
                        "description": "A restaurant",
                    },
                )
                approvals = _read_agent_approvals(state, session["id"])
                self.assertEqual(len(approvals), 1)
                approved = _approve_agent_action(
                    state, session["id"], approvals[0]["id"]
                )
            wizard.assert_called_once_with(
                state,
                "ws_x",
                {"role": "environment", "id": "restaurant", "description": "A restaurant"},
            )
        result = approved["result"]
        self.assertTrue(result["ok"], result)
        self.assertIn("policy hook was detected", result["summary"])
        self.assertIn("prepare and validate", result["summary"])


class ReadableStudyBuilderNamesTest(unittest.TestCase):
    def _index(self, entries):
        return {
            "environments": [
                {
                    "id": ref.entry_id,
                    "qualified_id": f"pkg/environment/{ref.entry_id}",
                    "catalog_key": f"pkg/environment/{ref.entry_id}",
                    "uid": ref.token(),
                }
                for ref in entries
            ],
            "methods": [],
        }

    def test_the_registered_copy_wins_over_its_filesystem_source(self) -> None:
        realm = _ref("realm-catalog", "dispatch-station")
        filesystem = _ref("configured-filesystem-import", "dispatch-station")
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            with mock.patch.object(
                server,
                "_catalog_index_payload",
                return_value=self._index([filesystem, realm]),
            ):
                resolved = _realm_catalog_ref_for_readable_id(
                    state, "environment", "dispatch-station"
                )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.source_kind, "realm-catalog")

    def test_a_filesystem_only_match_is_returned_for_the_import_first_refusal(
        self,
    ) -> None:
        filesystem = _ref("configured-filesystem-import", "dispatch-station")
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            with mock.patch.object(
                server,
                "_catalog_index_payload",
                return_value=self._index([filesystem]),
            ):
                resolved = _realm_catalog_ref_for_readable_id(
                    state, "environment", "dispatch-station"
                )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.source_kind, "configured-filesystem-import")

    def test_an_unknown_name_resolves_to_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            with mock.patch.object(
                server, "_catalog_index_payload",
                return_value={"environments": [], "methods": []},
            ):
                self.assertIsNone(
                    _realm_catalog_ref_for_readable_id(
                        state, "environment", "no-such-thing"
                    )
                )

    def test_a_token_is_left_for_the_exact_resolver(self) -> None:
        realm = _ref("realm-catalog", "dispatch-station")
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            self.assertIsNone(
                _realm_catalog_ref_for_readable_id(
                    state, "environment", realm.token()
                )
            )

    def test_a_typo_in_the_draft_flow_reads_as_a_typo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            with mock.patch.object(
                server, "_catalog_index_payload",
                return_value={"environments": [], "methods": []},
            ):
                with self.assertRaisesRegex(
                    ValueError, "does not name any catalog entry"
                ):
                    server._study_builder_sources(
                        state,
                        {"environment_ref": "dispach-station", "method_ref": "x"},
                    )


if __name__ == "__main__":
    unittest.main()
