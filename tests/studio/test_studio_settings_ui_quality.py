"""Release contracts for the Studio Settings interaction.

These checks keep the security-sensitive Settings form honest even though the
frontend is shipped as dependency-free static JavaScript.  Live browser QA
still covers layout and interaction; these tests protect the underlying
ownership, preservation, and accessibility decisions from quiet regressions.
"""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}")
    end = source.index(f"function {next_name}", start)
    return source[start:end]


class StudioSettingsUiQualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = (_STATIC / "app.js").read_text(encoding="utf-8")
        cls.html = (_STATIC / "index.html").read_text(encoding="utf-8")
        cls.styles = (_STATIC / "styles.css").read_text(encoding="utf-8")

    def test_opening_settings_owns_the_mobile_surface(self) -> None:
        body = _function(self.app, "openSettings", "loadSettingsForModal")
        self.assertIn("openedFromMobileRail", body)
        self.assertIn("state.shell.mobileRailOpen = false", body)
        self.assertIn("renderShell()", body)
        self.assertIn("els.railToggleButton", body)
        self.assertIn("z-index: 130", self.styles)

    def test_settings_tabs_follow_the_keyboard_tabs_pattern(self) -> None:
        for tab, panel in (
            ("settingsAssistantTab", "settingsAssistantPanel"),
            ("settingsPermissionsTab", "settingsPermissionsPanel"),
            ("settingsEnvironmentTab", "settingsEnvironmentPanel"),
        ):
            self.assertIn(f'id="{tab}"', self.html)
            self.assertIn(f'aria-labelledby="{tab}"', self.html)
            self.assertIn(f'aria-controls="{panel}"', self.html)
        keyboard = _function(
            self.app, "handleSettingsTabKeydown", "markSettingsDirty"
        )
        for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
            self.assertIn(f'"{key}"', keyboard)
        self.assertIn("button.tabIndex = active ? 0 : -1", self.app)

    def test_inactive_capabilities_are_preserved_not_round_tripped(self) -> None:
        for element_id in (
            "assistantSkillsInput",
            "assistantMcpServersInput",
            "assistantCustomToolsInput",
        ):
            self.assertNotIn(f'id="{element_id}"', self.html)
        save = _function(self.app, "saveSettings", "parseStudioRoute")
        payload = save[save.index("const payload = {") : save.index(
            "state.settingsSaving = true"
        )]
        self.assertNotIn("capabilities:", payload)
        self.assertIn("Omitting the\n    // section preserves legacy data", save)

    def test_a_refused_save_keeps_the_visible_form(self) -> None:
        save = _function(self.app, "saveSettings", "parseStudioRoute")
        failed = save[save.index("if (result.error)") : save.index("} else {")]
        succeeded = save[save.index("} else {") :]
        self.assertNotIn("fillSettingsForm()", failed)
        self.assertIn("fillSettingsForm()", succeeded)
        self.assertIn("state.settingsDirty = false", succeeded)

    def test_partial_environment_value_is_an_accessible_error(self) -> None:
        self.assertIn('id="environmentVariableError"', self.html)
        self.assertIn('role="alert"', self.html)
        self.assertEqual(self.html.count('aria-describedby="environmentVariableError"'), 2)
        validation = _function(
            self.app,
            "pendingEnvironmentVariableInput",
            "addEnvironmentVariableDraft",
        )
        self.assertIn("Variable names must start", validation)
        self.assertIn("Enter a value, or leave both fields empty", validation)
        self.assertIn('setAttribute("aria-invalid", "true")', self.app)

    def test_saved_environment_removal_is_staged_and_reversible(self) -> None:
        render = _function(
            self.app,
            "renderEnvironmentVariablesList",
            "clearEnvironmentVariableError",
        )
        self.assertIn("will be removed when saved", render)
        self.assertIn('aria-pressed="${record.removePending', render)
        self.assertIn("Undo", render)
        self.assertIn("saved value for", render)
        self.assertNotIn('<label class="env-secret-row">', render)

    def test_dirty_saving_and_success_states_are_explicit(self) -> None:
        for state_name in (
            "settingsSaving",
            "settingsSaveSucceeded",
            "settingsDirty",
            "settingsCloseWarning",
        ):
            self.assertIn(f"{state_name}:", self.app)
        render = _function(self.app, "renderSettingsModal", "fillSettingsForm")
        self.assertIn("!state.settingsDirty", render)
        self.assertIn('"Saving…"', render)
        self.assertIn('"Settings saved"', render)
        self.assertIn('"Discard changes"', render)
        self.assertIn("input, select, textarea, button", render)
        self.assertIn("els.settingsCancelButton.disabled = state.settingsSaving", render)
        self.assertIn("els.settingsCloseButton.disabled = state.settingsSaving", render)
        close_request = _function(
            self.app, "requestCloseSettings", "closeSettings"
        )
        self.assertIn("if (state.settingsSaving) return", close_request)

    def test_permissions_have_a_dedicated_explanatory_section(self) -> None:
        self.assertIn('data-settings-tab="permissions"', self.html)
        self.assertIn("These defaults apply when the Assistant proposes an action", self.html)
        self.assertIn("Use recommended defaults", self.html)
        self.assertIn("Code and package execution", self.html)
        self.assertIn("Publishing and Run lifecycle", self.html)

    def test_small_screen_settings_are_a_full_height_sheet(self) -> None:
        self.assertIn("#settingsModal {", self.styles)
        self.assertIn("height: 100dvh", self.styles)
        self.assertIn("max-height: 100dvh", self.styles)
        self.assertIn("grid-template-columns: 1fr", self.styles)
        self.assertIn("min-height: 44px", self.styles)

    def test_plaintext_storage_scope_is_visible(self) -> None:
        self.assertIn("Plaintext, project-scoped storage", self.html)
        self.assertIn('id="settingsStoragePath"', self.html)
        self.assertIn("If the project folder is synchronized", self.html)
        self.assertIn('state.settingsStoragePath = String(payload.settings_path', self.app)


if __name__ == "__main__":
    unittest.main()
