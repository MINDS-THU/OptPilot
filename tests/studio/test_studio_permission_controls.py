"""Every Assistant permission the server honours has a control in Settings.

Permissions are stored as a fixed set of named keys, and the Settings dialog
lists them one hard-coded control at a time. Adding a key on the server without
adding its control does something worse than hiding it: the dialog builds the
whole permissions object from the controls it has, so the first time anyone
opens Settings and saves, the missing key silently returns to its default --
undoing a choice the person made deliberately, with no message.

That matters most for execution permissions, which must never silently become
less restrictive. These tests read the client files as text, the established
pattern for browser contracts in this suite.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    ASSISTANT_PERMISSION_VALUES,
    DEFAULT_ASSISTANT_PERMISSIONS,
)

_UI = (
    Path(__file__).resolve().parents[2]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
)


def _element_id(key: str) -> str:
    """`smoke_test` -> `assistantPermissionSmokeTest`."""

    return "assistantPermission" + "".join(part.title() for part in key.split("_"))


class PermissionControlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (_UI / "index.html").read_text(encoding="utf-8")
        cls.app = (_UI / "app.js").read_text(encoding="utf-8")

    def test_every_permission_has_a_control(self) -> None:
        for key in DEFAULT_ASSISTANT_PERMISSIONS:
            with self.subTest(permission=key):
                self.assertIn(f'id="{_element_id(key)}"', self.html)

    def test_browser_source_contains_no_binary_nul_bytes(self) -> None:
        self.assertNotIn("\x00", self.app)

    def test_every_control_is_read_and_written_by_the_client(self) -> None:
        for key in DEFAULT_ASSISTANT_PERMISSIONS:
            with self.subTest(permission=key):
                element = _element_id(key)
                self.assertIn(f"setSelectValue(els.{element}", self.app)
                self.assertIn(f"{key}: els.{element}", self.app)

    def test_every_control_offers_exactly_the_allowed_values(self) -> None:
        for key, allowed in ASSISTANT_PERMISSION_VALUES.items():
            with self.subTest(permission=key):
                start = self.html.index(f'id="{_element_id(key)}"')
                end = self.html.index("</select>", start)
                offered = set(re.findall(r'value="([a-z_]+)"', self.html[start:end]))
                self.assertEqual(offered, set(allowed))

    def test_the_saved_default_matches_the_servers_default(self) -> None:
        for key, default in DEFAULT_ASSISTANT_PERMISSIONS.items():
            with self.subTest(permission=key):
                element = _element_id(key)
                fallback = re.search(
                    rf"{key}: els\.{element} \? els\.{element}\.value : \"([a-z_]+)\"",
                    self.app,
                )
                self.assertIsNotNone(fallback, key)
                self.assertEqual(fallback.group(1), default)


if __name__ == "__main__":
    unittest.main()
