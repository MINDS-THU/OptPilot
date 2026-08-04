"""Process-lifetime ownership for Studio's shared runtime namespace."""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Optional

try:  # Studio runtime supervision currently requires POSIX advisory locks.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fail-closed boundary
    fcntl = None  # type: ignore[assignment]


class StudioRuntimeSupervisorBusy(RuntimeError):
    """Another live Studio process owns this project's runtime namespace."""


_PROCESS_LIFETIME_CLAIMS: set["StudioRuntimeSupervisorClaim"] = set()
_PROCESS_LIFETIME_CLAIMS_LOCK = threading.Lock()


class StudioRuntimeSupervisorClaim:
    """An exclusive, crash-released claim held for one Studio process."""

    def __init__(
        self,
        *,
        studio_root: Path,
        path: Path,
        descriptor: int,
        device: int,
        inode: int,
        compatibility_claim: Optional["StudioRuntimeSupervisorClaim"] = None,
    ) -> None:
        self.studio_root = studio_root
        self.path = path
        self._descriptor: Optional[int] = descriptor
        self._device = int(device)
        self._inode = int(inode)
        self._compatibility_claim = compatibility_claim

    @classmethod
    def acquire(
        cls,
        studio_root: Path,
        *,
        control_root: Optional[Path] = None,
    ) -> "StudioRuntimeSupervisorClaim":
        if fcntl is None:
            raise RuntimeError(
                "OptPilot Studio runtime supervision requires POSIX file locks."
            )
        root = Path(os.path.abspath(studio_root.expanduser())).resolve()
        legacy_control_root = root / ".optpilot-ui"
        control_root = (
            legacy_control_root
            if control_root is None
            else Path(os.path.abspath(control_root.expanduser()))
        )
        # During the transition from checkout-local to OS-local control state,
        # also hold the legacy claim.  This prevents a pre-upgrade Studio from
        # remaining live while a new process migrates the coordination store
        # and starts serving from a separate authority.
        compatibility_claim = (
            cls.acquire(root)
            if control_root != legacy_control_root
            else None
        )
        try:
            control_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            control_status = control_root.lstat()
            if control_root.is_symlink() or not stat.S_ISDIR(control_status.st_mode):
                raise RuntimeError(
                    "OptPilot Studio control storage must be a real directory."
                )
            path = control_root / "runtime-supervisor.lock"
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
        except BaseException:
            if compatibility_claim is not None:
                compatibility_claim.close()
            raise
        try:
            os.set_inheritable(descriptor, False)
            opened = os.fstat(descriptor)
            visible = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(visible.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != visible.st_dev
                or opened.st_ino != visible.st_ino
            ):
                raise RuntimeError(
                    "OptPilot Studio runtime-supervisor claim was replaced."
                )
            getuid = getattr(os, "getuid", None)
            if getuid is not None and opened.st_uid != getuid():
                raise RuntimeError(
                    "OptPilot Studio runtime-supervisor claim has another owner."
                )
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {
                    errno.EACCES,
                    errno.EAGAIN,
                    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
                }:
                    raise StudioRuntimeSupervisorBusy(
                        "Another OptPilot Studio process is already supervising "
                        "this project's workspace runtimes. Stop it before "
                        "starting another Studio here."
                    ) from None
                raise
            # Opening and inspecting the path before flock is not enough: it
            # may have been replaced while this process waited for ownership.
            # Validate the visible coordinate again while the descriptor lock
            # is held so a replaced path can never become recovery authority.
            opened = os.fstat(descriptor)
            visible = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(visible.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != visible.st_dev
                or opened.st_ino != visible.st_ino
            ):
                raise RuntimeError(
                    "OptPilot Studio runtime-supervisor claim was replaced."
                )
            diagnostic = json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at": time.time(),
                    "studio_root": str(root),
                },
                sort_keys=True,
            ).encode("utf-8")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            remaining = memoryview(diagnostic + b"\n")
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError(
                        "Could not persist the Studio runtime-supervisor diagnostic."
                    )
                remaining = remaining[written:]
            os.fsync(descriptor)
            return cls(
                studio_root=root,
                path=path,
                descriptor=descriptor,
                device=opened.st_dev,
                inode=opened.st_ino,
                compatibility_claim=compatibility_claim,
            )
        except BaseException:
            os.close(descriptor)
            if compatibility_claim is not None:
                compatibility_claim.close()
            raise

    def assert_active_for(self, studio_root: Path) -> None:
        root = Path(os.path.abspath(studio_root.expanduser())).resolve()
        descriptor = self._descriptor
        if descriptor is None or root != self.studio_root:
            raise RuntimeError(
                "A live matching Studio runtime-supervisor claim is required."
            )
        opened = os.fstat(descriptor)
        visible = self.path.lstat()
        if (
            opened.st_dev != self._device
            or opened.st_ino != self._inode
            or visible.st_dev != self._device
            or visible.st_ino != self._inode
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(visible.st_mode)
        ):
            raise RuntimeError(
                "The Studio runtime-supervisor claim is no longer attached."
            )
        compatibility_claim = self._compatibility_claim
        if compatibility_claim is not None:
            compatibility_claim.assert_active_for(root)

    def retain_until_process_exit(self) -> None:
        """Keep this live claim strongly reachable until explicit close or exit."""

        with _PROCESS_LIFETIME_CLAIMS_LOCK:
            self.assert_active_for(self.studio_root)
            _PROCESS_LIFETIME_CLAIMS.add(self)

    def close(self) -> None:
        with _PROCESS_LIFETIME_CLAIMS_LOCK:
            _PROCESS_LIFETIME_CLAIMS.discard(self)
            descriptor = self._descriptor
            if descriptor is None:
                return
            self._descriptor = None
            compatibility_claim = self._compatibility_claim
            self._compatibility_claim = None
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            try:
                os.close(descriptor)
            finally:
                if compatibility_claim is not None:
                    compatibility_claim.close()
