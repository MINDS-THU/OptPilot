"""Static browser contracts for configured-source Workspace Setup."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_APP_JS = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
_INDEX_HTML = (
    _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "index.html"
)


def _source_between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


class StudioConfiguredPackagePublishStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP_JS.read_text(encoding="utf-8")
        cls.html = _INDEX_HTML.read_text(encoding="utf-8")

    def test_catalog_renders_configured_sources_independently_of_entries(self) -> None:
        renderer = _source_between(
            self.source,
            "function renderConfiguredCatalogSources()",
            "async function openConfiguredCatalogSourceWorkspace(",
        )

        self.assertIn('id="catalogSources"', self.html)
        self.assertIn("state.catalog.sources", renderer)
        self.assertIn('class="catalog-source-disclosure"', renderer)
        self.assertIn("Local packages", renderer)
        self.assertIn("Published · Catalog revision", renderer)
        self.assertIn("Not published", renderer)
        self.assertIn('aria-label="Open ${escapeHtml(sourceLabel)} as a Workspace"', renderer)
        self.assertNotIn("without copying it", renderer)

    def test_source_action_opens_workspace_then_the_shared_setup_flow(self) -> None:
        opened = _source_between(
            self.source,
            "async function openConfiguredCatalogSourceWorkspace(",
            "function renderCatalogPackageFilter(",
        )
        request = _source_between(
            opened,
            "const payload = await postJson(",
            "const workspace = mergeUiWorkspace(payload.workspace)",
        )
        self.assertIn("/api/catalog/sources/${encodeURIComponent(sourceId)}/workspace", request)
        self.assertIn('schema: "optpilot.configured-source-workspace.v1"', request)
        self.assertIn("mergeUiWorkspace(payload.workspace)", opened)
        self.assertIn('setView("workspace")', opened)
        self.assertIn('state.workbenchMode = "setup"', opened)
        self.assertIn("await openRegistrationMenu()", opened)
        self.assertNotIn("path:", request)
        self.assertNotIn("package_id:", request)

    def test_whole_package_setup_is_one_composed_check_register_experience(self) -> None:
        registration = _source_between(
            self.source,
            "function registrationMenuHtml()",
            "function renderWorkspaceSetup()",
        )
        helpers = _source_between(
            self.source,
            "function packageSmokeSummary(",
            "function packagePlanDetailsHtml(",
        )

        self.assertIn('plan.publication_scope === "configured-whole-package"', registration)
        self.assertIn("Whole configured folder", registration)
        self.assertIn("registrationTestStepStatus(plan)", registration)
        self.assertIn('plan.publication_scope === "configured-whole-package"', helpers)
        self.assertIn("Check is static and does not execute Workspace code", helpers)

    def test_existing_study_plan_edits_are_preserved_while_refs_are_remapped(self) -> None:
        remap = _source_between(
            self.source,
            "function captureStudyPlanCatalogBindings()",
            "function renderCatalogPackageFilter(",
        )
        confirmation = _source_between(
            self.source,
            "async function confirmCheckedRegistration()",
            "function bindRegistrationMenu()",
        )

        self.assertIn("state.plans.map", remap)
        self.assertIn("planBindings.forEach", remap)
        self.assertIn('["study", "environment", "method"]', remap)
        self.assertIn("plan[kind] = replacement", remap)
        self.assertNotIn("state.plans =", remap)
        self.assertNotIn("buildPlans()", remap)
        self.assertIn('entry.ref.source_kind === "realm-catalog"', remap)
        self.assertIn("source_kind: originalRef", remap)
        self.assertIn(
            "const planBindings = captureStudyPlanCatalogBindings()",
            confirmation,
        )
        self.assertIn(
            "remapStudyPlansToRealmCatalogEntries(planBindings)",
            confirmation,
        )
        self.assertLess(
            confirmation.index("captureStudyPlanCatalogBindings()"),
            confirmation.index("await loadCatalogAndCompatibility()"),
        )
        self.assertLess(
            confirmation.index("await loadCatalogAndCompatibility()"),
            confirmation.index("remapStudyPlansToRealmCatalogEntries(planBindings)"),
        )

    def test_existing_realm_study_bindings_remain_revision_pinned(self) -> None:
        remap = _source_between(
            self.source,
            "function remapStudyPlansToRealmCatalogEntries(planBindings)",
            "function renderCatalogPackageFilter(",
        )

        immutable_guard = 'binding.source_kind !== "configured-filesystem-import"'
        replacement_lookup = "const replacement = entries.find"
        self.assertIn(immutable_guard, remap)
        self.assertIn(replacement_lookup, remap)
        self.assertLess(remap.index(immutable_guard), remap.index(replacement_lookup))

    def test_study_builder_blocks_mutable_refs_and_opens_package_setup(self) -> None:
        publication = _source_between(
            self.source,
            "function studyCatalogPublicationSetup(plan)",
            "function studyRuntimeEnvironmentRequirements(plan)",
        )
        detail = _source_between(
            self.source,
            "function renderPlanDetail()",
            "function studyLaunchForPlan(",
        )
        save = _source_between(
            self.source,
            "async function generatePlanDraft(plan)",
            "async function savePlanDraft(",
        )
        launch = _source_between(
            self.source,
            "async function launchPlan(plan)",
            "function persistActiveStudyLaunch(",
        )

        self.assertIn('reference.source_kind === "configured-filesystem-import"', publication)
        self.assertIn("Publish package first", publication)
        self.assertIn("Run setup drafts use checked, immutable Catalog versions", publication)
        self.assertIn('data-study-package-source=', publication)
        self.assertIn("Boolean(publicationReason)", detail)
        self.assertIn("&& !publicationReason", detail)
        self.assertIn("openConfiguredCatalogSourceWorkspace", detail)
        self.assertIn('blockUnpublishedStudyAction(plan, "save")', save)
        self.assertIn('blockUnpublishedStudyAction(plan, "launch")', launch)


if __name__ == "__main__":
    unittest.main()
