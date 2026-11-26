"""One vocabulary for container image references.

Three copies of the same "is this pinned?" pattern had grown up in
``realm/provider_trust_records.py``, ``realm/local_container_web_provider.py`` and
``realm/environment_preview.py``, alongside digest-only patterns elsewhere. They
agreed, which is why nothing had broken yet; keeping them agreeing by hand is the
part that does not scale.

The distinction this module exists to make explicit is the one that is easy to get
wrong. A pinned reference comes in two shapes, and they name *different digests*:

``sha256:<64hex>``
    An image **config** digest -- what ``docker image inspect`` reports as ``Id``.

``repository@sha256:<64hex>``
    A **manifest** digest -- what a registry serves, and what appears in
    ``RepoDigests``. It is NOT equal to the config digest of the same image.

Checking a ``repository@sha256:`` reference against ``Id`` therefore fails for every
legitimately pinned image. :meth:`ImageReference.matches_inspection` picks the right
comparison so no caller has to remember which is which.

Stdlib only, deliberately: this module is reachable from the code that runs inside a
container, which is guaranteed nothing but a Python interpreter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

__all__ = [
    "IMAGE_DIGEST_RE",
    "IMAGE_REFERENCE_RE",
    "ImageReference",
    "is_pinned_image_reference",
    "parse_image_reference",
]

_DIGEST = r"sha256:[0-9a-f]{64}"

#: A bare image config digest, as reported by ``docker image inspect`` ``Id``.
IMAGE_DIGEST_RE = re.compile(rf"^{_DIGEST}$")

#: Either shape of pinned reference. Anything else -- a tag such as ``:latest``, or a
#: bare repository name -- is refused, because it can resolve to different bytes
#: later and a record naming it would not mean anything.
IMAGE_REFERENCE_RE = re.compile(rf"^(?:{_DIGEST}|[^\s@]+@{_DIGEST})$")

#: How many bytes a reference may occupy. A repository name can be long; a digest
#: alone is 71.
MAX_IMAGE_REFERENCE_BYTES = 1024


@dataclass(frozen=True)
class ImageReference:
    """A reference that has been checked to be pinned."""

    raw: str
    digest: str
    repository: Optional[str]

    @property
    def names_manifest_digest(self) -> bool:
        """True when the digest is a manifest digest rather than a config digest.

        A repository-qualified reference names what the registry serves; a bare
        digest names what the local image store holds.
        """

        return self.repository is not None

    def matches_inspection(
        self,
        image_id: Optional[str],
        repo_digests: Iterable[str] = (),
    ) -> bool:
        """Whether an inspected image is the one this reference names.

        ``image_id`` is the config digest (``Id``) and ``repo_digests`` the
        registry-qualified manifest references (``RepoDigests``). Which one decides
        depends on the shape of the reference, which is the whole point of this
        method existing.
        """

        if self.names_manifest_digest:
            return any(
                str(entry).endswith(f"@{self.digest}") for entry in repo_digests
            )
        return image_id == self.digest


def parse_image_reference(value: object, *, subject: str = "image reference") -> ImageReference:
    """Parse and validate a pinned reference, or raise ``ValueError``.

    The error names what was wrong rather than restating the pattern, because the
    common mistake is a tag and the useful thing to say is "pin it by digest".
    """

    if not isinstance(value, str):
        raise ValueError(f"{subject} must be a string.")
    text = value.strip()
    if not text:
        raise ValueError(f"{subject} must not be empty.")
    if len(text.encode("utf-8")) > MAX_IMAGE_REFERENCE_BYTES:
        raise ValueError(
            f"{subject} must be at most {MAX_IMAGE_REFERENCE_BYTES} bytes."
        )
    if IMAGE_REFERENCE_RE.fullmatch(text) is None:
        raise ValueError(
            f"{subject} must be pinned by sha256 digest, as either "
            f"'sha256:<64 hex>' or 'repository@sha256:<64 hex>'. "
            f"A tag such as ':latest' can resolve to different bytes later, so a "
            f"record naming it would not describe what ran."
        )
    repository, separator, _ = text.partition("@")
    if separator:
        return ImageReference(raw=text, digest=text[len(repository) + 1:], repository=repository)
    return ImageReference(raw=text, digest=text, repository=None)


def is_pinned_image_reference(value: object) -> bool:
    """Whether ``value`` is a pinned reference, without raising."""

    return isinstance(value, str) and IMAGE_REFERENCE_RE.fullmatch(value.strip()) is not None
