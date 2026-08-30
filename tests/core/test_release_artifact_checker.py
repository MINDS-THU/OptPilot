"""The release checker proves licence and cross-distribution boundaries."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.check_release_artifacts import (
    STUDIO_REQUIRED_SDIST_ENTRIES,
    _check_sdist,
    _check_wheel,
)


class ReleaseArtifactCheckerTest(unittest.TestCase):
    def test_wheel_requires_the_declared_licence_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            missing = root / "demo-1-py3-none-any.whl"
            complete = root / "complete-1-py3-none-any.whl"
            for path, include_license in ((missing, False), (complete, True)):
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("demo-1.dist-info/METADATA", "Version: 1\n")
                    archive.writestr("demo-1.dist-info/entry_points.txt", "")
                    if include_license:
                        archive.writestr(
                            "demo-1.dist-info/licenses/LICENSE", "licence text\n"
                        )

            missing_errors = _check_wheel(
                missing,
                required=set(),
                forbidden_prefixes=(),
                metadata_name="demo",
                version="1",
                required_entry_points=(),
            )
            complete_errors = _check_wheel(
                complete,
                required=set(),
                forbidden_prefixes=(),
                metadata_name="demo",
                version="1",
                required_entry_points=(),
            )

        self.assertEqual(
            missing_errors,
            [
                "demo-1-py3-none-any.whl is missing "
                "demo-1.dist-info/licenses/LICENSE"
            ],
        )
        self.assertEqual(complete_errors, [])

    def test_studio_sdist_requires_license_and_rejects_core_package_prefix(
        self,
    ) -> None:
        self.assertIn("LICENSE", STUDIO_REQUIRED_SDIST_ENTRIES)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "optpilot_studio-1.tar.gz"
            payload = b"from __future__ import annotations\n"
            member = tarfile.TarInfo(
                "optpilot_studio-1/src/optpilot/__init__.py"
            )
            member.size = len(payload)
            with tarfile.open(path, "w:gz") as archive:
                archive.addfile(member, io.BytesIO(payload))

            errors = _check_sdist(
                path,
                required=set(),
                forbidden_prefixes=("src/optpilot/",),
            )

        self.assertEqual(
            errors,
            [
                "optpilot_studio-1.tar.gz contains forbidden entry: "
                "src/optpilot/__init__.py"
            ],
        )


if __name__ == "__main__":
    unittest.main()
