"""Focused contracts for the essential Study configuration experience."""

from __future__ import annotations

import unittest
from pathlib import Path


_APP_JS = (
    Path(__file__).resolve().parents[1]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)
_STYLES_CSS = _APP_JS.with_name("styles.css")


def _function_source(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


def _async_function_source(source: str, name: str, next_name: str) -> str:
    start = source.index(f"async function {name}(")
    end = source.index(f"async function {next_name}(", start)
    return source[start:end]


class StudioStudyEditorStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP_JS.read_text(encoding="utf-8")
        cls.styles = _STYLES_CSS.read_text(encoding="utf-8")

    def test_required_study_choices_are_visible_before_one_advanced_section(self) -> None:
        editor = _function_source(
            self.source,
            "studyConfigEditor",
            "studyAdvancedGroup",
        )
        advanced_at = editor.index('studyConfigSection("Advanced settings"')
        primary = editor[:advanced_at]
        advanced = editor[advanced_at:]

        self.assertIn("study-primary-settings", primary)
        self.assertIn("Study configuration", primary)
        for label, field in (
            ("Environment", "environmentUid"),
            ("Method", "methodUid"),
            ("Metric", "metric"),
            ("Direction", "direction"),
            ("Max trials", "maxTrials"),
        ):
            self.assertIn(f'"{label}", "{field}"', primary)
            self.assertNotIn(f'"{label}", "{field}"', advanced)

        for label, field in (
            ("Aggregation", "aggregation"),
            ("Secondary metrics", "secondaryMetrics"),
            ("Timeout seconds", "timeoutSeconds"),
            ("Parallelism", "parallelism"),
            ("Max failures", "maxFailures"),
            ("Max retries", "maxRetries"),
            ("Max wall-clock seconds", "maxWallClockSeconds"),
            ("Evidence level", "evidenceLevel"),
            ("Seed", "seed"),
            ("Tags", "tags"),
            ("Description", "description"),
        ):
            self.assertIn(f'"{label}", "{field}"', advanced)

        self.assertEqual(editor.count('studyConfigSection("Advanced settings"'), 1)
        self.assertNotIn('studyConfigSection("Binding"', editor)
        self.assertNotIn('studyConfigSection("Objective"', editor)
        self.assertNotIn('studyConfigSection("Run Policy"', editor)

    def test_advanced_fields_keep_the_existing_study_payload_semantics(self) -> None:
        payload = _function_source(self.source, "planPayload", "exactCatalogEntryRef")
        for field in (
            "environment_ref",
            "method_ref",
            "name",
            "description",
            "tags",
            "metric",
            "direction",
            "aggregation",
            "secondaryMetrics",
            "maxTrials",
            "maxWallClockSeconds",
            "maxFailures",
            "parallelism",
            "timeoutSeconds",
            "maxRetries",
            "evidenceLevel",
            "seed",
        ):
            self.assertIn(f"{field}:", payload)

    def test_study_actions_stay_explicit_and_explain_disabled_launches(self) -> None:
        detail = _function_source(
            self.source,
            "renderPlanDetail",
            "studyConfigEditor",
        )

        self.assertIn('"Launch Run"', detail)
        self.assertIn('savedDraft ? "Update draft" : "Save draft"', detail)
        self.assertIn('class="ghost-button plan-draft"', detail)
        self.assertIn('class="primary-button plan-launch"', detail)
        self.assertIn("studyLaunchBindingReason(plan)", detail)
        self.assertIn("Launch unavailable:", detail)
        self.assertIn('role="status"', detail)

    def test_unavailable_saved_draft_has_a_clear_recovery_path(self) -> None:
        detail = _function_source(
            self.source,
            "renderPlanDetail",
            "studyConfigEditor",
        )

        self.assertIn("study-browse-current-catalog", detail)
        self.assertIn("Browse current Catalog", detail)
        self.assertIn('openContentSurface("catalog", { history: "push" })', detail)

    def test_incomplete_component_pair_stays_repairable(self) -> None:
        detail = _function_source(
            self.source,
            "renderPlanDetail",
            "studyConfigEditor",
        )
        editor = _function_source(
            self.source,
            "studyConfigEditor",
            "studyAdvancedGroup",
        )
        choices = _function_source(
            self.source,
            "catalogSelectField",
            "catalogChoices",
        )
        readiness = _function_source(
            self.source,
            "studyComponentPairReady",
            "catalogChoicesForPlan",
        )
        presentation = _function_source(
            self.source,
            "studyLaunchPresentation",
            "labeledStatusPill",
        )

        self.assertIn("const componentPairReady = studyComponentPairReady(plan);", detail)
        self.assertIn("const locked = draftUnavailable;", detail)
        self.assertIn("const saveDisabled = !componentPairReady", detail)
        self.assertIn("const launchEnabled = componentPairReady", detail)
        self.assertIn("studyConfigEditor(plan, locked)", detail)
        self.assertIn(
            'catalogSelectField("Environment", "environmentUid"',
            editor,
        )
        self.assertIn(
            'catalogSelectField("Method", "methodUid"',
            editor,
        )
        self.assertIn("locked || !(state.catalog.environments", editor)
        self.assertIn("locked || !(state.catalog.methods", editor)
        self.assertIn(
            '<option value="" selected disabled>Choose ${escapeHtml(label)}</option>',
            choices,
        )
        self.assertIn("pair.compatible === true", readiness)
        self.assertIn("!studyComponentPairReady(plan)", presentation)

    def test_saved_state_and_launch_readiness_are_separate_facts(self) -> None:
        status = _function_source(
            self.source,
            "studyPersistencePresentation",
            "labeledStatusPill",
        )
        requirements = _function_source(
            self.source,
            "studyRuntimeEnvironmentRequirementsPanel",
            "studyValidationPanel",
        )

        self.assertIn('"Saved draft"', status)
        self.assertIn('"Ready to launch"', status)
        self.assertIn('"Setup needed"', status)
        self.assertIn("studyRuntimeSetupReason(plan)", status)
        self.assertIn("only to this Run's Method process", requirements)
        self.assertIn("not copied into the Run record", requirements)
        self.assertIn("Open Studio Settings", requirements)

    def test_catalog_entries_have_one_direct_new_study_path(self) -> None:
        detail = _function_source(
            self.source,
            "renderComponentDetail",
            "componentEditableWorkspaceCapability",
        )
        choices = _function_source(
            self.source,
            "catalogChoicesForPlan",
            "studyComponentCompatibilityMessage",
        )
        editor = _function_source(
            self.source,
            "studyConfigEditor",
            "studyAdvancedGroup",
        )

        self.assertIn("Choose ${escapeHtml(counterpartLabel)}", detail)
        self.assertIn("component-compatible-options", detail)
        self.assertIn("first.focus({ preventScroll: true })", detail)
        self.assertNotIn("createPlanFromPair(pairs[0])", detail)
        self.assertIn("No compatible", detail)
        self.assertIn('catalogChoicesForPlan("environment", plan)', editor)
        self.assertIn('catalogChoicesForPlan("method", plan)', editor)
        self.assertIn("choiceCompatibility", choices)
        self.assertIn("Incompatible selection:", self.source)

    def test_study_and_launch_preparation_use_plain_product_language(self) -> None:
        sections = [
            _function_source(self.source, "studyConfigEditor", "studyAdvancedGroup"),
            _function_source(self.source, "studyGuidePanel", "studyCardHeading"),
            _function_source(self.source, "studySourceNote", "hasWorkspaceStudyDraft"),
            _function_source(self.source, "studyReadinessPanel", "studyValidationPanel"),
            _function_source(self.source, "studyReadinessRows", "readinessRow"),
            _function_source(
                self.source,
                "renderOperatorJobSummary",
                "renderOperatorJobReviewAction",
            ),
            _async_function_source(self.source, "launchPlan", "submitStudyLaunch"),
        ]
        visible_copy = "\n".join(sections)

        self.assertNotIn("Realm evidence", visible_copy)
        self.assertNotIn("Retained execution", visible_copy)
        self.assertNotIn("retained package snapshot", visible_copy.lower())
        self.assertNotIn("canonical Realm Workbench", visible_copy)
        self.assertIn("Environment evaluates Candidates", visible_copy)
        self.assertIn("Launching starts a Run", visible_copy)
        self.assertIn("Launch status", visible_copy)
        self.assertIn("Advanced settings", visible_copy)

    def test_primary_and_advanced_sections_have_dedicated_compact_styles(self) -> None:
        for selector in (
            ".study-primary-settings",
            ".study-card-heading.study-primary-heading",
            ".study-settings-group",
            ".study-step-number",
            ".study-advanced-group",
            ".study-action-reason",
            ".study-launch-status",
        ):
            self.assertIn(selector, self.styles)
        self.assertRegex(
            self.styles,
            r"\.study-card-heading\.study-primary-heading\s*\{"
            r"[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s+auto;",
        )


if __name__ == "__main__":
    unittest.main()
