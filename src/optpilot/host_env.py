"""How a component says which environment values come from the person's machine.

A declaration used to be a plain list of names, and every name was required.
That made a model id indistinguishable from an API key: both had to be set
before anything would run, even though the package knows a perfectly good model
id and only the person knows their key. Installing OptPilot therefore meant
being asked for several values whose meaning was not explained anywhere, which
is where first sessions stalled.

An entry may now carry a default::

    envFromHost:
      - name: DEVS_INTERFACE_MODEL_ID
        default: openrouter/openai/gpt-5.4
      - OPTPILOT_STRICT_MODE          # still required, as before

A bare name behaves exactly as it always did. A default is used only when the
host supplies nothing, so setting the value in Studio Settings or exporting it
still wins.

Secrets are deliberately excluded. ``secretsFromHost`` remains a plain list of
names, because a default secret is either useless or a credential committed
into a settings file, and both are worse than being asked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Tuple

__all__ = [
    "HostEnvDeclaration",
    "compile_host_env_declarations",
    "host_env_defaults",
    "host_env_names",
    "host_env_required_names",
]


@dataclass(frozen=True)
class HostEnvDeclaration:
    """One environment value a component wants from the host."""

    name: str
    default: str | None = None
    description: str = ""

    @property
    def required(self) -> bool:
        """Whether the host must supply this, having no default to fall back on."""

        return self.default is None


def compile_host_env_declarations(
    payload: Any, *, location: str = "grants.envFromHost"
) -> Tuple[HostEnvDeclaration, ...]:
    """Read a declaration in either form, rejecting anything malformed.

    Accepts the historical list of names and the richer entries beside them, so
    a package written before defaults existed keeps working untouched.
    """

    if payload is None:
        return ()
    if not isinstance(payload, (list, tuple)):
        raise ValueError(f"{location} must be a list.")
    declarations: List[HostEnvDeclaration] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(payload):
        where = f"{location}[{index}]"
        if isinstance(item, str):
            name, default, description = item.strip(), None, ""
        elif isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
            if "default" not in item:
                raise ValueError(
                    f"{where} names {name!r} without a default. Use the plain "
                    "name to require it from the host, or add a default."
                )
            raw_default = item.get("default")
            if not isinstance(raw_default, str):
                raise ValueError(f"{where}.default must be a string.")
            default = raw_default
            description = str(item.get("description") or "")
        else:
            raise ValueError(f"{where} must be a name or a name with a default.")
        if not name:
            raise ValueError(f"{where} must name an environment variable.")
        if name in seen:
            raise ValueError(
                f"{where} repeats {name!r}, already declared at "
                f"{location}[{seen[name]}]."
            )
        seen[name] = index
        declarations.append(
            HostEnvDeclaration(name=name, default=default, description=description)
        )
    return tuple(declarations)


def host_env_names(payload: Any, *, location: str = "grants.envFromHost") -> List[str]:
    """Every declared name, in order -- what most callers still want."""

    return [item.name for item in compile_host_env_declarations(payload, location=location)]


def host_env_required_names(
    payload: Any, *, location: str = "grants.envFromHost"
) -> List[str]:
    """Only the names the host must supply, having no default."""

    return [
        item.name
        for item in compile_host_env_declarations(payload, location=location)
        if item.required
    ]


def host_env_defaults(
    payload: Any, *, location: str = "grants.envFromHost"
) -> Dict[str, str]:
    """The fallback value for each name that declares one."""

    return {
        item.name: item.default
        for item in compile_host_env_declarations(payload, location=location)
        if item.default is not None
    }
