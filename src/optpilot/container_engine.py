"""Finding the container program, and checking an image before a run starts.

Four things have to be true before any of a package's code runs, and all four are
checked before anything is written, so a failure leaves nothing behind:

  the container program is installed
  the image is present on this machine
  its fingerprint is the one the package names
  it was built for this machine's architecture

Each failure has its own code so the reason survives into a record and into a
message, rather than arriving as one undifferentiated "could not start".

Two deliberate refusals:

The program is never taken from a package. It is found from the operator's own
configuration or from the search path, and only ``docker`` or ``podman`` are
accepted -- a package that could name the program to run would be choosing what
executes on the machine, which is a larger permission than running inside a
container it named.

Nothing here ever downloads an image. The design requires an image to exist
before it is named, so a missing one is reported and the launch stops. Fetching
it is a deliberate step someone takes, not something that happens inside a run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .image_reference import ImageReference, parse_image_reference

__all__ = [
    "CONTAINER_ENGINE_NAMES",
    "ContainerEngineError",
    "ImageInspection",
    "inspect_image",
    "resolve_container_engine",
    "verify_image_available",
]

#: The only programs that will be run. Not configurable by a package.
CONTAINER_ENGINE_NAMES = ("docker", "podman")

#: How long to wait for the program to describe an image. Inspection is a local
#: lookup, so a slow answer means something is wrong rather than busy.
INSPECT_TIMEOUT_SECONDS = 30


class ContainerEngineError(RuntimeError):
    """A named reason a container could not be started."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImageInspection:
    """What the container program reports about an image it holds."""

    config_digest: Optional[str]
    repo_digests: tuple[str, ...]
    os_name: str
    architecture: str

    @property
    def platform(self) -> str:
        return f"{self.os_name}/{self.architecture}"


def resolve_container_engine(
    configured: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Find the container program, or say plainly that there is none.

    ``configured`` comes from the operator, never from a package, and is still
    checked against the accepted names so a stray value cannot redirect what runs.
    """

    environ = os.environ if environ is None else environ
    candidates: Sequence[str]
    if configured:
        name = str(configured).strip()
        if name not in CONTAINER_ENGINE_NAMES:
            raise ContainerEngineError(
                "container_engine_unsupported",
                f"{name!r} is not a container program OptPilot will run. "
                f"Choose one of: {', '.join(CONTAINER_ENGINE_NAMES)}.",
            )
        candidates = (name,)
    else:
        candidates = CONTAINER_ENGINE_NAMES

    for name in candidates:
        found = shutil.which(name, path=environ.get("PATH"))
        if found:
            return found

    raise ContainerEngineError(
        "container_engine_unavailable",
        "No container program is installed. Every component runs in a "
        f"container, so one of {' or '.join(CONTAINER_ENGINE_NAMES)} must be "
        "available on this machine.",
    )


def inspect_image(engine: str, reference: str) -> Optional[ImageInspection]:
    """Describe an image already on this machine, or ``None`` if absent.

    Never fetches. A program that cannot be run at all is a different failure
    from an image that is not there, and is reported as one.
    """

    try:
        completed = subprocess.run(
            [
                engine,
                "image",
                "inspect",
                reference,
                "--format",
                "{{json .}}",
            ],
            capture_output=True,
            text=True,
            timeout=INSPECT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise ContainerEngineError(
            "container_engine_unavailable",
            f"The container program could not be run: {error}",
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ContainerEngineError(
            "container_engine_unresponsive",
            "The container program did not answer within "
            f"{INSPECT_TIMEOUT_SECONDS} seconds. It may be installed but not "
            "running.",
        ) from error

    if completed.returncode != 0:
        stderr = (completed.stderr or "").lower()
        # A program that cannot reach its service is not an absent image. Left
        # undistinguished, someone whose container software simply is not
        # running would be told to go and fetch an image they already have.
        if any(
            marker in stderr
            for marker in (
                "cannot connect",
                "failed to connect",
                "is the docker daemon running",
                "connection refused",
                "no such file or directory",
                "permission denied",
            )
        ):
            raise ContainerEngineError(
                "container_engine_unavailable",
                "The container program is installed but could not be reached. "
                "It is most likely not running. "
                + (completed.stderr or "").strip()[:200],
            )
        return None

    payload = (completed.stdout or "").strip()
    if not payload:
        return None
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ContainerEngineError(
            "container_engine_unreadable",
            f"The container program described the image in a form OptPilot "
            f"could not read: {error}",
        ) from error
    if isinstance(raw, list):
        if not raw:
            return None
        raw = raw[0]
    if not isinstance(raw, dict):
        raise ContainerEngineError(
            "container_engine_unreadable",
            "The container program did not describe the image as an object.",
        )

    repo_digests = raw.get("RepoDigests") or []
    return ImageInspection(
        config_digest=(raw.get("Id") or None),
        repo_digests=tuple(str(item) for item in repo_digests if item),
        os_name=str(raw.get("Os") or ""),
        architecture=str(raw.get("Architecture") or ""),
    )


def verify_image_available(
    engine: str,
    image: str | ImageReference,
    platform: str,
) -> ImageInspection:
    """Check the image is here, is the one named, and fits this machine.

    Returns what the program reported, so a caller can record it.
    """

    reference = image if isinstance(image, ImageReference) else parse_image_reference(image)
    inspection = inspect_image(engine, reference.raw)
    if inspection is None:
        raise ContainerEngineError(
            "container_image_absent",
            f"The image {reference.raw} is not on this machine. OptPilot never "
            "downloads one during a run, because what a download returns can "
            "differ from one time to the next. Fetch it first, then launch.",
        )

    if not reference.matches_inspection(
        inspection.config_digest, inspection.repo_digests
    ):
        # Which digest decides depends on the shape of the reference; see
        # image_reference. Comparing the wrong pair rejects every correctly
        # pinned image, so the message says what was compared.
        held = (
            ", ".join(inspection.repo_digests)
            if reference.names_manifest_digest
            else str(inspection.config_digest)
        )
        raise ContainerEngineError(
            "container_image_mismatch",
            f"The image on this machine is not the one {reference.raw} names. "
            f"It reports {held or 'nothing'}. An image named by fingerprint must "
            "be exactly those bytes, or a run's record would not describe what "
            "ran.",
        )

    if inspection.platform != platform:
        raise ContainerEngineError(
            "container_image_platform_mismatch",
            f"The image {reference.raw} was built for {inspection.platform}, but "
            f"the package declares {platform}. The same image reference on a "
            "machine of a different architecture is different bytes under the "
            "same name.",
        )

    return inspection
