"""What the Settings dialog says after you press Save.

The dialog had one status banner, used only for "settings could not be
loaded". A save that was refused went somewhere else entirely: it overwrote
the Assistant's runtime status with mode "unavailable", so a mistyped value
made the Assistant look broken and the sentence naming the bad value was never
shown at all.

The banner now covers all three outcomes -- loading, a refused save, and a save
that went through with something left out -- and a refused save no longer
touches the Assistant's status, because nothing about the Assistant changed.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_APP = (
    Path(__file__).resolve().parents[2]
    / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
)


class SettingsDialogReportsSavesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _APP.read_text(encoding="utf-8", errors="replace")

    def _save_body(self) -> str:
        i = self.app.index('const result = await postJson("/api/agent/settings"')
        return self.app[i : i + 1100]

    def test_a_refused_save_does_not_report_the_assistant_as_broken(self) -> None:
        body = self._save_body()
        self.assertIn("state.settingsSaveError", body)
        self.assertNotIn('mode: "unavailable"', body)

    def test_a_refused_save_keeps_the_reason(self) -> None:
        self.assertIn("state.settingsSaveError = String(result.error)", self.app)

    def test_what_a_save_left_out_is_kept(self) -> None:
        self.assertIn("state.settingsSaveNotices", self.app)

    def test_the_banner_covers_every_outcome(self) -> None:
        i = self.app.index("function renderSettingsModal")
        body = self.app[i : i + 2200]
        self.assertIn("That setting was not saved", body)
        self.assertIn("Saved, with one thing left out", body)
        self.assertIn("Studio settings could not be loaded", body)

    def test_save_stays_clickable_after_a_refusal(self) -> None:
        # The person has to be able to correct the value and try again; only a
        # failed LOAD leaves the form untrustworthy enough to disable saving.
        i = self.app.index("if (els.settingsSaveButton) {")
        body = self.app[i : i + 220]
        self.assertIn("state.settingsError", body)
        self.assertNotIn("settingsSaveError", body)


if __name__ == "__main__":
    unittest.main()
