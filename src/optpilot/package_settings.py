"""Settings that describe a package itself, rather than anything inside it.

Until now a package had no settings of its own: its name came from its folder
name and everything else was a property of the components inside it. That left
one thing with nowhere to live -- a durable identity for the package.

Why that matters. When a package is published, the record notes who may later
replace which files in it. That authority was anchored to a hash of the folder's
absolute path, so moving the folder to another directory produced a different
anchor and the package silently became a second, unrelated package with no
access to its own history. Nothing warned; the old one simply stopped being
updatable.

An identity written inside the folder travels with it. This module owns that
file. The same file is where a package-level container image will be declared,
which is the other thing that has no home today.

Reading never writes. A folder without the file keeps behaving exactly as it did
-- callers fall back to whatever they used before -- because creating files in
someone's directory as a side effect of listing a catalog would be its own kind
of surprise. The file is written when a package is published, which is already a
moment that writes to the folder.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .image_reference import ImageReference, parse_image_reference

__all__ = [
    "PACKAGE_SETTINGS_FILENAMES",
    "PACKAGE_IDENTITY_BYTES",
    "ContainerImageDeclaration",
    "PackagePaper",
    "PackageSettings",
    "resolve_component_image",
    "ensure_package_identity",
    "find_package_settings_path",
    "load_package_settings",
    "new_package_identity",
    "package_identity",
    "write_package_settings",
]

AUTHORING_API_VERSION = "optpilot.io/v1"
PACKAGE_CONFIG_KIND = "package"

#: Accepted names, most preferred first. Mirrors the resource-manifest
#: convention already used for folders that describe themselves.
PACKAGE_SETTINGS_FILENAMES = ("optpilot.package.yaml", "optpilot-package.yaml")

#: 16 bytes -> 32 hex characters. Long enough that two independently generated
#: identities will not collide; short enough to read out over a call.
PACKAGE_IDENTITY_BYTES = 16

_IDENTITY_LENGTH = PACKAGE_IDENTITY_BYTES * 2


@dataclass(frozen=True)
class ContainerImageDeclaration:
    """An image, named by fingerprint, and the architecture it is built for."""

    image: ImageReference
    platform: str


@dataclass(frozen=True)
class PackagePaper:
    """The research paper for which a public package is the companion."""

    title: str
    url: str


@dataclass(frozen=True)
class PackageSettings:
    """What a package says about itself."""

    path: Path
    identity: str
    description: Optional[str] = None
    #: Human-facing catalog name. The folder name remains the stable technical id.
    title: Optional[str] = None
    #: Catalog grouping: research, tutorial, or local.
    category: Optional[str] = None
    paper: Optional[PackagePaper] = None
    #: The image every component in this package uses unless it names its own.
    #: Absent when the package needs nothing beyond what OptPilot provides.
    container: Optional[ContainerImageDeclaration] = None

    @property
    def package_root(self) -> Path:
        return self.path.parent


def new_package_identity() -> str:
    """Generate an identity for a package that does not have one yet."""

    return secrets.token_hex(PACKAGE_IDENTITY_BYTES)


def validate_package_identity(value: Any) -> str:
    """Return ``value`` as a well-formed identity, or raise ``ValueError``."""

    # Missing is the common case and has a different cause from malformed:
    # people reach it by deleting the line after reading that they must not
    # edit it, which is the right instinct applied to the wrong case.
    if value is None:
        raise ValueError(
            "This package has no identity line. Every package needs one, "
            "including a copy of another package: add "
            f"'identity: <{_IDENTITY_LENGTH} hex characters>' to its settings "
            "file. If you copied this folder to start a separate package, use "
            "a value different from the original's, so the two do not claim to "
            "be the same package."
        )
    if not isinstance(value, str):
        raise ValueError(
            f"Package identity must be {_IDENTITY_LENGTH} lowercase hex "
            f"characters written as text, not {type(value).__name__}."
        )
    text = value.strip()
    if len(text) != _IDENTITY_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(
            f"Package identity must be {_IDENTITY_LENGTH} lowercase hex "
            f"characters; this one is {len(text)}. If you are copying a "
            "package, change every character you replace to 0-9 or a-f."
        )
    return text


def find_package_settings_path(package_root: str | Path) -> Optional[Path]:
    """Return the package's settings file, or ``None`` when it has none."""

    root = Path(package_root)
    if root.is_symlink():
        raise ValueError(f"Package root must not be a symbolic link: {root}")
    for name in PACKAGE_SETTINGS_FILENAMES:
        candidate = root / name
        if candidate.is_symlink():
            raise ValueError(
                f"Package settings must not be a symbolic link: {candidate}"
            )
        if candidate.is_file():
            return candidate
    return None


def load_package_settings(package_root: str | Path) -> Optional[PackageSettings]:
    """Read a package's own settings, or ``None`` when the file is absent.

    A malformed file raises rather than being ignored. Silently falling back to
    the old path-derived anchor would reintroduce the exact fork this file
    exists to prevent, at the moment someone is least likely to notice.
    """

    root = Path(package_root)
    if root.is_symlink():
        raise ValueError(f"Package root must not be a symbolic link: {root}")
    path = find_package_settings_path(root)
    if path is None:
        return None
    if path.is_symlink():
        raise ValueError(f"Package settings must not be a symbolic link: {path}")
    try:
        canonical_root = root.resolve(strict=True)
        canonical_path = path.resolve(strict=True)
        canonical_path.relative_to(canonical_root)
    except (OSError, ValueError) as error:
        raise ValueError(
            f"Package settings must be a regular file inside {root}: {path}"
        ) from error
    if not canonical_path.is_file():
        raise ValueError(f"Package settings must be a regular file: {path}")

    import yaml  # Imported here so this module stays usable without PyYAML.

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"{path} is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping.")
    if raw.get("apiVersion") != AUTHORING_API_VERSION:
        raise ValueError(
            f"{path} must declare apiVersion: {AUTHORING_API_VERSION}."
        )
    if raw.get("config") != PACKAGE_CONFIG_KIND:
        raise ValueError(f"{path} must declare config: {PACKAGE_CONFIG_KIND}.")
    allowed = {
        "apiVersion",
        "category",
        "config",
        "description",
        "identity",
        "paper",
        "runtime",
        "title",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(
            f"{path} has unknown keys: " + ", ".join(sorted(unknown))
        )

    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError(f"{path} description must be a string.")
    title = raw.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise ValueError(f"{path} title must be a non-empty string.")
    category = raw.get("category")
    if category is not None:
        if not isinstance(category, str) or category not in {"research", "tutorial", "local"}:
            raise ValueError(
                f"{path} category must be research, tutorial, or local."
            )
    paper = _parse_paper(raw.get("paper"), subject=str(path))

    return PackageSettings(
        path=path,
        identity=validate_package_identity(raw.get("identity")),
        description=description,
        title=title.strip() if isinstance(title, str) else None,
        category=category,
        paper=paper,
        container=_parse_container(raw.get("runtime"), subject=str(path)),
    )


def _parse_paper(value: Any, *, subject: str) -> Optional[PackagePaper]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{subject} paper must be a mapping.")
    unknown = set(value) - {"title", "url"}
    if unknown:
        raise ValueError(
            f"{subject} paper has unknown keys: " + ", ".join(sorted(unknown))
        )
    title = value.get("title")
    url = value.get("url")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"{subject} paper.title must be a non-empty string.")
    if not isinstance(url, str) or not url.startswith("https://arxiv.org/abs/"):
        raise ValueError(f"{subject} paper.url must be an arXiv abstract URL.")
    return PackagePaper(title=title.strip(), url=url.strip())


def _parse_container(
    runtime: Any, *, subject: str
) -> Optional[ContainerImageDeclaration]:
    """Read the optional package-wide image declaration."""

    if runtime is None:
        return None
    if not isinstance(runtime, Mapping):
        raise ValueError(f"{subject} runtime must be a mapping.")
    unknown_runtime = set(runtime) - {"container"}
    if unknown_runtime:
        raise ValueError(
            f"{subject} runtime has unknown keys: "
            + ", ".join(sorted(unknown_runtime))
        )
    container = runtime.get("container")
    if container is None:
        return None
    if not isinstance(container, Mapping):
        raise ValueError(f"{subject} runtime.container must be a mapping.")
    unknown = set(container) - {"image", "platform"}
    if unknown:
        raise ValueError(
            f"{subject} runtime.container has unknown keys: "
            + ", ".join(sorted(unknown))
        )
    platform = container.get("platform")
    if not isinstance(platform, str) or not platform.strip():
        # Required, because the same reference on a machine of a different
        # architecture would otherwise run different bytes under the same name.
        raise ValueError(
            f"{subject} runtime.container.platform is required, for example "
            "linux/amd64."
        )
    return ContainerImageDeclaration(
        image=parse_image_reference(
            container.get("image"), subject=f"{subject} runtime.container.image"
        ),
        platform=platform.strip(),
    )


def resolve_component_image(
    component_container: Optional[ContainerImageDeclaration],
    package_settings: Optional[PackageSettings],
) -> Optional[ContainerImageDeclaration]:
    """Which image a component runs in: its own, else its package's, else none.

    Returning ``None`` means the component named nothing and neither did its
    package, so it runs in the image OptPilot provides by default. Resolution
    stops at the first declaration found -- a component that names an image does
    not inherit anything from the package.
    """

    if component_container is not None:
        return component_container
    if package_settings is not None:
        return package_settings.container
    return None


def package_identity(package_root: str | Path) -> Optional[str]:
    """The package's durable identity, or ``None`` when it has not got one."""

    settings = load_package_settings(package_root)
    return None if settings is None else settings.identity


def write_package_settings(
    package_root: str | Path,
    *,
    identity: str,
    description: Optional[str] = None,
    title: Optional[str] = None,
    category: Optional[str] = None,
    paper: Optional[PackagePaper] = None,
    container: Optional[ContainerImageDeclaration] = None,
) -> Path:
    """Write the settings file, creating the package folder if needed.

    ``container`` is written out when given. A caller rewriting an existing file
    must pass the declaration it read, or the package would silently lose the
    image its components run in.
    """

    identity = validate_package_identity(identity)
    root = Path(package_root)
    root.mkdir(parents=True, exist_ok=True)
    path = find_package_settings_path(root) or root / PACKAGE_SETTINGS_FILENAMES[0]

    lines = [
        f"apiVersion: {AUTHORING_API_VERSION}",
        f"config: {PACKAGE_CONFIG_KIND}",
        "# Identifies this package, so moving or renaming its folder still",
        "# updates this package rather than creating a second one. Keep it as",
        "# it is when moving or renaming.",
        "# Copying the folder to start a SEPARATE package is the one case where",
        "# you change it: replace the value below with 32 different hex",
        "# characters, so the copy is its own package with its own history.",
        "# Do not delete the line -- a package without this cannot be read.",
        f"identity: {identity}",
    ]
    if description:
        lines.append(f"description: {_yaml_scalar(description)}")
    if title:
        lines.append(f"title: {_yaml_scalar(title)}")
    if category:
        if category not in {"research", "tutorial", "local"}:
            raise ValueError("Package category must be research, tutorial, or local.")
        lines.append(f"category: {category}")
    if paper is not None:
        lines.extend(
            [
                "paper:",
                f"  title: {_yaml_scalar(paper.title)}",
                f"  url: {_yaml_scalar(paper.url)}",
            ]
        )
    if container is not None:
        lines.extend(
            [
                "runtime:",
                "  container:",
                f"    image: {container.image.raw}",
                f"    platform: {container.platform}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def ensure_package_identity(
    package_root: str | Path,
    *,
    description: Optional[str] = None,
) -> str:
    """Return the package's identity, giving it one if it has none.

    Writes only when the file is absent, so calling it repeatedly is safe and an
    existing identity is never replaced.
    """

    existing = load_package_settings(package_root)
    if existing is not None:
        return existing.identity
    identity = new_package_identity()
    write_package_settings(package_root, identity=identity, description=description)
    return identity


def _yaml_scalar(value: str) -> str:
    """Quote a scalar when plain style would not round-trip."""

    if value != value.strip() or not value:
        return repr(value)
    if any(character in value for character in ":#\n\"'{}[]&*!|>%@`,"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def package_settings_payload(settings: PackageSettings) -> Dict[str, Any]:
    """A plain mapping for callers that report package settings."""

    payload: Dict[str, Any] = {"identity": settings.identity}
    if settings.description is not None:
        payload["description"] = settings.description
    if settings.title is not None:
        payload["title"] = settings.title
    if settings.category is not None:
        payload["category"] = settings.category
    if settings.paper is not None:
        payload["paper"] = {
            "title": settings.paper.title,
            "url": settings.paper.url,
        }
    return payload
