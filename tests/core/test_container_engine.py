"""Finding the container program, and checking an image before a run starts.

The case worth its own attention is the digest comparison. A reference written
as `repository@sha256:...` names a manifest digest, and one written as a bare
`sha256:...` names an image's config digest -- different values for the same
image. Comparing the wrong pair rejects every correctly pinned image, and does
so by refusing to start, which reads as a trust problem rather than a bug.
"""

import json
import subprocess
import unittest
from unittest.mock import patch

from optpilot.container_engine import (
    CONTAINER_ENGINE_NAMES,
    ContainerEngineError,
    ImageInspection,
    inspect_image,
    resolve_container_engine,
    verify_image_available,
)

CONFIG_DIGEST = "sha256:" + "a" * 64
MANIFEST_DIGEST = "sha256:" + "b" * 64
REPOSITORY = "ghcr.io/example/or-solving"


def _completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _inspect_payload(**overrides) -> str:
    payload = {
        "Id": CONFIG_DIGEST,
        "RepoDigests": [f"{REPOSITORY}@{MANIFEST_DIGEST}"],
        "Os": "linux",
        "Architecture": "amd64",
    }
    payload.update(overrides)
    return json.dumps(payload)


class ResolveEngineTests(unittest.TestCase):
    def test_finds_the_first_available_program(self) -> None:
        with patch("shutil.which", side_effect=lambda n, path=None: f"/usr/bin/{n}" if n == "podman" else None):
            self.assertEqual(resolve_container_engine(), "/usr/bin/podman")

    def test_an_operator_choice_is_honoured(self) -> None:
        with patch("shutil.which", side_effect=lambda n, path=None: f"/usr/bin/{n}"):
            self.assertEqual(resolve_container_engine("podman"), "/usr/bin/podman")

    def test_a_program_outside_the_accepted_names_is_refused(self) -> None:
        # A package that could name the program would be choosing what executes
        # on the machine, not merely what it executes inside.
        with self.assertRaises(ContainerEngineError) as caught:
            resolve_container_engine("/bin/sh")
        self.assertEqual(caught.exception.code, "container_engine_unsupported")
        for name in CONTAINER_ENGINE_NAMES:
            self.assertIn(name, str(caught.exception))

    def test_no_program_installed_is_named_as_such(self) -> None:
        with patch("shutil.which", return_value=None):
            with self.assertRaises(ContainerEngineError) as caught:
                resolve_container_engine()
        self.assertEqual(caught.exception.code, "container_engine_unavailable")


class InspectImageTests(unittest.TestCase):
    def test_an_absent_image_reads_as_none(self) -> None:
        with patch("subprocess.run", return_value=_completed(1)):
            self.assertIsNone(inspect_image("docker", CONFIG_DIGEST))

    def test_a_present_image_is_described(self) -> None:
        with patch("subprocess.run", return_value=_completed(0, _inspect_payload())):
            inspection = inspect_image("docker", CONFIG_DIGEST)
        self.assertEqual(inspection.config_digest, CONFIG_DIGEST)
        self.assertEqual(inspection.platform, "linux/amd64")

    def test_a_list_response_is_accepted(self) -> None:
        # Some versions wrap the object in a list.
        with patch("subprocess.run", return_value=_completed(0, f"[{_inspect_payload()}]")):
            self.assertIsNotNone(inspect_image("docker", CONFIG_DIGEST))

    def test_an_unrunnable_program_is_not_an_absent_image(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("no docker")):
            with self.assertRaises(ContainerEngineError) as caught:
                inspect_image("docker", CONFIG_DIGEST)
        self.assertEqual(caught.exception.code, "container_engine_unavailable")

    def test_a_program_that_never_answers_is_named(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 30)):
            with self.assertRaises(ContainerEngineError) as caught:
                inspect_image("docker", CONFIG_DIGEST)
        self.assertEqual(caught.exception.code, "container_engine_unresponsive")
        self.assertIn("not running", str(caught.exception))

    def test_a_stopped_daemon_is_not_reported_as_a_missing_image(self) -> None:
        """Found against a real daemon that had shut down mid-session.

        The program exits non-zero either way. Treating that as "the image is
        not here" sends someone to fetch an image they already have, while the
        actual problem is that their container software is not running.
        """

        stderr = (
            "failed to connect to the docker API at unix:///var/run/docker.sock; "
            "check if the path is correct and if the daemon is running"
        )
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(ContainerEngineError) as caught:
                inspect_image("docker", CONFIG_DIGEST)
        self.assertEqual(caught.exception.code, "container_engine_unavailable")
        self.assertIn("not running", str(caught.exception))

    def test_a_genuinely_absent_image_still_reads_as_none(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error: No such image: sha256:abc"
        )
        with patch("subprocess.run", return_value=completed):
            self.assertIsNone(inspect_image("docker", CONFIG_DIGEST))

    def test_unreadable_output_is_not_mistaken_for_absence(self) -> None:
        with patch("subprocess.run", return_value=_completed(0, "not json")):
            with self.assertRaises(ContainerEngineError) as caught:
                inspect_image("docker", CONFIG_DIGEST)
        self.assertEqual(caught.exception.code, "container_engine_unreadable")


class VerifyImageTests(unittest.TestCase):
    def test_a_bare_reference_is_checked_against_the_config_digest(self) -> None:
        with patch("subprocess.run", return_value=_completed(0, _inspect_payload())):
            inspection = verify_image_available("docker", CONFIG_DIGEST, "linux/amd64")
        self.assertEqual(inspection.platform, "linux/amd64")

    def test_a_repository_reference_is_checked_against_the_manifest_digest(self) -> None:
        # The trap: Id is CONFIG_DIGEST here, deliberately different.
        with patch("subprocess.run", return_value=_completed(0, _inspect_payload())):
            verify_image_available(
                "docker", f"{REPOSITORY}@{MANIFEST_DIGEST}", "linux/amd64"
            )

    def test_an_absent_image_says_it_will_not_be_downloaded(self) -> None:
        with patch("subprocess.run", return_value=_completed(1)):
            with self.assertRaises(ContainerEngineError) as caught:
                verify_image_available("docker", CONFIG_DIGEST, "linux/amd64")
        self.assertEqual(caught.exception.code, "container_image_absent")
        self.assertIn("never downloads", str(caught.exception))

    def test_a_different_image_under_the_same_name_is_refused(self) -> None:
        other = "sha256:" + "c" * 64
        with patch("subprocess.run", return_value=_completed(0, _inspect_payload(Id=other))):
            with self.assertRaises(ContainerEngineError) as caught:
                verify_image_available("docker", CONFIG_DIGEST, "linux/amd64")
        self.assertEqual(caught.exception.code, "container_image_mismatch")

    def test_the_wrong_architecture_is_refused_and_says_why(self) -> None:
        with patch(
            "subprocess.run",
            return_value=_completed(0, _inspect_payload(Architecture="arm64")),
        ):
            with self.assertRaises(ContainerEngineError) as caught:
                verify_image_available("docker", CONFIG_DIGEST, "linux/amd64")
        self.assertEqual(caught.exception.code, "container_image_platform_mismatch")
        self.assertIn("linux/arm64", str(caught.exception))
        self.assertIn("different bytes", str(caught.exception))

    def test_a_tag_is_refused_before_anything_is_run(self) -> None:
        with patch("subprocess.run", side_effect=AssertionError("must not run")):
            with self.assertRaises(ValueError) as caught:
                verify_image_available("docker", f"{REPOSITORY}:latest", "linux/amd64")
        self.assertIn("pinned by sha256", str(caught.exception))

    def test_every_failure_carries_its_own_code(self) -> None:
        # The reason has to survive into a record and a message, rather than
        # arriving as one undifferentiated failure to start.
        codes = set()
        cases = [
            (_completed(1), None),
            (_completed(0, _inspect_payload(Id="sha256:" + "c" * 64)), None),
            (_completed(0, _inspect_payload(Architecture="arm64")), None),
        ]
        for completed, _ in cases:
            with patch("subprocess.run", return_value=completed):
                try:
                    verify_image_available("docker", CONFIG_DIGEST, "linux/amd64")
                except ContainerEngineError as error:
                    codes.add(error.code)
        self.assertEqual(len(codes), 3, codes)


class NoNetworkTests(unittest.TestCase):
    def test_inspection_never_asks_the_program_to_fetch(self) -> None:
        seen = {}

        def record(args, **kwargs):
            seen["args"] = args
            return _completed(0, _inspect_payload())

        with patch("subprocess.run", side_effect=record):
            inspect_image("docker", CONFIG_DIGEST)
        self.assertIn("inspect", seen["args"])
        for forbidden in ("pull", "--pull", "run"):
            self.assertNotIn(forbidden, seen["args"])


if __name__ == "__main__":
    unittest.main()
