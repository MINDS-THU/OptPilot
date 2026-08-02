"""Transient launch-time environment binding for retained Method workers.

Authored ``method.runtime.envFromHost`` entries are requirements, not
reproducible Study inputs.  Their names are retained in the Method contract so
Studio can explain what a Method needs.  A non-secret binding revision records
which settings snapshot a Run selected, while the corresponding values are a
one-use in-memory handoff to a newly started Method process.

Only :class:`MethodLaunchEnvironmentDescriptor` is safe to retain.
:class:`MethodLaunchEnvironment` deliberately provides no serializer and
never exposes its values through ``repr``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .realm.run_definition import RunDefinitionManifest


_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_ENVIRONMENT_NAMES = 256
_MAX_ENVIRONMENT_VALUE_BYTES = 1024 * 1024
_MAX_BINDING_REVISION_BYTES = 512
_BINDING_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+@~-]*$")

# These values define the deterministic and isolated Python worker itself.
# Allowing authored values to replace them would make the worker contract
# ambiguous and could re-enable ambient Python package/user-site behavior.
METHOD_WORKER_RESERVED_ENVIRONMENT_NAMES = frozenset(
    {
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "PYTHONNOUSERSITE",
        "PYTHONUNBUFFERED",
    }
)


class MethodLaunchEnvironmentError(ValueError):
    """Bounded launch-environment rejection that never includes values."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        names: Sequence[str] = (),
    ) -> None:
        self.code = code
        self.names = tuple(names)
        super().__init__(message)


def normalize_method_environment_names(
    value: Any,
    *,
    label: str = "method.runtime.envFromHost",
) -> tuple[str, ...]:
    """Return one canonical ordered declaration of launch environment names."""

    if value in (None, (), []):
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MethodLaunchEnvironmentError(
            "method_environment_declaration_invalid",
            f"{label} must be a list of environment variable names.",
        )
    names: list[str] = []
    seen: set[str] = set()
    for index, raw_name in enumerate(value):
        if (
            not isinstance(raw_name, str)
            or raw_name != raw_name.strip()
            or _ENVIRONMENT_NAME_RE.fullmatch(raw_name) is None
        ):
            raise MethodLaunchEnvironmentError(
                "method_environment_declaration_invalid",
                f"{label}[{index}] is not a canonical environment variable name.",
            )
        if raw_name in METHOD_WORKER_RESERVED_ENVIRONMENT_NAMES:
            raise MethodLaunchEnvironmentError(
                "method_environment_name_reserved",
                f"{label} cannot replace OptPilot worker variable {raw_name}.",
                names=(raw_name,),
            )
        if raw_name in seen:
            raise MethodLaunchEnvironmentError(
                "method_environment_declaration_invalid",
                f"{label} must not contain duplicate names.",
                names=(raw_name,),
            )
        names.append(raw_name)
        seen.add(raw_name)
    if len(names) > _MAX_ENVIRONMENT_NAMES:
        raise MethodLaunchEnvironmentError(
            "method_environment_declaration_invalid",
            f"{label} exceeds the supported variable count.",
        )
    return tuple(names)


def method_environment_names(
    definition: RunDefinitionManifest,
) -> tuple[str, ...]:
    """Read the retained Method's launch-time environment requirements."""

    if not isinstance(definition, RunDefinitionManifest):
        raise TypeError("definition must be a RunDefinitionManifest.")
    contract = definition.method_revision.method_contract
    runtime = contract.get("runtime_requirements")
    if not isinstance(runtime, Mapping):
        raise MethodLaunchEnvironmentError(
            "method_environment_contract_invalid",
            "The retained Method runtime requirements are invalid.",
        )
    return normalize_method_environment_names(runtime.get("envFromHost"))


def _binding_revision(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MethodLaunchEnvironmentError(
            "method_environment_binding_revision_invalid",
            "Method environment binding revision must be a non-empty token.",
        )
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise MethodLaunchEnvironmentError(
            "method_environment_binding_revision_invalid",
            "Method environment binding revision must be a path-free ASCII token.",
        ) from error
    if (
        len(encoded) > _MAX_BINDING_REVISION_BYTES
        or _BINDING_REVISION_RE.fullmatch(value) is None
    ):
        raise MethodLaunchEnvironmentError(
            "method_environment_binding_revision_invalid",
            "Method environment binding revision must be a path-free ASCII token.",
        )
    return value


@dataclass(frozen=True)
class MethodLaunchEnvironmentDescriptor:
    """Names-only retained identity of one selected Method binding."""

    names: tuple[str, ...]
    binding_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "names",
            normalize_method_environment_names(
                self.names, label="Method launch environment names"
            ),
        )
        object.__setattr__(
            self, "binding_revision", _binding_revision(self.binding_revision)
        )

    @classmethod
    def for_definition(
        cls,
        definition: RunDefinitionManifest,
        *,
        binding_revision: str,
    ) -> "MethodLaunchEnvironmentDescriptor":
        return cls(
            names=method_environment_names(definition),
            binding_revision=binding_revision,
        )

    def matches(self, definition: RunDefinitionManifest) -> bool:
        return self.names == method_environment_names(definition)

    def to_dict(self) -> dict[str, Any]:
        """Return the complete, value-free durable binding description."""

        return {
            "binding_revision": self.binding_revision,
            "names": list(self.names),
        }


class MethodLaunchEnvironment:
    """One Run launch's private values for its declared Method requirements."""

    __slots__ = ("_descriptor", "_values")

    def __init__(
        self,
        names: Sequence[str],
        source: Mapping[str, str] | None,
        *,
        binding_revision: str,
    ) -> None:
        normalized = normalize_method_environment_names(
            names, label="Method launch environment names"
        )
        if source is None:
            source = {}
        if not isinstance(source, Mapping):
            raise TypeError("Method launch environment source must be a mapping.")
        missing = tuple(
            name
            for name in normalized
            if name not in source or source[name] in (None, "")
        )
        if missing:
            joined = ", ".join(missing)
            raise MethodLaunchEnvironmentError(
                "method_environment_missing",
                f"Missing Method environment variable"
                f"{'s' if len(missing) != 1 else ''}: {joined}.",
                names=missing,
            )
        selected: dict[str, str] = {}
        for name in normalized:
            value = source[name]
            if not isinstance(value, str):
                raise MethodLaunchEnvironmentError(
                    "method_environment_value_invalid",
                    f"Method environment variable {name} must be a string.",
                    names=(name,),
                )
            if "\x00" in value:
                raise MethodLaunchEnvironmentError(
                    "method_environment_value_invalid",
                    f"Method environment variable {name} contains an invalid value.",
                    names=(name,),
                )
            try:
                value_size = len(value.encode("utf-8", errors="strict"))
            except UnicodeEncodeError as error:
                raise MethodLaunchEnvironmentError(
                    "method_environment_value_invalid",
                    f"Method environment variable {name} contains an invalid value.",
                    names=(name,),
                ) from error
            if value_size > _MAX_ENVIRONMENT_VALUE_BYTES:
                raise MethodLaunchEnvironmentError(
                    "method_environment_value_invalid",
                    f"Method environment variable {name} exceeds the supported size.",
                    names=(name,),
                )
            selected[name] = value
        self._descriptor = MethodLaunchEnvironmentDescriptor(
            names=normalized,
            binding_revision=binding_revision,
        )
        self._values: dict[str, str] | None = selected

    @classmethod
    def for_definition(
        cls,
        definition: RunDefinitionManifest,
        source: Mapping[str, str] | None,
        *,
        binding_revision: str,
    ) -> "MethodLaunchEnvironment":
        """Select only values explicitly required by ``definition``."""

        return cls(
            method_environment_names(definition),
            source,
            binding_revision=binding_revision,
        )

    @property
    def names(self) -> tuple[str, ...]:
        return self._descriptor.names

    @property
    def binding_revision(self) -> str:
        return self._descriptor.binding_revision

    @property
    def descriptor(self) -> MethodLaunchEnvironmentDescriptor:
        return self._descriptor

    @property
    def values_available(self) -> bool:
        """Whether the one-use value handoff has not yet been consumed."""

        return self._values is not None

    def matches(self, definition: RunDefinitionManifest) -> bool:
        """Return whether this binding is scoped to the exact declaration."""

        return self._descriptor.matches(definition)

    def process_environment(self) -> dict[str, str]:
        """Return a detached copy without consuming the one-use binding.

        This compatibility accessor is intentionally not used by the retained
        process provider.  New launch paths should call
        :meth:`take_process_environment` so the provider cannot retain the
        credential values after start or attachment.
        """

        if self._values is None:
            raise MethodLaunchEnvironmentError(
                "method_environment_values_unavailable",
                "Method environment values are no longer available.",
                names=self.names,
            )
        return dict(self._values)

    def take_process_environment(self) -> dict[str, str]:
        """Consume and detach the values for one new physical process start."""

        values = self._values
        if values is None:
            raise MethodLaunchEnvironmentError(
                "method_environment_values_unavailable",
                "Method environment values are no longer available.",
                names=self.names,
            )
        self._values = None
        return values

    def discard_values(self) -> None:
        """Drop any unconsumed values after attaching to an existing process."""

        values = self._values
        self._values = None
        if values is not None:
            values.clear()

    def __repr__(self) -> str:
        return (
            "MethodLaunchEnvironment("
            f"names={self.names!r}, binding_revision={self.binding_revision!r})"
        )


__all__ = [
    "METHOD_WORKER_RESERVED_ENVIRONMENT_NAMES",
    "MethodLaunchEnvironment",
    "MethodLaunchEnvironmentDescriptor",
    "MethodLaunchEnvironmentError",
    "method_environment_names",
    "normalize_method_environment_names",
]
