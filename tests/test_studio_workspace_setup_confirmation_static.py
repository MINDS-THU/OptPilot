"""Browser contracts for the deterministic Workspace Setup hierarchy."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
_HTML = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "index.html"
_CSS = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "styles.css"


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    return source[start : source.index(f"function {next_name}(", start)]


class StudioWorkspaceSetupConfirmationStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")
        cls.html = _HTML.read_text(encoding="utf-8")
        cls.styles = _CSS.read_text(encoding="utf-8")

    def test_setup_has_one_changing_primary_action(self) -> None:
        hierarchy = _function(
            self.source,
            "registrationActionHierarchyHtml",
            "planCanApply",
        )

        self.assertIn('primary-button registration-validate', hierarchy)
        self.assertIn('primary-button registration-smoke', hierarchy)
        self.assertIn('primary-button registration-apply', hierarchy)
        self.assertIn('ghost-button registration-smoke', hierarchy)
        self.assertIn("Run optional test", hierarchy)
        self.assertIn("More", hierarchy)

    def test_opening_setup_restores_the_server_backed_plan(self) -> None:
        binding = _function(
            self.source,
            "bindEvents",
            "shouldRefreshSelectedRunDetail",
        )

        self.assertIn('button.dataset.workbenchMode === "setup"', binding)
        self.assertIn("openRegistrationMenu()", binding)

    def test_prepared_dependencies_use_plain_check_then_test_language(self) -> None:
        validation_summary = _function(
            self.source,
            "packageValidationSummary",
            "packageSmokeSummary",
        )
        setup_summary = _function(
            self.source,
            "targetSetupSummaryHtml",
            "packagePlanValidationHtml",
        )
        validation = _function(
            self.source,
            "packagePlanValidationHtml",
            "packagePlanSmokeHtml",
        )
        result = _function(
            self.source,
            "packagePlanSmokeHtml",
            "resourceRegistrationHtml",
        )

        self.assertIn('runtime.setup.cache === "prepared"', setup_summary)
        self.assertIn(
            "Check verifies the declaration and referenced lock-file paths",
            setup_summary,
        )
        self.assertIn("Test verifies the supplied packages", setup_summary)
        self.assertIn(
            "runs the Study with its normal runtime", setup_summary
        )
        self.assertIn(
            "Static checks passed; run Test to verify executable behavior",
            validation_summary,
        )
        self.assertIn(
            "Whole folder passed static checks",
            validation_summary,
        )
        self.assertNotIn("requires_smoke", validation_summary)
        self.assertIn("Run Test to verify executable behavior", validation)
        self.assertIn("<strong>Test</strong>", result)
        self.assertIn("Test passed in the Study runtime", result)
        self.assertNotIn("requires Smoke", validation)

    def test_registration_confirmation_is_accessible_and_complete(self) -> None:
        renderer = _function(
            self.source,
            "renderRegistrationConfirmation",
            "handleRegistrationConfirmationKeydown",
        )

        self.assertIn('role="dialog"', self.html)
        self.assertIn('aria-modal="true"', self.html)
        self.assertIn('aria-labelledby="registrationConfirmationTitle"', self.html)
        for label in (
            "Catalog name",
            "Component kind",
            "Checked version",
            "Check",
            "Test",
            "Current files",
        ):
            self.assertIn(label, renderer)
        self.assertIn("Current files match the checked version", renderer)
        self.assertIn("Checked publication summary", renderer)
        self.assertIn('pending.submitting ? "Publishing…" : "Publish"', renderer)
        self.assertIn("More · technical details", renderer)

    def test_generated_environment_starter_explains_the_exact_enable_step(self) -> None:
        resource_setup = _function(
            self.source,
            "resourceRegistrationHtml",
            "registrationStep",
        )
        event_binding = _function(
            self.source,
            "bindRegistrationMenu",
            "requestWorkspaceCuration",
        )

        self.assertIn("environment.template.yaml.disabled", resource_setup)
        self.assertIn("rename the template to environment.yaml", resource_setup)
        self.assertIn("metric_values names match metrics.keys", resource_setup)
        self.assertIn("replace metrics.keys: [score]", event_binding)
        self.assertIn("Rename the .template.yaml.disabled file", event_binding)
        self.assertIn("choose Find Catalog items again", event_binding)

    def test_workspace_uses_one_code_interface_publish_navigation(self) -> None:
        self.assertIn(">Code</button>", self.html)
        self.assertIn(">Interface</button>", self.html)
        self.assertIn(">Publish</button>", self.html)
        self.assertNotIn('id="workspaceWorkingInterfaceButton"', self.html)
        self.assertNotIn('id="primaryActionButton"', self.html)
        self.assertNotIn(">Catalog setup</button>", self.html)

        registration = _function(
            self.source,
            "registrationMenuHtml",
            "renderWorkspaceSetup",
        )
        self.assertIn("Publish to Catalog", registration)
        self.assertIn("publishedRegistrationHtml", registration)
        self.assertIn("Publication details", registration)
        self.assertIn("View in Catalog", registration)
        for selector in (
            ".registration-plan",
            ".registration-plan-block",
            ".config-section-title",
            ".publication-success",
            ".publication-detail-grid",
        ):
            self.assertIn(selector, self.styles)

    def test_confirmation_refreshes_check_before_apply_and_success_opens_catalog(self) -> None:
        prepare = _function(
            self.source,
            "prepareRegistrationConfirmation",
            "confirmCheckedRegistration",
        )
        confirm = _function(
            self.source,
            "confirmCheckedRegistration",
            "bindRegistrationMenu",
        )

        self.assertIn("/validate", prepare)
        self.assertIn("openRegistrationConfirmation", prepare)
        self.assertLess(prepare.index("/validate"), prepare.index("openRegistrationConfirmation"))
        self.assertIn("/apply", confirm)
        self.assertIn("await loadCatalogAndCompatibility()", confirm)
        self.assertIn("openRegisteredCatalogResult(applied)", confirm)
        self.assertNotIn("window.confirm", confirm)


if __name__ == "__main__":
    unittest.main()
