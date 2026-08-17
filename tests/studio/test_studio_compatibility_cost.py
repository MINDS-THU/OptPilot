"""Building the compatibility answer is paid once, not per request.

The Catalog page asks for the catalog and the compatibility pairs together, so
anything expensive here is paid on every visit. Two costs were real: each
method's settings file was parsed once per environment, and the whole payload
was rebuilt on every request with no reuse at all.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optpilot_studio.ui import server as ui_server


def _entry(name: str, kind: str, path: Path) -> dict:
    return {"id": name, "kind": kind, "label": name, "_source_path": str(path)}


class CompatibilityCostTest(unittest.TestCase):
    def _catalog(self, tmp: Path, *, environments: int, methods: int) -> dict:
        paths = []
        for index in range(environments + methods):
            path = tmp / f"config{index}.yaml"
            path.write_text("id: x\n", encoding="utf-8")
            paths.append(path)
        return {
            "environments": [
                _entry(f"env{i}", "environment", paths[i])
                for i in range(environments)
            ],
            "methods": [
                _entry(f"method{i}", "method", paths[environments + i])
                for i in range(methods)
            ],
        }

    def test_each_settings_file_is_read_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self._catalog(Path(tmp), environments=14, methods=13)
            reads: list[str] = []

            def counting_read(path):
                reads.append(str(path))
                return {"id": "x"}

            state = type("S", (), {"catalog_refresh_ttl_seconds": 0.0})()
            with (
                patch.object(ui_server, "_catalog_index_payload", return_value=catalog),
                patch.object(ui_server, "_read_yaml", counting_read),
                patch.object(ui_server, "_compatibility_result", lambda *a: {"ok": True}),
                patch.object(ui_server, "_public_catalog_entry", lambda item: item),
            ):
                payload = ui_server._compatibility_payload(state)

        self.assertEqual(len(payload["pairs"]), 14 * 13)
        self.assertEqual(
            len(reads),
            27,
            "each settings file must be parsed once, not once per pairing",
        )
        self.assertEqual(len(set(reads)), 27)

    def test_the_answer_is_reused_within_the_refresh_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self._catalog(Path(tmp), environments=2, methods=2)
            builds: list[int] = []

            def counting_result(*_args):
                builds.append(1)
                return {"ok": True}

            state = type(
                "S",
                (),
                {
                    "catalog_refresh_ttl_seconds": 5.0,
                    "_catalog_projection_lock": __import__("threading").RLock(),
                    "_compatibility_cache": None,
                },
            )()
            with (
                patch.object(ui_server, "_catalog_index_payload", return_value=catalog),
                patch.object(ui_server, "_read_yaml", lambda _p: {"id": "x"}),
                patch.object(ui_server, "_compatibility_result", counting_result),
                patch.object(ui_server, "_public_catalog_entry", lambda item: item),
            ):
                first = ui_server._compatibility_payload(state)
                second = ui_server._compatibility_payload(state)

        self.assertEqual(len(builds), 4, "the second call rebuilt the pairs")
        self.assertIs(first, second)

    def test_a_refreshed_catalog_rebuilds_the_answer(self) -> None:
        # Reuse is keyed on the exact catalog it came from, so new packages
        # cannot be masked by a stale answer.
        with tempfile.TemporaryDirectory() as tmp:
            first_catalog = self._catalog(Path(tmp), environments=1, methods=1)
            second_catalog = self._catalog(Path(tmp), environments=2, methods=1)
            state = type(
                "S",
                (),
                {
                    "catalog_refresh_ttl_seconds": 5.0,
                    "_catalog_projection_lock": __import__("threading").RLock(),
                    "_compatibility_cache": None,
                },
            )()
            with (
                patch.object(ui_server, "_read_yaml", lambda _p: {"id": "x"}),
                patch.object(ui_server, "_compatibility_result", lambda *a: {"ok": True}),
                patch.object(ui_server, "_public_catalog_entry", lambda item: item),
            ):
                with patch.object(
                    ui_server, "_catalog_index_payload", return_value=first_catalog
                ):
                    first = ui_server._compatibility_payload(state)
                with patch.object(
                    ui_server, "_catalog_index_payload", return_value=second_catalog
                ):
                    second = ui_server._compatibility_payload(state)

        self.assertEqual(len(first["pairs"]), 1)
        self.assertEqual(len(second["pairs"]), 2)


if __name__ == "__main__":
    unittest.main()
