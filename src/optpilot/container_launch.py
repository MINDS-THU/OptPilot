"""Building the exact command that starts a container.

A package's code is not necessarily code the person running it wrote or read.
Some methods have a language model write Python while the run is in progress and
then execute it, so what a container is allowed to reach is a real boundary
rather than a formality. Everything that boundary depends on is decided here, in
one pure function, so it can be read in one place and tested without starting
anything.

What is granted, and nothing else:

  four mounts, three of them read-only (§7 of the design)
  no network unless the component asked for it
  a processor share, a memory cap, a limit on processes
  no new privileges, and every capability dropped
  a writable temporary directory, since the filesystem itself is read-only

Two details that are easy to get wrong and matter:

Credentials are passed by file, never on the command line. Anything on a command
line is readable by every other program on the machine for as long as the process
lives, and a method's exchange can last many minutes.

The import path is written last. Both container programs take the last value when
an option repeats, so a component that declared its own environment variables
could otherwise redirect where its code is loaded from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

__all__ = [
    "ContainerLimits",
    "ContainerMount",
    "LaunchSpec",
    "RESERVED_ENVIRONMENT_NAMES",
    "build_container_command",
]

#: Names a component may not set, because each one redirects what is loaded or
#: executed rather than configuring the code that runs.
RESERVED_ENVIRONMENT_NAMES = frozenset(
    {
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
    }
)

#: Applied unless a component raises them, and a raised limit is shown when the
#: image is approved, because raising one is part of what is being agreed to.
DEFAULT_CPUS = "2"
DEFAULT_MEMORY = "4g"
DEFAULT_PIDS = 512


@dataclass(frozen=True)
class ContainerLimits:
    cpus: str = DEFAULT_CPUS
    memory: str = DEFAULT_MEMORY
    pids: int = DEFAULT_PIDS


@dataclass(frozen=True)
class ContainerMount:
    """One path made visible inside the container."""

    source: Path
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class LaunchSpec:
    """Everything needed to start one container."""

    image: str
    platform: str
    name: str
    command: Sequence[str]
    mounts: Sequence[ContainerMount] = ()
    workdir: Optional[str] = None
    network: bool = False
    env: Mapping[str, str] = field(default_factory=dict)
    env_file: Optional[Path] = None
    import_paths: Sequence[str] = ()
    limits: ContainerLimits = ContainerLimits()
    interactive: bool = False


def build_container_command(engine: str, spec: LaunchSpec) -> list[str]:
    """The full command to start one container, and nothing implicit.

    ``engine`` is a resolved path found by the operator's configuration or the
    search path -- never a value a package supplied.
    """

    if not spec.image:
        raise ValueError("A container launch needs an image.")
    if not spec.name:
        raise ValueError("A container launch needs a name, so it can be found again.")
    if not spec.command:
        raise ValueError("A container launch needs a command.")

    forbidden = sorted(set(spec.env) & RESERVED_ENVIRONMENT_NAMES)
    if forbidden:
        raise ValueError(
            "A component may not set "
            + ", ".join(forbidden)
            + ": these redirect what is loaded or executed rather than "
            "configuring it."
        )

    argv: list[str] = [engine, "run", "--rm"]
    if spec.interactive:
        # Keeps the input stream open for an exchange that stays open.
        argv.append("--interactive")
    argv += ["--name", spec.name]
    argv += ["--platform", spec.platform]

    # Nothing implicit: the image must already be here, and is never fetched
    # during a run because what a fetch returns can differ between runs.
    argv += ["--pull", "never"]

    argv += ["--network", "bridge" if spec.network else "none"]

    argv += ["--cpus", str(spec.limits.cpus)]
    argv += ["--memory", str(spec.limits.memory)]
    argv += ["--pids-limit", str(spec.limits.pids)]

    argv += ["--cap-drop", "ALL"]
    argv += ["--security-opt", "no-new-privileges"]

    # The filesystem is read-only, so a writable temporary directory has to be
    # supplied or ordinary library code that writes one file will fail.
    argv += ["--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev"]

    for mount in spec.mounts:
        suffix = ":ro" if mount.read_only else ":rw"
        argv += ["--volume", f"{Path(mount.source).resolve()}:{mount.target}{suffix}"]

    if spec.workdir:
        argv += ["--workdir", spec.workdir]

    if spec.env_file is not None:
        # By file, never on the command line: a command line is readable by any
        # other program on the machine for as long as the process lives.
        argv += ["--env-file", str(Path(spec.env_file).resolve())]

    for name in sorted(spec.env):
        argv += ["--env", f"{name}={spec.env[name]}"]

    if spec.import_paths:
        # Last, so a declared variable cannot redirect where code is loaded from:
        # both container programs take the last value when an option repeats.
        argv += ["--env", "PYTHONPATH=" + ":".join(spec.import_paths)]

    argv.append(spec.image)
    argv.extend(spec.command)
    return argv
