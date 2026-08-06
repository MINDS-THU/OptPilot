"""Browser-side trust contracts for structured Assistant cards.

The server already sanitizes ``ui_cards`` before persisting an Assistant event.
These tests keep the browser as an independent validation and dispatch boundary:
cards are presentation data attached to one typed event, never executable payloads
or content inferred from the Assistant transcript.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"
_APP = _STATIC / "app.js"
_STYLES = _STATIC / "styles.css"


def _function_source(source: str, name: str) -> str:
    match = re.search(
        rf"(?m)^(?:async\s+)?function\s+{re.escape(name)}\s*\(", source
    )
    if match is None:
        raise AssertionError(f"JavaScript function {name!r} was not found")
    successor = re.search(
        r"(?m)^(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(",
        source[match.end() :],
    )
    end = len(source) if successor is None else match.end() + successor.start()
    return source[match.start() : end]


class StudioAssistantCardsStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")
        cls.styles = _STYLES.read_text(encoding="utf-8")

    def test_card_pipeline_has_one_unambiguous_browser_definition(self) -> None:
        # Function declarations are hoisted. A second declaration silently
        # replaces the first and can change the validation contract used by
        # event collection, refresh, or click dispatch.
        for name in (
            "normalizeAssistantUiCard",
            "assistantUiCardsFromEvents",
            "assistantUiCardsHtml",
            "bindAssistantUiCards",
            "executeAssistantUiCardAction",
        ):
            declarations = re.findall(
                rf"(?m)^(?:async\s+)?function\s+{re.escape(name)}\s*\(",
                self.source,
            )
            self.assertEqual(
                len(declarations),
                1,
                f"{name} must have exactly one browser definition",
            )

    def test_cards_are_collected_only_from_typed_tool_result_payloads(self) -> None:
        collector = _function_source(self.source, "assistantUiCardsFromEvents")
        renderer = _function_source(self.source, "assistantUiCardsHtml")
        timeline = _function_source(self.source, "assistantInterleavedTimelineHtml")

        self.assertIn('"optpilot_tool_result"', collector)
        self.assertIn("payload.ui_cards", collector)
        self.assertIn("normalizeAssistantUiCard", collector)
        self.assertNotIn("result_preview", collector)
        self.assertNotIn("result_preview", renderer)
        self.assertNotIn("markdown", collector.lower())
        self.assertNotIn("markdown", renderer.lower())
        self.assertIn("assistantUiCardsHtml", timeline)

    def test_browser_revalidates_schema_kinds_coordinates_and_operations(self) -> None:
        normalizer = _function_source(self.source, "normalizeAssistantUiCard")

        self.assertIn('"optpilot.studio-ui-card.v1"', self.source)
        self.assertIn("ASSISTANT_UI_CARD_SCHEMA", normalizer)
        for card_kind in ("catalog-use", "run-setup", "run"):
            self.assertIn(f'"{card_kind}"', normalizer)
        for coordinate_kind in (
            "catalog-entry",
            "study-workspace",
            "workspace",
            "study-launch",
            "run",
        ):
            self.assertIn(f'"{coordinate_kind}"', normalizer)
        for coordinate_field in (
            "config_kind",
            "uid",
            "workspace_id",
            "workspace_revision",
            "study_relative_path",
            "launch_id",
            "run_id",
        ):
            self.assertIn(coordinate_field, normalizer)
        self.assertIn('startsWith("/")', normalizer)
        self.assertIn('part !== "."', normalizer)
        self.assertIn('part !== ".."', normalizer)
        for operation in (
            "configure-run",
            "open-catalog",
            "open-interface",
            "open-launch",
            "open-run",
            "open-workspace",
            "start-run",
        ):
            self.assertIn(f'"{operation}"', normalizer)

        # Cards carry opaque coordinates and operation names. They may not
        # introduce an arbitrary destination or a pre-authorized request body.
        for executable_field in ("request_body", "requestBody", ".url", "href"):
            self.assertNotIn(executable_field, normalizer)
        self.assertRegex(normalizer, r"allowed[A-Za-z]*Operations|operations[A-Za-z]*By")

        operation_map: dict[str, set[str]] = {}
        for coordinate_kind in (
            "catalog-entry",
            "study-workspace",
            "workspace",
            "study-launch",
            "run",
        ):
            match = re.search(
                rf'(?:^|[,{{])\s*(?:["\']{re.escape(coordinate_kind)}["\']|'
                rf'{re.escape(coordinate_kind)})\s*:\s*new Set\s*\(\s*\[([^\]]*)\]',
                normalizer,
            )
            self.assertIsNotNone(
                match,
                f"missing operation allowlist for {coordinate_kind}",
            )
            operation_map[coordinate_kind] = set(
                re.findall(r'["\']([^"\']+)["\']', match.group(1))
            )
        self.assertEqual(
            {key: value for key, value in operation_map.items() if key != "catalog-entry"},
            {
                "study-workspace": {
                    "configure-run",
                    "open-workspace",
                    "start-run",
                },
                "workspace": {"open-workspace", "start-run"},
                "study-launch": {"open-launch", "open-run"},
                "run": {"open-run"},
            },
        )
        catalog_operations = operation_map["catalog-entry"]
        if catalog_operations == {"open-catalog", "open-interface"}:
            # A study coordinate may add the two Run-setup operations after
            # the common Catalog allowlist is copied.
            self.assertRegex(
                normalizer,
                r'config_kind\s*===\s*["\']study["\'][\s\S]*?'
                r'(?:add\s*\(\s*["\']configure-run["\']|'
                r'new Set\s*\(\s*\[[^\]]*["\']configure-run["\'])',
            )
            self.assertRegex(
                normalizer,
                r'config_kind\s*===\s*["\']study["\'][\s\S]*?'
                r'(?:add\s*\(\s*["\']start-run["\']|'
                r'new Set\s*\(\s*\[[^\]]*["\']start-run["\'])',
            )
        else:
            # Equivalently, the initial coordinate allowlist may include the
            # study-only operations, provided both are removed for every
            # non-study Catalog coordinate.
            self.assertEqual(
                catalog_operations,
                {
                    "configure-run",
                    "open-catalog",
                    "open-interface",
                    "start-run",
                },
            )
            self.assertRegex(
                normalizer,
                r'config_kind\s*!==\s*["\']study["\'][\s\S]*?'
                r'delete\s*\(\s*["\']configure-run["\']\s*\)[\s\S]*?'
                r'delete\s*\(\s*["\']start-run["\']\s*\)',
            )

    def test_card_renderer_shows_substantive_fields_and_available_actions(self) -> None:
        renderer = _function_source(self.source, "assistantUiCardsHtml")

        for presentation_field in (
            "card.title",
            "card.description",
            "card.status",
            "card.facts",
            "card.actions",
        ):
            self.assertIn(presentation_field, renderer)
        self.assertIn("assistant-ui-card", renderer)
        self.assertIn("data-assistant-card-action", renderer)
        self.assertIn("escapeHtml", renderer)

        for style_hook in (
            ".assistant-ui-card",
            ".assistant-ui-card-facts",
            ".assistant-ui-card-actions",
        ):
            self.assertIn(style_hook, self.styles)

    def test_ineligible_actions_are_disabled_and_explain_why(self) -> None:
        renderer = _function_source(self.source, "assistantUiCardsHtml")
        current_state = _function_source(
            self.source, "assistantUiCardCurrentActionState"
        )

        self.assertRegex(
            renderer,
            r"assistantUiCardCurrentActionState\s*\(\s*card\s*,\s*action\s*\)",
        )
        self.assertRegex(renderer, r"currentAction\.eligible")
        self.assertIn("disabled", renderer)
        self.assertRegex(renderer, r"currentAction\.reason")
        self.assertRegex(renderer, r"assistant-ui-card-(?:action-)?reason")
        self.assertIn("aria-describedby", renderer)
        self.assertRegex(current_state, r"return\s+\{\s*eligible:\s*(?:true|false)")

    def test_action_binding_dispatches_only_from_an_explicit_click(self) -> None:
        binder = _function_source(self.source, "bindAssistantUiCards")
        executor = _function_source(self.source, "executeAssistantUiCardAction")

        self.assertIn("data-assistant-card-action", binder)
        self.assertIn('addEventListener("click"', binder)
        self.assertIn("executeAssistantUiCardAction", binder)
        self.assertNotIn("executeAssistantUiCardAction", _function_source(
            self.source, "assistantUiCardsHtml"
        ))
        self.assertRegex(
            executor,
            r"assistantUiCardCurrentActionState\s*\(\s*card\s*,\s*action\s*\)",
        )
        self.assertRegex(executor, r"currentAction\.eligible")

    def test_actions_rehydrate_only_exact_current_objects(self) -> None:
        plan = _function_source(self.source, "findAssistantUiCardPlan")
        executor = _function_source(self.source, "executeAssistantUiCardAction")
        component = _function_source(
            self.source, "resolveAssistantUiCardComponent"
        )

        for exact_field in (
            "workspace_id",
            "workspace_revision",
            "study_relative_path",
            "draft_id",
            "draft_revision",
            "environment_uid",
            "method_uid",
        ):
            self.assertIn(exact_field, plan)
        self.assertIn("workspaceSessionByBackendId", plan)
        self.assertNotIn('coordinate.kind === "workspace"', plan)
        self.assertIn("loadCatalogAndCompatibility", component)
        self.assertIn("componentInterfaceLaunchCapability", executor)
        self.assertIn("assistantUiCardPlanLaunchState", executor)
        self.assertIn("launchPlan(plan)", executor)

    def test_latest_card_projection_replaces_stale_status_across_turns(self) -> None:
        collector = _function_source(self.source, "assistantUiCardsFromEvents")
        latest = _function_source(
            self.source, "assistantUiCardLatestEventIndexes"
        )
        timeline = _function_source(
            self.source, "assistantInterleavedTimelineHtml"
        )

        self.assertIn("cardsById.delete(card.id)", collector)
        self.assertIn("cardsById.set(card.id, card)", collector)
        self.assertIn("latest.set(card.id", latest)
        self.assertIn("latestCardEventIndexes", timeline)
        self.assertIn("latestEventIndexes", timeline)

    def test_initial_render_cannot_overwrite_a_stored_draft(self) -> None:
        capture = _function_source(self.source, "captureAssistantContinuity")

        self.assertIn(
            "state.renderedAssistantSessionId !== sessionId",
            capture,
        )
        self.assertRegex(
            capture,
            r"renderedAssistantSessionId\s*!==\s*sessionId\)\s*return",
        )

    def test_start_run_stays_explicit_and_approval_aware(self) -> None:
        normalizer = _function_source(self.source, "normalizeAssistantUiCard")
        renderer = _function_source(self.source, "assistantUiCardsHtml")
        executor = _function_source(self.source, "executeAssistantUiCardAction")

        self.assertIn('"start-run"', normalizer)
        self.assertIn("approval_required", normalizer)
        self.assertIn("approval_required", renderer)
        self.assertIn('"start-run"', executor)
        self.assertRegex(
            executor,
            r"(?:operation\s*===\s*[\"']start-run[\"']|case\s+[\"']start-run[\"'])",
        )
        # The structured card must not inherit the Assistant's global
        # auto-approval preference; a run starts only through its card action.
        self.assertNotIn("assistantPermissionStudyLaunch", executor)


if __name__ == "__main__":
    unittest.main()
