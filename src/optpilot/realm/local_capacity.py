"""Conservative capacity limits for the current local process host.

The helper reports only resources the process can reasonably address.  It
takes the minimum of logical CPU, affinity, and cgroup constraints, and the
minimum of physical memory and cgroup constraints, then leaves explicit
headroom for the OS and OptPilot control plane.  Unknown capacity is an error:
inventing a fallback would silently overclaim resources that may not exist.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional


_CPU_HEADROOM_NUMERATOR = 4
_CPU_HEADROOM_DENOMINATOR = 5
_MEMORY_HEADROOM_NUMERATOR = 3
_MEMORY_HEADROOM_DENOMINATOR = 4
_MIB = 1024 * 1024


def conservative_local_host_capacity_limits() -> Mapping[str, int]:
    """Discover stable conservative CPU/memory limits and advertise zero GPUs.

    ``cpu_millis`` can be fractional relative to a logical CPU when a cgroup
    quota is fractional.  Memory is rounded down to MiB after headroom.  GPUs
    remain zero until a provider-specific discovery and isolation contract can
    prove that they are actually available.
    """

    cpu_candidates = []
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        try:
            process_count = process_cpu_count()
        except OSError:
            process_count = None
        if isinstance(process_count, int) and process_count > 0:
            cpu_candidates.append(process_count * 1000)
    logical_count = os.cpu_count()
    if isinstance(logical_count, int) and logical_count > 0:
        cpu_candidates.append(logical_count * 1000)
    affinity = _affinity_cpu_millis()
    if affinity is not None:
        cpu_candidates.append(affinity)
    quota = _cgroup_cpu_quota_millis()
    if quota is not None:
        cpu_candidates.append(quota)
    if not cpu_candidates:
        raise RuntimeError("Could not safely discover local CPU capacity.")
    discovered_cpu = min(cpu_candidates)
    cpu_millis = (
        discovered_cpu * _CPU_HEADROOM_NUMERATOR // _CPU_HEADROOM_DENOMINATOR
    )
    if cpu_millis <= 0:
        raise RuntimeError("Discovered local CPU capacity is too small to reserve.")

    memory_candidates = []
    physical_memory = _physical_memory_bytes()
    if physical_memory is not None:
        memory_candidates.append(physical_memory)
    cgroup_memory = _cgroup_memory_limit_bytes()
    if cgroup_memory is not None:
        memory_candidates.append(cgroup_memory)
    if not memory_candidates:
        raise RuntimeError("Could not safely discover local memory capacity.")
    discovered_memory = min(memory_candidates)
    memory_bytes = (
        discovered_memory
        * _MEMORY_HEADROOM_NUMERATOR
        // _MEMORY_HEADROOM_DENOMINATOR
    )
    memory_bytes = memory_bytes // _MIB * _MIB
    if memory_bytes <= 0:
        raise RuntimeError("Discovered local memory capacity is too small to reserve.")

    return MappingProxyType(
        {
            "cpu_millis": cpu_millis,
            "gpu_count": 0,
            "memory_bytes": memory_bytes,
        }
    )


def _affinity_cpu_millis() -> Optional[int]:
    get_affinity = getattr(os, "sched_getaffinity", None)
    if get_affinity is None:
        return None
    try:
        count = len(get_affinity(0))
    except (OSError, TypeError, ValueError):
        return None
    return count * 1000 if count > 0 else None


def _physical_memory_bytes() -> Optional[int]:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, TypeError, ValueError):
        pages = page_size = None
    if (
        isinstance(pages, int)
        and not isinstance(pages, bool)
        and pages > 0
        and isinstance(page_size, int)
        and not isinstance(page_size, bool)
        and page_size > 0
    ):
        return pages * page_size
    if os.name != "nt":
        return None

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    try:
        succeeded = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return None
    return int(status.ullTotalPhys) if succeeded and status.ullTotalPhys > 0 else None


def _cgroup_cpu_quota_millis() -> Optional[int]:
    candidates = []
    # cgroup v2: ``max`` means unconstrained.  Inspect the process cgroup and
    # every ancestor because a parent quota constrains descendants too.
    for path in _cgroup_v2_constraint_paths("cpu.max"):
        cpu_max = _read_small_text(path)
        if cpu_max is None:
            continue
        parts = cpu_max.split()
        if len(parts) == 2 and parts[0] != "max":
            quota = _positive_decimal(parts[0])
            period = _positive_decimal(parts[1])
            if quota is not None and period is not None:
                candidates.append(max(1, quota * 1000 // period))

    # cgroup v1 has separate quota and period files at each constrained level.
    for directory in _cgroup_v1_constraint_directories("cpu"):
        quota = _positive_decimal(
            _read_small_text(directory / "cpu.cfs_quota_us")
        )
        period = _positive_decimal(
            _read_small_text(directory / "cpu.cfs_period_us")
        )
        if quota is not None and period is not None:
            candidates.append(max(1, quota * 1000 // period))
    return min(candidates) if candidates else None


def _cgroup_memory_limit_bytes() -> Optional[int]:
    candidates = []
    for path in _cgroup_v2_constraint_paths("memory.max"):
        value = _read_small_text(path)
        if value is None or value == "max":
            continue
        limit = _positive_decimal(value)
        if limit is not None:
            candidates.append(limit)
    for directory in _cgroup_v1_constraint_directories("memory"):
        limit = _positive_decimal(
            _read_small_text(directory / "memory.limit_in_bytes")
        )
        # Some v1 runtimes encode "unlimited" as a huge page-aligned integer.
        if limit is not None and limit < (1 << 63):
            candidates.append(limit)
    return min(candidates) if candidates else None


def _cgroup_v2_constraint_paths(filename: str) -> tuple[Path, ...]:
    root = Path("/sys/fs/cgroup")
    relative = _current_cgroup_relative_path(controller=None)
    directories = _constraint_ancestors(root, relative)
    return tuple(directory / filename for directory in directories)


def _cgroup_v1_constraint_directories(controller: str) -> tuple[Path, ...]:
    relative = _current_cgroup_relative_path(controller=controller)
    roots = {
        Path("/sys/fs/cgroup") / controller,
        Path("/sys/fs/cgroup") / f"{controller},cpuacct"
        if controller == "cpu"
        else Path("/sys/fs/cgroup") / controller,
        Path("/sys/fs/cgroup") / f"cpuacct,{controller}"
        if controller == "cpu"
        else Path("/sys/fs/cgroup") / controller,
    }
    result = []
    for root in sorted(roots, key=str):
        result.extend(_constraint_ancestors(root, relative))
    return tuple(dict.fromkeys(result))


def _current_cgroup_relative_path(*, controller: Optional[str]) -> Optional[Path]:
    value = _read_bounded_text(Path("/proc/self/cgroup"), max_bytes=64 * 1024)
    if value is None:
        return None
    for line in value.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        controllers = set(filter(None, parts[1].split(",")))
        if (controller is None and parts[0] == "0" and not controllers) or (
            controller is not None and controller in controllers
        ):
            raw = parts[2].lstrip("/")
            path = Path(raw) if raw else Path()
            if any(part in {"", ".", ".."} for part in path.parts):
                return None
            return path
    return None


def _constraint_ancestors(root: Path, relative: Optional[Path]) -> tuple[Path, ...]:
    if relative is None:
        return (root,)
    current = root / relative
    result = []
    while True:
        result.append(current)
        if current == root:
            break
        parent = current.parent
        if root not in parent.parents and parent != root:
            return (root,)
        current = parent
    return tuple(result)


def _read_small_text(path: Path) -> Optional[str]:
    return _read_bounded_text(path, max_bytes=128)


def _read_bounded_text(path: Path, *, max_bytes: int) -> Optional[str]:
    try:
        raw = path.read_bytes()
    except (OSError, UnicodeError):
        return None
    if not raw or len(raw) > max_bytes:
        return None
    try:
        value = raw.decode("ascii").strip()
    except UnicodeError:
        return None
    return value if value else None


def _positive_decimal(value: Optional[str]) -> Optional[int]:
    if value is None or not value.isdecimal():
        return None
    result = int(value)
    return result if result > 0 else None


__all__ = ["conservative_local_host_capacity_limits"]
