from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optpilot_studio.ui import server as studio_server
from tests.core.test_realm_local_study_package import (
    _METHOD_IMAGE,
    _PACKAGE_IMAGE,
    _write_package,
    _write_package_settings,
)


class StudioPackageRuntimePreflightTest(unittest.TestCase):
    def test_catalog_list_and_detail_show_effective_package_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package_root = Path(raw) / "package"
            package_root.mkdir()
            _write_package(package_root)
            _write_package_settings(package_root)
            state = studio_server.UiState(
                cwd=Path(raw),
                catalog_roots=[package_root],
                run_roots=[],
            )
            try:
                catalog = studio_server._catalog_payload(state)
                environment = next(
                    item
                    for item in catalog["environments"]
                    if item["id"] == "clean-local-environment"
                )
                method = next(
                    item
                    for item in catalog["methods"]
                    if item["id"] == "clean-local-method"
                )
                environment_detail = studio_server._catalog_detail(
                    state,
                    "environment",
                    environment["uid"],
                )
                method_detail = studio_server._catalog_detail(
                    state,
                    "method",
                    method["uid"],
                )
            finally:
                state.close_coordination()

        for entry in (
            environment,
            method,
            environment_detail["entry"],
            method_detail["entry"],
        ):
            runtime = entry["summary"]["runtime"]
            self.assertEqual(
                runtime.get("sandbox") or runtime.get("type"),
                "container",
            )
            self.assertEqual(runtime["image"], _PACKAGE_IMAGE)
            self.assertEqual(runtime["platform"], "linux/amd64")
            self.assertEqual(runtime["networkPolicy"], "disabled")
            self.assertEqual(runtime["source"], "package")
            self.assertEqual(
                entry["package_metadata"]["runtime"],
                {
                    "sandbox": "container",
                    "container": {
                        "image": _PACKAGE_IMAGE,
                        "platform": "linux/amd64",
                    },
                },
            )

    def test_catalog_component_container_override_wins_over_package_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package_root = Path(raw) / "package"
            package_root.mkdir()
            _write_package(package_root)
            _write_package_settings(package_root)
            method_path = package_root / "configs" / "methods" / "method.yaml"
            method_path.write_text(
                method_path.read_text(encoding="utf-8")
                + "runtime:\n"
                + "  sandbox: container\n"
                + "  container:\n"
                + f"    image: {_METHOD_IMAGE}\n"
                + "    platform: linux/arm64\n"
                + "    network: enabled\n",
                encoding="utf-8",
            )
            state = studio_server.UiState(
                cwd=Path(raw),
                catalog_roots=[package_root],
                run_roots=[],
            )
            try:
                catalog = studio_server._catalog_payload(state)
                method = next(
                    item
                    for item in catalog["methods"]
                    if item["id"] == "clean-local-method"
                )
                detail = studio_server._catalog_detail(
                    state,
                    "method",
                    method["uid"],
                )
            finally:
                state.close_coordination()

        for entry in (method, detail["entry"]):
            runtime = entry["summary"]["runtime"]
            self.assertEqual(runtime["type"], "container")
            self.assertEqual(runtime["image"], _METHOD_IMAGE)
            self.assertEqual(runtime["platform"], "linux/arm64")
            self.assertEqual(runtime["networkPolicy"], "enabled")
            self.assertEqual(runtime["source"], "component")

    def test_catalog_preflight_uses_the_package_container_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package_root = Path(raw) / "package"
            package_root.mkdir()
            study = _write_package(package_root)
            _write_package_settings(package_root)
            captured = []

            with patch.object(
                studio_server,
                "preflight_retained_process_study",
                side_effect=lambda spec: captured.append(spec),
            ):
                validation = studio_server._validate_study(
                    study,
                    package_root=package_root,
                )

        self.assertTrue(validation["valid"], validation.get("errors"))
        self.assertTrue(validation["launch"]["eligible"])
        self.assertEqual(len(captured), 1)
        for component in (captured[0].environment, captured[0].method):
            self.assertEqual(component["runtime"]["type"], "container")
            self.assertEqual(
                component["runtime"]["container"]["image"],
                _PACKAGE_IMAGE,
            )

    def test_catalog_preflight_reports_package_runtime_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package_root = Path(raw) / "package"
            package_root.mkdir()
            study = _write_package(package_root)
            _write_package_settings(package_root)
            method = package_root / "configs" / "methods" / "method.yaml"
            method.write_text(
                method.read_text(encoding="utf-8")
                + "runtime:\n"
                + "  sandbox: process\n"
                + "  workdir: .\n",
                encoding="utf-8",
            )
            state = studio_server.UiState(
                cwd=Path(raw),
                catalog_roots=[package_root],
                run_roots=[],
            )
            try:
                catalog = studio_server._catalog_payload(state)
                indexed_method = next(
                    item
                    for item in catalog["methods"]
                    if item["id"] == "clean-local-method"
                )
                detail = studio_server._catalog_detail(
                    state, "method", indexed_method["uid"]
                )
                validation = studio_server._validate_study(
                    study,
                    state=state,
                    package_root=(
                        studio_server._configured_study_package_root_if_known(
                            state, study
                        )
                    ),
                )
            finally:
                state.close_coordination()

        self.assertFalse(validation["valid"])
        self.assertIn("process-only", validation["errors"][0])
        self.assertFalse(detail["validation"]["valid"])
        self.assertTrue(
            any("process-only" in error for error in detail["validation"]["errors"]),
            detail["validation"],
        )


if __name__ == "__main__":
    unittest.main()
