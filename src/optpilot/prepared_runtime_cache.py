"""Provider-scoped prepared-runtime cache for reusable local runtimes.

The cache deliberately stores only prepared output. Authored source and each
consumer's writable output remain independently owned.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


PREPARED_RUNTIME_CACHE_SCHEMA = "optpilot.prepared-runtime-cache.v3"
PREPARED_RUNTIME_KEY_SCHEMA = "optpilot.prepared-runtime-key.v3"
PREPARED_RUNTIME_LEASE_SCHEMA = "optpilot.prepared-runtime-lease.v3"
PREPARED_RUNTIME_LAYOUT_VERSION = "provider-scoped-prepared-output-v3"
PREPARED_RUNTIME_TREE_DIGEST = "sha256-tree-v1"
DEFAULT_MAX_CACHE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_lower_hex_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class PreparedRuntimeLease:
    """One live launch's reference to an immutable prepared cache entry."""

    cache_key: str
    launch_id: str
    entry_root: Path
    payload_root: Path
    lease_path: Path
    cache_status: str
    manifest: Mapping[str, Any]
    lease_id: str = ""

    def public_summary(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "status": self.cache_status,
            "key": self.cache_key,
            "scope": "exact-source-provider-path",
            "readOnlyAtRuntime": True,
        }


class PreparedRuntimeCache:
    """Own content-addressed, single-flight prepared runtime entries."""

    def __init__(
        self,
        root: Path,
        *,
        max_entries: int = 16,
        max_bytes: int | None = DEFAULT_MAX_CACHE_BYTES,
    ) -> None:
        candidate = Path(os.path.abspath(root.expanduser()))
        self._ensure_directory(candidate)
        self.root = candidate.resolve()
        self.entries_root = self.root / "entries"
        self.locks_root = self.root / "locks"
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = (
            None if max_bytes is None else max(1, int(max_bytes))
        )
        self._owned_lease_descriptors: dict[Path, tuple[int, str]] = {}
        self._owned_lease_lock = threading.RLock()
        self._last_prune_report: dict[str, Any] = {}
        self._last_recovery_report: dict[str, Any] = {}
        self._ensure_directory(self.entries_root)
        self._ensure_directory(self.locks_root)
        self._last_recovery_report = self.recover()

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        linked = path.lstat()
        if not stat.S_ISDIR(linked.st_mode) or path.is_symlink():
            raise ValueError(f"Prepared-runtime cache path must be a real directory: {path}")
        path.chmod(0o700)

    def key_payload(
        self,
        *,
        source_identity: Mapping[str, Any],
        setup: Mapping[str, Any],
        provider_identity: Mapping[str, Any],
        component_identity: Mapping[str, Any],
    ) -> dict[str, Any]:
        provider = dict(provider_identity)
        image_digest = str(provider.get("imageDigest") or "")
        explicit_interpreter = provider.get("interpreter")
        interpreter = (
            dict(explicit_interpreter)
            if isinstance(explicit_interpreter, Mapping)
            else {
                "kind": str(provider.get("interpreterKind") or "provider-image"),
                "abi": str(
                    provider.get("interpreterAbi")
                    or provider.get("pythonAbi")
                    or "captured-by-immutable-provider"
                ),
                "providerImageDigest": image_digest,
            }
        )
        platform = {
            "os": str(provider.get("os") or "unspecified"),
            "architecture": str(
                provider.get("architecture") or provider.get("arch") or "unspecified"
            ),
            "variant": str(provider.get("variant") or ""),
            "libc": str(provider.get("libc") or ""),
        }
        return {
            "schema": PREPARED_RUNTIME_KEY_SCHEMA,
            "layout_version": PREPARED_RUNTIME_LAYOUT_VERSION,
            "cache_format": {
                "manifest_schema": PREPARED_RUNTIME_CACHE_SCHEMA,
                "tree_digest": PREPARED_RUNTIME_TREE_DIGEST,
            },
            "cache_root": str(self.root),
            "source": dict(source_identity),
            "component": dict(component_identity),
            "setup": dict(setup),
            "provider": provider,
            "interpreter": interpreter,
            "platform": platform,
        }

    def cache_key(self, key_payload: Mapping[str, Any]) -> str:
        if key_payload.get("schema") != PREPARED_RUNTIME_KEY_SCHEMA:
            raise ValueError("Prepared-runtime key payload has an invalid schema.")
        return _canonical_digest(key_payload)

    def entry_root(self, cache_key: str) -> Path:
        if not _is_lower_hex_digest(cache_key):
            raise ValueError("Prepared-runtime cache key is invalid.")
        return self.entries_root / cache_key

    @contextmanager
    def _file_lock(self, lock_path: Path) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(
            lock_path,
            flags,
            0o600,
        )
        try:
            linked = os.fstat(descriptor)
            if not stat.S_ISREG(linked.st_mode):
                raise RuntimeError(
                    f"Prepared-runtime lock must be a regular file: {lock_path}"
                )
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @contextmanager
    def _key_lock(self, cache_key: str) -> Iterator[None]:
        if not _is_lower_hex_digest(cache_key):
            raise ValueError("Prepared-runtime cache key is invalid.")
        with self._file_lock(self.locks_root / f"{cache_key}.lock"):
            yield

    @contextmanager
    def _cache_lock(self) -> Iterator[None]:
        with self._file_lock(self.locks_root / "cache.lock"):
            yield

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(
                path.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _remove_entry(path: Path) -> None:
        try:
            linked = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
            path.unlink()
            return
        path.chmod(0o700)
        for directory, directory_names, _file_names in os.walk(
            path, topdown=True, followlinks=False
        ):
            current = Path(directory)
            if not current.is_symlink():
                current.chmod(0o700)
            for name in directory_names:
                child = current / name
                if not child.is_symlink():
                    child.chmod(0o700)
        shutil.rmtree(path)

    @staticmethod
    def _seal_payload(payload_root: Path) -> None:
        linked = payload_root.lstat()
        if not stat.S_ISDIR(linked.st_mode) or payload_root.is_symlink():
            raise RuntimeError("Prepared-runtime payload must be a real directory.")
        for directory, directory_names, file_names in os.walk(
            payload_root, topdown=False, followlinks=False
        ):
            current = Path(directory)
            for name in file_names:
                path = current / name
                item = path.lstat()
                if stat.S_ISLNK(item.st_mode):
                    continue
                path.chmod(stat.S_IMODE(item.st_mode) & ~0o222)
            for name in directory_names:
                path = current / name
                item = path.lstat()
                if stat.S_ISLNK(item.st_mode):
                    continue
                path.chmod(stat.S_IMODE(item.st_mode) & ~0o222)
        payload_root.chmod(stat.S_IMODE(linked.st_mode) & ~0o222)

    @staticmethod
    def _digest_field(hasher: Any, value: bytes) -> None:
        hasher.update(len(value).to_bytes(8, "big"))
        hasher.update(value)

    @classmethod
    def _payload_integrity(
        cls,
        payload_root: Path,
        *,
        require_sealed: bool,
    ) -> dict[str, Any]:
        root_info = payload_root.lstat()
        if payload_root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
            raise RuntimeError("Prepared-runtime payload must be a real directory.")
        hasher = hashlib.sha256()
        logical_bytes = 0
        file_count = 0
        directory_count = 0
        symlink_count = 0
        pending = [(payload_root, Path("."))]
        while pending:
            directory, relative_directory = pending.pop()
            linked = directory.lstat()
            if directory.is_symlink() or not stat.S_ISDIR(linked.st_mode):
                raise RuntimeError("Prepared-runtime payload directory changed during scan.")
            if require_sealed and stat.S_IMODE(linked.st_mode) & 0o222:
                raise RuntimeError("Prepared-runtime payload contains a writable directory.")
            directory_count += 1
            cls._digest_field(hasher, b"directory")
            cls._digest_field(hasher, os.fsencode(relative_directory.as_posix()))
            cls._digest_field(
                hasher, str(stat.S_IMODE(linked.st_mode)).encode("ascii")
            )
            children = sorted(
                directory.iterdir(), key=lambda child: os.fsencode(child.name)
            )
            child_directories: list[tuple[Path, Path]] = []
            for child in children:
                child_relative = relative_directory / child.name
                child_info = child.lstat()
                cls._digest_field(hasher, os.fsencode(child_relative.as_posix()))
                cls._digest_field(
                    hasher,
                    str(stat.S_IMODE(child_info.st_mode)).encode("ascii"),
                )
                if stat.S_ISLNK(child_info.st_mode):
                    symlink_count += 1
                    cls._digest_field(hasher, b"symlink")
                    cls._digest_field(hasher, os.fsencode(os.readlink(child)))
                    continue
                if stat.S_ISDIR(child_info.st_mode):
                    child_directories.append((child, child_relative))
                    continue
                if not stat.S_ISREG(child_info.st_mode):
                    raise RuntimeError(
                        "Prepared-runtime payload contains an unsupported special file."
                    )
                if require_sealed and stat.S_IMODE(child_info.st_mode) & 0o222:
                    raise RuntimeError(
                        "Prepared-runtime payload contains a writable file."
                    )
                file_count += 1
                logical_bytes += int(child_info.st_size)
                cls._digest_field(hasher, b"file")
                cls._digest_field(hasher, str(child_info.st_size).encode("ascii"))
                with child.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        hasher.update(chunk)
                after = child.lstat()
                if (
                    after.st_dev != child_info.st_dev
                    or after.st_ino != child_info.st_ino
                    or after.st_size != child_info.st_size
                    or after.st_mtime_ns != child_info.st_mtime_ns
                ):
                    raise RuntimeError(
                        "Prepared-runtime payload changed during integrity scan."
                    )
            pending.extend(reversed(child_directories))
        return {
            "algorithm": PREPARED_RUNTIME_TREE_DIGEST,
            "digest": hasher.hexdigest(),
            "logical_bytes": logical_bytes,
            "file_count": file_count,
            "directory_count": directory_count,
            "symlink_count": symlink_count,
        }

    @staticmethod
    def _logical_tree_bytes(path: Path) -> int:
        try:
            linked = path.lstat()
        except FileNotFoundError:
            return 0
        if stat.S_ISREG(linked.st_mode):
            return int(linked.st_size)
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
            return 0
        total = 0
        for directory, directory_names, file_names in os.walk(
            path, topdown=True, followlinks=False
        ):
            current = Path(directory)
            directory_names[:] = [
                name for name in directory_names if not (current / name).is_symlink()
            ]
            for name in file_names:
                child = current / name
                try:
                    child_info = child.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(child_info.st_mode):
                    total += int(child_info.st_size)
        return total

    def _read_valid_manifest(
        self,
        *,
        cache_key: str,
        key_payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        entry_root = self.entry_root(cache_key)
        manifest_path = entry_root / "manifest.json"
        payload_root = entry_root / "payload"
        try:
            entry_info = entry_root.lstat()
            payload_info = payload_root.lstat()
            manifest_info = manifest_path.lstat()
        except FileNotFoundError:
            return None
        if (
            entry_root.is_symlink()
            or payload_root.is_symlink()
            or manifest_path.is_symlink()
            or not stat.S_ISDIR(entry_info.st_mode)
            or not stat.S_ISDIR(payload_info.st_mode)
            or not stat.S_ISREG(manifest_info.st_mode)
            or manifest_info.st_size > _MAX_MANIFEST_BYTES
        ):
            return None
        leases_root = entry_root / "leases"
        try:
            leases_info = leases_root.lstat()
        except FileNotFoundError:
            pass
        else:
            if leases_root.is_symlink() or not stat.S_ISDIR(leases_info.st_mode):
                return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        expected_keys = {
            "schema",
            "state",
            "cache_key",
            "key_payload",
            "payload_identity",
            "payload_integrity",
            "created_at",
            "completed_at",
        }
        if (
            not isinstance(manifest, dict)
            or set(manifest) != expected_keys
            or manifest.get("schema") != PREPARED_RUNTIME_CACHE_SCHEMA
            or manifest.get("state") != "complete"
            or manifest.get("cache_key") != cache_key
            or manifest.get("key_payload") != dict(key_payload)
            or _canonical_digest(manifest["key_payload"]) != cache_key
        ):
            return None
        identity = manifest.get("payload_identity")
        if not isinstance(identity, dict) or set(identity) != {"device", "inode"}:
            return None
        if (
            identity.get("device") != payload_info.st_dev
            or identity.get("inode") != payload_info.st_ino
        ):
            return None
        if any(
            isinstance(manifest.get(field), bool)
            or not isinstance(manifest.get(field), (int, float))
            or not math.isfinite(float(manifest[field]))
            or float(manifest[field]) <= 0
            for field in ("created_at", "completed_at")
        ):
            return None
        if (
            float(manifest["completed_at"]) < float(manifest["created_at"])
            or float(manifest["completed_at"]) > time.time() + 300
        ):
            return None
        expected_integrity = manifest.get("payload_integrity")
        if not isinstance(expected_integrity, dict) or set(expected_integrity) != {
            "algorithm",
            "digest",
            "logical_bytes",
            "file_count",
            "directory_count",
            "symlink_count",
        }:
            return None
        if (
            expected_integrity.get("algorithm") != PREPARED_RUNTIME_TREE_DIGEST
            or not _is_lower_hex_digest(expected_integrity.get("digest"))
            or any(
                isinstance(expected_integrity.get(field), bool)
                or not isinstance(expected_integrity.get(field), int)
                or expected_integrity[field] < 0
                for field in (
                    "logical_bytes",
                    "file_count",
                    "directory_count",
                    "symlink_count",
                )
            )
        ):
            return None
        try:
            actual_integrity = self._payload_integrity(
                payload_root, require_sealed=True
            )
        except (OSError, RuntimeError):
            return None
        if actual_integrity != expected_integrity:
            return None
        try:
            final_payload_info = payload_root.lstat()
        except FileNotFoundError:
            return None
        if (
            payload_root.is_symlink()
            or not stat.S_ISDIR(final_payload_info.st_mode)
            or final_payload_info.st_dev != payload_info.st_dev
            or final_payload_info.st_ino != payload_info.st_ino
        ):
            return None
        return manifest

    @staticmethod
    def _validate_launch_id(launch_id: str) -> None:
        if (
            not isinstance(launch_id, str)
            or not launch_id
            or len(launch_id.encode("utf-8")) > 255
            or "/" in launch_id
            or "\\" in launch_id
            or "\x00" in launch_id
            or launch_id in {".", ".."}
        ):
            raise ValueError("Prepared-runtime lease launch id is invalid.")

    @staticmethod
    def _lease_payload_is_valid(
        payload: object,
        *,
        cache_key: str,
        launch_id: str,
    ) -> bool:
        if not isinstance(payload, dict) or set(payload) != {
            "schema",
            "cache_key",
            "launch_id",
            "lease_id",
            "created_at",
        }:
            return False
        created_at = payload.get("created_at")
        return (
            payload.get("schema") == PREPARED_RUNTIME_LEASE_SCHEMA
            and payload.get("cache_key") == cache_key
            and payload.get("launch_id") == launch_id
            and isinstance(payload.get("lease_id"), str)
            and bool(payload["lease_id"])
            and not isinstance(created_at, bool)
            and isinstance(created_at, (int, float))
            and math.isfinite(float(created_at))
            and float(created_at) > 0
            and float(created_at) <= time.time() + 300
        )

    @staticmethod
    def _read_descriptor_json(descriptor: int, *, max_bytes: int) -> object | None:
        try:
            linked = os.fstat(descriptor)
            if not stat.S_ISREG(linked.st_mode) or linked.st_size > max_bytes:
                return None
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > max_bytes:
                return None
            return json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    def _inspect_lease(
        self,
        lease_path: Path,
        *,
        cache_key: str,
        launch_id: str,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Return ``active``, ``stale``, ``invalid``, or ``missing``.

        A held advisory lock is the liveness authority.  JSON records are useful
        identity and diagnostics, but a locked malformed record is still treated
        as active so cleanup can never destroy a runtime another process may use.
        """

        with self._owned_lease_lock:
            owned = self._owned_lease_descriptors.get(lease_path)
            if owned is not None:
                return "active", None
        try:
            path_info = lease_path.lstat()
        except FileNotFoundError:
            return "missing", None
        if lease_path.is_symlink() or not stat.S_ISREG(path_info.st_mode):
            return "invalid", None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lease_path, flags)
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "invalid", None
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != path_info.st_dev
                or opened.st_ino != path_info.st_ino
            ):
                return "invalid", None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    return "active", None
                return "invalid", None
            payload = self._read_descriptor_json(descriptor, max_bytes=64 * 1024)
            if self._lease_payload_is_valid(
                payload, cache_key=cache_key, launch_id=launch_id
            ):
                return "stale", payload
            return "invalid", None
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)

    def _entry_owned_lease_paths(self, entry_root: Path) -> set[Path]:
        leases_root = entry_root / "leases"
        with self._owned_lease_lock:
            return {
                path
                for path in self._owned_lease_descriptors
                if path.parent == leases_root
            }

    def _scan_leases(
        self,
        entry_root: Path,
        *,
        cache_key: str,
        cleanup: bool,
    ) -> dict[str, Any]:
        active_paths = self._entry_owned_lease_paths(entry_root)
        removed = 0
        invalid_root = False
        leases_root = entry_root / "leases"
        try:
            root_info = leases_root.lstat()
        except FileNotFoundError:
            return {
                "active_paths": active_paths,
                "removed": removed,
                "invalid_root": invalid_root,
            }
        if leases_root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
            return {
                "active_paths": active_paths,
                "removed": removed,
                "invalid_root": True,
            }
        try:
            children = list(leases_root.iterdir())
        except OSError:
            return {
                "active_paths": active_paths,
                "removed": removed,
                "invalid_root": True,
            }
        for lease_path in children:
            launch_id = lease_path.name.removesuffix(".json")
            if not lease_path.name.endswith(".json"):
                state = "invalid"
            else:
                state, _payload = self._inspect_lease(
                    lease_path,
                    cache_key=cache_key,
                    launch_id=launch_id,
                )
            if state == "active":
                active_paths.add(lease_path)
                continue
            if state in {"stale", "invalid"} and cleanup:
                try:
                    self._remove_entry(lease_path)
                    removed += 1
                except OSError:
                    invalid_root = True
        return {
            "active_paths": active_paths,
            "removed": removed,
            "invalid_root": invalid_root,
        }

    def _create_lease(
        self,
        *,
        entry_root: Path,
        cache_key: str,
        launch_id: str,
    ) -> tuple[Path, str]:
        leases_root = entry_root / "leases"
        leases_root.mkdir(mode=0o700, exist_ok=True)
        linked = leases_root.lstat()
        if leases_root.is_symlink() or not stat.S_ISDIR(linked.st_mode):
            raise RuntimeError("Prepared-runtime lease directory is invalid.")
        leases_root.chmod(0o700)
        lease_path = leases_root / f"{launch_id}.json"
        state, _payload = self._inspect_lease(
            lease_path, cache_key=cache_key, launch_id=launch_id
        )
        if state == "active":
            raise RuntimeError(
                f"Prepared-runtime launch already holds a lease: {launch_id}"
            )
        if state in {"stale", "invalid"}:
            self._remove_entry(lease_path)

        lease_id = uuid.uuid4().hex
        payload = {
            "schema": PREPARED_RUNTIME_LEASE_SCHEMA,
            "cache_key": cache_key,
            "launch_id": launch_id,
            "lease_id": lease_id,
            "created_at": time.time(),
        }
        encoded = (
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        temporary = leases_root / f".{launch_id}.{lease_id}.tmp"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        committed = False
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("Could not persist prepared-runtime lease record.")
                view = view[written:]
            os.fsync(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.replace(temporary, lease_path)
            directory = os.open(
                leases_root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            with self._owned_lease_lock:
                self._owned_lease_descriptors[lease_path] = (descriptor, lease_id)
            committed = True
            return lease_path, lease_id
        finally:
            if not committed:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(descriptor)
                temporary.unlink(missing_ok=True)

    def _read_entry_manifest(self, cache_key: str) -> dict[str, Any] | None:
        manifest_path = self.entry_root(cache_key) / "manifest.json"
        try:
            linked = manifest_path.lstat()
            if (
                manifest_path.is_symlink()
                or not stat.S_ISREG(linked.st_mode)
                or linked.st_size > _MAX_MANIFEST_BYTES
            ):
                return None
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("key_payload"), dict):
            return None
        return self._read_valid_manifest(
            cache_key=cache_key,
            key_payload=raw["key_payload"],
        )

    def acquire(
        self,
        *,
        key_payload: Mapping[str, Any],
        launch_id: str,
        build: Callable[[Path, Path], None],
    ) -> PreparedRuntimeLease:
        self._validate_launch_id(launch_id)
        cache_key = self.cache_key(key_payload)
        entry_root = self.entry_root(cache_key)
        with self._key_lock(cache_key):
            manifest = self._read_valid_manifest(
                cache_key=cache_key, key_payload=key_payload
            )
            cache_status = "hit"
            if manifest is None:
                leases = self._scan_leases(
                    entry_root,
                    cache_key=cache_key,
                    cleanup=True,
                ) if entry_root.exists() else {"active_paths": set()}
                if leases["active_paths"]:
                    raise RuntimeError(
                        "Prepared-runtime entry is invalid but still has an active lease."
                    )
                self._remove_entry(entry_root)
                entry_root.mkdir(mode=0o700)
                payload_root = entry_root / "payload"
                payload_root.mkdir(mode=0o700)
                created_at = time.time()
                try:
                    build(entry_root, payload_root)
                    self._seal_payload(payload_root)
                    payload_info = payload_root.lstat()
                    integrity = self._payload_integrity(
                        payload_root, require_sealed=True
                    )
                    manifest = {
                        "schema": PREPARED_RUNTIME_CACHE_SCHEMA,
                        "state": "complete",
                        "cache_key": cache_key,
                        "key_payload": dict(key_payload),
                        "payload_identity": {
                            "device": payload_info.st_dev,
                            "inode": payload_info.st_ino,
                        },
                        "payload_integrity": integrity,
                        "created_at": created_at,
                        "completed_at": time.time(),
                    }
                    self._atomic_write_json(entry_root / "manifest.json", manifest)
                    cache_status = "built"
                except BaseException:
                    self._remove_entry(entry_root)
                    raise
            assert manifest is not None
            lease_path, lease_id = self._create_lease(
                entry_root=entry_root,
                cache_key=cache_key,
                launch_id=launch_id,
            )
            os.utime(entry_root, None, follow_symlinks=False)
        lease = PreparedRuntimeLease(
            cache_key=cache_key,
            launch_id=launch_id,
            entry_root=entry_root,
            payload_root=entry_root / "payload",
            lease_path=lease_path,
            cache_status=cache_status,
            manifest=manifest,
            lease_id=lease_id,
        )
        self.prune(exclude_keys={cache_key})
        return lease

    def _release_lease_path(
        self,
        lease_path: Path,
        *,
        cache_key: str,
        launch_id: str,
        lease_id: str | None,
    ) -> bool:
        with self._owned_lease_lock:
            owned = self._owned_lease_descriptors.get(lease_path)
            if owned is not None:
                descriptor, owned_lease_id = owned
                if lease_id is not None and owned_lease_id != lease_id:
                    raise ValueError("Prepared-runtime lease identity changed.")
                del self._owned_lease_descriptors[lease_path]
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
                try:
                    linked = lease_path.lstat()
                except FileNotFoundError:
                    return False
                if lease_path.is_symlink() or not stat.S_ISREG(linked.st_mode):
                    raise RuntimeError("Prepared-runtime lease record is invalid.")
                lease_path.unlink()
                return True

        state, payload = self._inspect_lease(
            lease_path, cache_key=cache_key, launch_id=launch_id
        )
        if state in {"missing", "active"}:
            return False
        if lease_id is not None and (
            state != "stale" or payload is None or payload.get("lease_id") != lease_id
        ):
            raise ValueError("Prepared-runtime lease identity changed.")
        self._remove_entry(lease_path)
        return True

    def release(self, lease: PreparedRuntimeLease) -> bool:
        if not isinstance(lease, PreparedRuntimeLease):
            raise TypeError("Prepared-runtime lease is invalid.")
        with self._key_lock(lease.cache_key):
            expected = (
                self.entry_root(lease.cache_key)
                / "leases"
                / f"{lease.launch_id}.json"
            )
            if lease.lease_path != expected:
                raise ValueError("Prepared-runtime lease path changed.")
            released = self._release_lease_path(
                expected,
                cache_key=lease.cache_key,
                launch_id=lease.launch_id,
                lease_id=lease.lease_id or None,
            )
        self.prune()
        return released

    def release_launch(self, launch_id: str) -> int:
        self._validate_launch_id(launch_id)
        released = 0
        for entry in self._entry_directories():
            if not _is_lower_hex_digest(entry.name):
                continue
            lease = entry / "leases" / f"{launch_id}.json"
            with self._key_lock(entry.name):
                if self._release_lease_path(
                    lease,
                    cache_key=entry.name,
                    launch_id=launch_id,
                    lease_id=None,
                ):
                    released += 1
        self.prune()
        return released

    def _entry_directories(self) -> list[Path]:
        entries: list[Path] = []
        for path in self.entries_root.iterdir():
            try:
                linked = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(linked.st_mode) and not path.is_symlink():
                entries.append(path)
        return entries

    def prune(self, *, exclude_keys: set[str] | None = None) -> list[str]:
        excluded = set(exclude_keys or ())
        if any(not _is_lower_hex_digest(key) for key in excluded):
            raise ValueError("Prepared-runtime prune exclusion key is invalid.")
        removed: list[str] = []
        reclaimed_bytes = 0
        errors: list[str] = []
        active_keys: set[str] = set()
        with self._cache_lock():
            raw_entries = list(self.entries_root.iterdir())
            before_entries = len(raw_entries)
            before_bytes = sum(self._logical_tree_bytes(path) for path in raw_entries)
            candidates: list[dict[str, Any]] = []
            for entry in raw_entries:
                key = entry.name
                try:
                    linked = entry.lstat()
                    real_directory = (
                        stat.S_ISDIR(linked.st_mode) and not entry.is_symlink()
                    )
                    if not real_directory or not _is_lower_hex_digest(key):
                        size = self._logical_tree_bytes(entry)
                        self._remove_entry(entry)
                        reclaimed_bytes += size
                        continue
                    with self._key_lock(key):
                        lease_scan = self._scan_leases(
                            entry, cache_key=key, cleanup=True
                        )
                        active = bool(lease_scan["active_paths"])
                        if active:
                            active_keys.add(key)
                        manifest = self._read_entry_manifest(key)
                        size = self._logical_tree_bytes(entry)
                        if (
                            (manifest is None or lease_scan["invalid_root"])
                            and key not in excluded
                            and not active
                        ):
                            self._remove_entry(entry)
                            reclaimed_bytes += size
                            removed.append(key)
                            continue
                        try:
                            modified = entry.stat().st_mtime_ns
                        except FileNotFoundError:
                            continue
                        candidates.append(
                            {
                                "entry": entry,
                                "key": key,
                                "size": size,
                                "modified": modified,
                                "protected": key in excluded or active,
                            }
                        )
                except OSError as exc:
                    errors.append(f"{key}: {exc}")

            protected = [item for item in candidates if item["protected"]]
            ordinary = sorted(
                (item for item in candidates if not item["protected"]),
                key=lambda item: item["modified"],
                reverse=True,
            )
            retained_entries = len(protected)
            retained_bytes = sum(item["size"] for item in protected)
            eviction: list[dict[str, Any]] = []
            for item in ordinary:
                fits_entries = retained_entries + 1 <= self.max_entries
                fits_bytes = (
                    self.max_bytes is None
                    or retained_bytes + item["size"] <= self.max_bytes
                )
                if fits_entries and fits_bytes:
                    retained_entries += 1
                    retained_bytes += item["size"]
                else:
                    eviction.append(item)

            for item in eviction:
                key = item["key"]
                entry = item["entry"]
                try:
                    with self._key_lock(key):
                        lease_scan = self._scan_leases(
                            entry, cache_key=key, cleanup=True
                        )
                        if lease_scan["active_paths"] or key in excluded:
                            active_keys.add(key)
                            continue
                        size = self._logical_tree_bytes(entry)
                        self._remove_entry(entry)
                        reclaimed_bytes += size
                        removed.append(key)
                except OSError as exc:
                    errors.append(f"{key}: {exc}")

            remaining = list(self.entries_root.iterdir())
            after_entries = len(remaining)
            after_bytes = sum(self._logical_tree_bytes(path) for path in remaining)
            report = {
                "schema": "optpilot.prepared-runtime-prune.v1",
                "timestamp": time.time(),
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "before_entries": before_entries,
                "before_bytes": before_bytes,
                "after_entries": after_entries,
                "after_bytes": after_bytes,
                "removed_keys": list(removed),
                "reclaimed_bytes": reclaimed_bytes,
                "active_keys": sorted(active_keys),
                "excluded_keys": sorted(excluded),
                "errors": errors,
            }
        with self._owned_lease_lock:
            self._last_prune_report = report
        return removed

    def recover(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "schema": "optpilot.prepared-runtime-recovery.v1",
            "timestamp": time.time(),
            "scanned_entries": 0,
            "valid_entries": 0,
            "removed_entries": 0,
            "removed_bytes": 0,
            "stale_leases_removed": 0,
            "active_leases": 0,
            "deferred_active_entries": 0,
            "errors": [],
        }
        with self._cache_lock():
            for entry in list(self.entries_root.iterdir()):
                report["scanned_entries"] += 1
                key = entry.name
                try:
                    linked = entry.lstat()
                    if (
                        not stat.S_ISDIR(linked.st_mode)
                        or entry.is_symlink()
                        or not _is_lower_hex_digest(key)
                    ):
                        size = self._logical_tree_bytes(entry)
                        self._remove_entry(entry)
                        report["removed_entries"] += 1
                        report["removed_bytes"] += size
                        continue
                    with self._key_lock(key):
                        lease_scan = self._scan_leases(
                            entry, cache_key=key, cleanup=True
                        )
                        active_count = len(lease_scan["active_paths"])
                        report["active_leases"] += active_count
                        report["stale_leases_removed"] += lease_scan["removed"]
                        manifest = self._read_entry_manifest(key)
                        if manifest is not None and not lease_scan["invalid_root"]:
                            report["valid_entries"] += 1
                            continue
                        if active_count:
                            report["deferred_active_entries"] += 1
                            continue
                        size = self._logical_tree_bytes(entry)
                        self._remove_entry(entry)
                        report["removed_entries"] += 1
                        report["removed_bytes"] += size
                except OSError as exc:
                    report["errors"].append(f"{key}: {exc}")
        with self._owned_lease_lock:
            self._last_recovery_report = report
        return json.loads(json.dumps(report))

    @property
    def last_prune_report(self) -> dict[str, Any]:
        with self._owned_lease_lock:
            return json.loads(json.dumps(self._last_prune_report))

    @property
    def last_recovery_report(self) -> dict[str, Any]:
        with self._owned_lease_lock:
            return json.loads(json.dumps(self._last_recovery_report))

    def close(self) -> None:
        """Drop this process's live locks while leaving crash-recoverable records."""

        with self._owned_lease_lock:
            descriptors = list(self._owned_lease_descriptors.values())
            self._owned_lease_descriptors.clear()
        for descriptor, _lease_id in descriptors:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "PREPARED_RUNTIME_CACHE_SCHEMA",
    "PREPARED_RUNTIME_KEY_SCHEMA",
    "PREPARED_RUNTIME_LAYOUT_VERSION",
    "PreparedRuntimeCache",
    "PreparedRuntimeLease",
]
