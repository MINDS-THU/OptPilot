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
from typing import Any, Dict, Optional

__all__ = [
    "PACKAGE_SETTINGS_FILENAMES",
    "PACKAGE_IDENTITY_BYTES",
    "PackageSettings",
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
class PackageSettings:
    """What a package says about itself."""

    path: Path
    identity: str
    description: Optional[str] = None

    @property
    def package_root(self) -> Path:
        return self.path.parent


def new_package_identity() -> str:
    """Generate an identity for a package that does not have one yet."""

    return secrets.token_hex(PACKAGE_IDENTITY_BYTES)


def validate_package_identity(value: Any) -> str:
    """Return ``value`` as a well-formed identity, or raise ``ValueError``."""

    if not isinstance(value, str):
        raise ValueError("Package identity must be a string.")
    text = value.strip()
    if len(text) != _IDENTITY_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(
            f"Package identity must be {_IDENTITY_LENGTH} lowercase hex characters."
        )
    return text


def find_package_settings_path(package_root: str | Path) -> Optional[Path]:
    """Return the package's settings file, or ``None`` when it has none."""

    root = Path(package_root)
    for name in PACKAGE_SETTINGS_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def load_package_settings(package_root: str | Path) -> Optional[PackageSettings]:
    """Read a package's own settings, or ``None`` when the file is absent.

    A malformed file raises rather than being ignored. Silently falling back to
    the old path-derived anchor would reintroduce the exact fork this file
    exists to prevent, at the moment someone is least likely to notice.
    """

    path = find_package_settings_path(package_root)
    if path is None:
        return None

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

    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError(f"{path} description must be a string.")

    return PackageSettings(
        path=path,
        identity=validate_package_identity(raw.get("identity")),
        description=description,
    )


def package_identity(package_root: str | Path) -> Optional[str]:
    """The package's durable identity, or ``None`` when it has not got one."""

    settings = load_package_settings(package_root)
    return None if settings is None else settings.identity


def write_package_settings(
    package_root: str | Path,
    *,
    identity: str,
    description: Optional[str] = None,
) -> Path:
    """Write the settings file, creating the package folder if needed."""

    identity = validate_package_identity(identity)
    root = Path(package_root)
    root.mkdir(parents=True, exist_ok=True)
    path = find_package_settings_path(root) or root / PACKAGE_SETTINGS_FILENAMES[0]

    lines = [
        f"apiVersion: {AUTHORING_API_VERSION}",
        f"config: {PACKAGE_CONFIG_KIND}",
        "# Identifies this package wherever its folder is moved to. Do not edit:",
        "# changing it detaches the package from its own published history.",
        f"identity: {identity}",
    ]
    if description:
        lines.append(f"description: {_yaml_scalar(description)}")
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
    return payload
