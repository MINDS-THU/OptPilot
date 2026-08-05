from __future__ import annotations

import unittest
from unittest.mock import patch

from optpilot_studio.agent import (
    OpenHandsAdapter,
    OpenHandsRuntimeConfig,
    STUDIO_UI_CARD_MAX_COUNT,
    STUDIO_UI_CARD_SCHEMA,
    sanitize_studio_ui_cards,
)
from optpilot_studio.ui.server import _sanitize_agent_event, _tool_result


def _run_card(*, title: str = "Recorded run") -> dict:
    return {
        "schema": STUDIO_UI_CARD_SCHEMA,
        "id": "run_card_1",
        "kind": "run",
        "coordinate": {"kind": "run", "run_id": "run-123"},
        "title": title,
        "description": "Current retained result.",
        "status": "completed",
        "facts": [{"label": "Completed trials", "value": 12}],
        "actions": [
            {
                "id": "run_card_1:open-run",
                "label": "Open run",
                "operation": "open-run",
                "eligible": True,
                "approval_required": False,
            }
        ],
    }


class StudioAssistantCardContractTests(unittest.TestCase):
    def test_card_sanitizer_keeps_only_bounded_allowlisted_presentation_data(self) -> None:
        raw = _run_card()
        raw["url"] = "https://attacker.invalid/run"
        raw["coordinate"]["request_body"] = {"approved": True}
        raw["facts"].extend(
            [
                {"label": "Nested", "value": {"secret": "not presentation"}},
                {"label": "Too long", "value": "x" * 1001},
            ]
        )
        raw["actions"].extend(
            [
                {
                    "id": "run_card_1:delete",
                    "label": "Delete everything",
                    "operation": "delete-workspace",
                    "eligible": True,
                    "approval_required": False,
                    "url": "https://attacker.invalid/delete",
                },
                {
                    "id": "run_card_1:open-workspace",
                    "label": "Open Workspace",
                    "operation": "open-workspace",
                    "eligible": False,
                    "reason": "No Workspace is associated with this Run.",
                    "approval_required": False,
                    "request": {"workspace_id": "forged"},
                },
            ]
        )

        cards = sanitize_studio_ui_cards([raw])

        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(
            set(card),
            {
                "schema",
                "id",
                "kind",
                "coordinate",
                "title",
                "description",
                "status",
                "facts",
                "actions",
            },
        )
        self.assertEqual(card["coordinate"], {"kind": "run", "run_id": "run-123"})
        self.assertNotIn("attacker.invalid", str(card))
        self.assertEqual(card["facts"], [{"label": "Completed trials", "value": 12}])
        self.assertEqual(
            [action["operation"] for action in card["actions"]],
            ["open-run"],
        )

        many = sanitize_studio_ui_cards(
            [
                {
                    **_run_card(title=f"Run {index}"),
                    "id": f"run_card_{index}",
                    "coordinate": {"kind": "run", "run_id": f"run-{index}"},
                }
                for index in range(STUDIO_UI_CARD_MAX_COUNT + 5)
            ]
        )
        self.assertEqual(len(many), STUDIO_UI_CARD_MAX_COUNT)

    def test_card_sanitizer_rejects_semantically_mismatched_catalog_kinds(self) -> None:
        catalog_use_for_study = {
            **_run_card(),
            "id": "bad_catalog_use",
            "kind": "catalog-use",
            "coordinate": {
                "kind": "catalog-entry",
                "config_kind": "study",
                "uid": "cref_study_exact",
            },
            "actions": [],
        }
        run_setup_for_environment = {
            **_run_card(),
            "id": "bad_run_setup",
            "kind": "run-setup",
            "coordinate": {
                "kind": "catalog-entry",
                "config_kind": "environment",
                "uid": "cref_environment_exact",
            },
            "actions": [],
        }

        self.assertEqual(
            sanitize_studio_ui_cards(
                [catalog_use_for_study, run_setup_for_environment]
            ),
            [],
        )

    def test_tool_result_projects_a_substantive_exact_run_setup_card(self) -> None:
        result = _tool_result(
            "optpilot_study_draft",
            True,
            "Study draft prepared.",
            data={
                "draft_id": "draft-1",
                "draft_revision": 2,
                "workspace_id": "workspace-1",
                "workspace_revision": 7,
                "study_relative_path": "studies/plan.yaml",
                "draft": {
                    "name": "Production scheduling",
                    "description": "Improve throughput while limiting delay.",
                    "objective": {"metric": "throughput", "direction": "maximize"},
                    "budget": {"maxTrials": 24},
                },
                "compatibility": {
                    "compatible": True,
                    "environment": {
                        "uid": "cref_environment_exact",
                        "label": "Factory simulator",
                    },
                    "method": {
                        "uid": "cref_method_exact",
                        "label": "Bayesian optimization",
                    },
                },
                "validation": {
                    "valid": True,
                    "launch": {"eligible": True, "code": "ready"},
                },
            },
        )

        self.assertEqual(len(result["ui_cards"]), 1)
        card = result["ui_cards"][0]
        self.assertEqual(card["kind"], "run-setup")
        self.assertEqual(
            card["coordinate"],
            {
                "kind": "study-workspace",
                "workspace_id": "workspace-1",
                "workspace_revision": 7,
                "study_relative_path": "studies/plan.yaml",
                "environment_uid": "cref_environment_exact",
                "method_uid": "cref_method_exact",
                "draft_id": "draft-1",
                "draft_revision": 2,
            },
        )
        self.assertEqual(
            {fact["label"]: fact["value"] for fact in card["facts"]},
            {
                "Environment": "Factory simulator",
                "Method": "Bayesian optimization",
                "Metric": "throughput",
                "Direction": "maximize",
                "Max trials": 24,
                "Compatibility": "Compatible",
                "Validation": "Valid",
                "Workspace revision": 7,
            },
        )
        actions = {item["operation"]: item for item in card["actions"]}
        self.assertEqual(
            set(actions), {"configure-run", "open-workspace", "start-run"}
        )
        self.assertTrue(actions["start-run"]["eligible"])
        self.assertTrue(actions["start-run"]["approval_required"])

    def test_tool_result_projects_catalog_and_run_coordinates_without_urls(self) -> None:
        catalog = _tool_result(
            "optpilot_catalog_detail",
            True,
            "Loaded environment catalog entry.",
            data={
                "entry": {
                    "uid": "cref_exact_environment",
                    "config": "environment",
                    "label": "Factory simulator",
                    "package_id": "factory",
                    "description": "A bounded simulator.",
                },
                "validation": {"valid": True},
            },
        )["ui_cards"][0]
        run = _tool_result(
            "optpilot_run_detail",
            True,
            "Run detail loaded.",
            data={
                "run": {
                    "run_id": "run-exact-1",
                    "name": "Factory improvement",
                    "status": "running",
                    "completed_trials": 4,
                    "accepted_trials": 5,
                    "failure_count": 0,
                    "objective": {"metric": "throughput"},
                }
            },
        )["ui_cards"][0]

        self.assertEqual(
            catalog["coordinate"],
            {
                "kind": "catalog-entry",
                "config_kind": "environment",
                "uid": "cref_exact_environment",
            },
        )
        self.assertEqual(run["coordinate"], {"kind": "run", "run_id": "run-exact-1"})
        self.assertNotIn("url", str(catalog).lower())
        self.assertNotIn("url", str(run).lower())

    def test_broad_catalog_list_is_search_evidence_not_a_wall_of_cards(self) -> None:
        environments = [
            {
                "uid": f"cref_environment_{index}",
                "config": "environment",
                "label": f"Environment {index}",
            }
            for index in range(STUDIO_UI_CARD_MAX_COUNT + 4)
        ]
        result = _tool_result(
            "optpilot_catalog_list",
            True,
            "Loaded Catalog entries.",
            data={
                "environments": environments,
                "methods": [
                    {
                        "uid": "cref_method_exact",
                        "config": "method",
                        "label": "Exact method",
                    }
                ],
                "resources": [
                    {
                        "uid": "cref_resource_exact",
                        "config": "resource",
                        "label": "Exact resource",
                    }
                ],
                "studies": [],
            },
        )

        self.assertEqual(result["ui_cards"], [])

    def test_invalid_but_created_run_setup_remains_recoverable(self) -> None:
        result = _tool_result(
            "optpilot_study_draft",
            False,
            "Study draft prepared with validation errors.",
            data={
                "draft_id": "draft-review",
                "workspace_id": "workspace-review",
                "workspace_revision": 3,
                "study_relative_path": "studies/review.yaml",
                "draft": {
                    "name": "Setup needing review",
                    "objective": {"metric": "cost", "direction": "minimize"},
                    "budget": {"maxTrials": 10},
                },
                "validation": {
                    "valid": False,
                    "errors": ["Objective metric is unavailable."],
                },
            },
        )

        card = result["ui_cards"][0]
        self.assertEqual(card["status"], "needs-review")
        self.assertEqual(card["coordinate"]["draft_id"], "draft-review")
        start = next(
            action for action in card["actions"] if action["operation"] == "start-run"
        )
        self.assertFalse(start["eligible"])
        self.assertEqual(start["reason"], "Objective metric is unavailable.")

    def test_successful_tool_event_carries_redacted_cards_independent_of_preview(self) -> None:
        secret = "sk-card-secret"
        adapter = OpenHandsAdapter(
            OpenHandsRuntimeConfig(api_key=secret, enabled=True)
        )
        result = {
            "ok": True,
            "tool": "optpilot_run_detail",
            "summary": "Loaded run.",
            "data": {"large_preview_prefix": "x" * 5000},
            "ui_cards": [_run_card(title=f"Run using {secret}")],
        }
        events = [
            {
                "kind": "ActionEvent",
                "tool_name": "optpilot_run_detail",
                "tool_call_id": "call-run-1",
                "action": {"kind": "optpilot_run_detail", "run_id": "run-123"},
            }
        ]

        with patch.object(adapter, "_send_tool_result_message", return_value=None):
            tool_events, approval_id = adapter._execute_openhands_client_tools(
                events,
                "http://127.0.0.1/api/conversations",
                "conversation-1",
                lambda _name, _arguments: result,
                set(),
            )

        self.assertEqual(approval_id, "")
        self.assertEqual(len(tool_events), 1)
        payload = tool_events[0]["payload"]
        self.assertNotIn("Run using", payload["result_preview"])
        self.assertEqual(payload["ui_cards"][0]["title"], "Run using [redacted]")
        self.assertNotIn(secret, str(payload))
        self.assertEqual(payload["delivery_status"], "sent")

    def test_unsuccessful_or_tampered_events_cannot_publish_executable_actions(self) -> None:
        adapter = OpenHandsAdapter(OpenHandsRuntimeConfig())
        failed = {
            "ok": False,
            "tool": "optpilot_run_detail",
            "summary": "Run missing.",
            "ui_cards": [_run_card()],
        }
        events = [
            {
                "kind": "ActionEvent",
                "tool_name": "optpilot_run_detail",
                "tool_call_id": "call-run-failed",
                "action": {"kind": "optpilot_run_detail", "run_id": "run-123"},
            }
        ]
        with patch.object(adapter, "_send_tool_result_message", return_value=None):
            tool_events, _approval_id = adapter._execute_openhands_client_tools(
                events,
                "http://127.0.0.1/api/conversations",
                "conversation-1",
                lambda _name, _arguments: failed,
                set(),
            )
        failed_card = tool_events[0]["payload"]["ui_cards"][0]
        self.assertEqual(failed_card["coordinate"], {"kind": "run", "run_id": "run-123"})
        self.assertEqual(
            [action["operation"] for action in failed_card["actions"]],
            ["open-run"],
        )

        tampered = _run_card()
        tampered["url"] = "https://attacker.invalid"
        tampered["actions"].append(
            {
                "id": "run_card_1:exec",
                "label": "Execute",
                "operation": "shell-run",
                "eligible": True,
                "approval_required": False,
            }
        )
        public = _sanitize_agent_event(
            {
                "type": "optpilot_tool_result",
                "payload": {"tool": "optpilot_run_detail", "ui_cards": [tampered]},
            }
        )
        card = public["payload"]["ui_cards"][0]
        self.assertNotIn("url", card)
        self.assertEqual(
            [action["operation"] for action in card["actions"]], ["open-run"]
        )


if __name__ == "__main__":
    unittest.main()
