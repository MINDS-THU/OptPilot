"""Exact offline Python dependency preparation for retained process studies.

This first slice intentionally accepts only vendored, hash-locked, pure-Python
wheels.  It has no arbitrary setup shell, no package-index access, and no host
environment or secret inheritance.  Prepared bytes are cached locally for
speed, then sealed into Realm content so every Study definition retains the
exact layer it will execute.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .locked_python_runtime_contract import (
    ENVIRONMENT_PREPARED_PYTHON_SCOPE,
    LOCKED_PYTHON_RUNTIME_LAYOUT,
    LOCKED_PYTHON_RUNTIME_OWNER_KIND,
    LOCKED_PYTHON_RUNTIME_POLICY_SCHEMA,
    LOCKED_PYTHON_RUNTIME_SOURCE_ROLE,
    LockedPythonRuntimeError,
    LockedPythonRuntimePlan,
    LockedWheel,
    MAX_LOCK_BYTES,
    MAX_PREPARATION_SECONDS,
    MAX_WHEELS,
    MAX_WHEEL_BYTES,
    METHOD_PREPARED_PYTHON_SCOPE,
    PreparedPythonRuntime,
    parse_locked_python_wheel_lock,
    validate_locked_python_setup_declaration,
)

from .prepared_runtime_cache import (
    PREPARED_RUNTIME_KEY_SCHEMA,
    PreparedRuntimeCache,
)
from .realm._validation import required_text
from .realm.content import AllowedTreeSource
from .realm.errors import RealmConflict
from .realm.ledger import RealmLedger
from .realm.manifests import TreeEntry, TreeManifest, validate_portable_path
from .realm.owner_derivation import SourceAnchor
from .realm.owners import OwnerMembership, OwnerPermission, OwnerState
from .realm.process_provider import ProcessProviderIdentity
from .realm.refs import BlobRef, SnapshotRef, request_digest
from .realm.service import RealmContentService


MAX_PREPARED_BYTES = 1024 * 1024 * 1024
MAX_WHEEL_FILES = 50_000
MAX_WHEEL_METADATA_BYTES = 64 * 1024


class LockedPythonRuntimePreparer:
    """Prepare, cache, seal, and retain one exact dependency layer."""

    def __init__(
        self,
        ledger: RealmLedger,
        content_service: RealmContentService,
        *,
        actor_principal_id: str,
        store_id: str,
        provider: ProcessProviderIdentity,
        cache_root: Path,
        max_cache_bytes: int | None = None,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(content_service, RealmContentService):
            raise TypeError("content_service must be a RealmContentService.")
        if not isinstance(provider, ProcessProviderIdentity):
            raise TypeError("provider must be a ProcessProviderIdentity.")
        self._ledger = ledger
        self._content_service = content_service
        self._actor = required_text(actor_principal_id, "actor_principal_id")
        self._store_id = required_text(store_id, "store_id", max_bytes=128)
        self._provider = provider
        cache_options = {} if max_cache_bytes is None else {"max_bytes": max_cache_bytes}
        self._cache = PreparedRuntimeCache(cache_root, **cache_options)

    @property
    def cache(self) -> PreparedRuntimeCache:
        return self._cache

    def prepare(
        self,
        *,
        operation_id: str,
        package_root: Path,
        package_snapshot: SnapshotRef,
        component_kind: str,
        component_id: str,
        config_relative_path: str,
        runtime: Mapping[str, Any],
        capture_ttl_seconds: float = 300,
    ) -> PreparedPythonRuntime | None:
        """Return ``None`` when no setup is declared; otherwise retain a layer."""

        required_text(operation_id, "locked runtime operation_id", max_bytes=512)
        setup = runtime.get("setup") if isinstance(runtime, Mapping) else None
        if setup in (None, {}):
            return None
        plan = self.plan(
            package_root=package_root,
            package_snapshot=package_snapshot,
            component_kind=component_kind,
            component_id=component_id,
            config_relative_path=config_relative_path,
            setup=setup,
        )
        cache_key = self._cache.cache_key(plan.key_payload)
        launch_id = "study-prepare-" + request_digest(
            {
                "cache_key": cache_key,
                "operation_id": operation_id,
                "schema": LOCKED_PYTHON_RUNTIME_POLICY_SCHEMA,
            }
        )[:40]
        try:
            lease = self._cache.acquire(
                key_payload=plan.key_payload,
                launch_id=launch_id,
                build=lambda _entry, payload: self._build(plan, payload),
            )
        except LockedPythonRuntimeError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise LockedPythonRuntimeError(
                "dependency_preparation_failed",
                "Exact dependency preparation failed before a Run was created.",
            ) from error
        try:
            membership, anchor = self._capture_or_reuse(
                cache_key=cache_key,
                payload_root=lease.payload_root,
                capture_ttl_seconds=capture_ttl_seconds,
            )
        finally:
            self._cache.release(lease)
        return PreparedPythonRuntime(
            component_kind=component_kind,
            cache_key=cache_key,
            source_anchor=anchor,
            membership=membership,
            scope=(
                ENVIRONMENT_PREPARED_PYTHON_SCOPE
                if component_kind == "environment"
                else METHOD_PREPARED_PYTHON_SCOPE
            ),
        )

    def plan(
        self,
        *,
        package_root: Path,
        package_snapshot: SnapshotRef,
        component_kind: str,
        component_id: str,
        config_relative_path: str,
        setup: Any,
    ) -> LockedPythonRuntimePlan:
        """Validate the deliberately narrow offline locked-wheel declaration."""

        if component_kind not in {"environment", "method"}:
            raise LockedPythonRuntimeError(
                "component_kind_unsupported", "Python preparation supports Environment and Method only."
            )
        if not isinstance(package_snapshot, SnapshotRef):
            raise TypeError("package_snapshot must be a SnapshotRef.")
        root = package_root.resolve(strict=True)
        try:
            config_relative = validate_portable_path(config_relative_path)
        except (TypeError, ValueError, RuntimeError) as error:
            raise LockedPythonRuntimeError(
                "dependency_config_path_invalid", "Component config path is not portable."
            ) from error
        normalized_declaration = validate_locked_python_setup_declaration(setup)
        step = normalized_declaration["steps"][0]
        requirements = step["requirements"]
        timeout_seconds = normalized_declaration["timeoutSeconds"]
        config_dir = (root / PurePosixPath(config_relative).parent).resolve()
        cwd_text = step["cwd"]
        cwd = self._safe_package_path(root, config_dir, cwd_text, "dependency setup cwd")
        if not cwd.is_dir():
            raise LockedPythonRuntimeError(
                "dependency_cwd_missing", "Dependency setup directory is missing from the registered version."
            )

        lock_paths: list[str] = []
        wheels: list[LockedWheel] = []
        seen_wheels: set[str] = set()
        total_lock_bytes = 0
        total_wheel_bytes = 0
        for requirement in requirements:
            lock_path = self._safe_package_path(
                root, cwd, requirement, "dependency lock path"
            )
            if not lock_path.is_file():
                raise LockedPythonRuntimeError(
                    "dependency_lock_missing", f"Dependency lock file is missing: {requirement}."
                )
            size = lock_path.stat().st_size
            total_lock_bytes += size
            if size > MAX_LOCK_BYTES or total_lock_bytes > MAX_LOCK_BYTES:
                raise LockedPythonRuntimeError(
                    "dependency_lock_oversized", "Dependency lock files exceed the supported size."
                )
            lock_relative = lock_path.relative_to(root).as_posix()
            lock_paths.append(lock_relative)
            for wheel_path_text, expected_digest in self._parse_lock(lock_path):
                wheel_path = self._safe_package_path(
                    root, lock_path.parent, wheel_path_text, "locked wheel path"
                )
                wheel_relative = wheel_path.relative_to(root).as_posix()
                if wheel_relative in seen_wheels:
                    raise LockedPythonRuntimeError(
                        "dependency_wheel_duplicate", "Dependency lock contains the same wheel more than once."
                    )
                if not wheel_path.is_file() or wheel_path.suffix != ".whl":
                    raise LockedPythonRuntimeError(
                        "dependency_wheel_missing", f"Locked wheel is missing: {wheel_path_text}."
                    )
                wheel_size = wheel_path.stat().st_size
                total_wheel_bytes += wheel_size
                if wheel_size < 1 or wheel_size > MAX_WHEEL_BYTES:
                    raise LockedPythonRuntimeError(
                        "dependency_wheel_oversized", f"Locked wheel is outside the supported size: {wheel_path_text}."
                    )
                if total_wheel_bytes > MAX_PREPARED_BYTES:
                    raise LockedPythonRuntimeError(
                        "dependency_wheel_oversized",
                        "Locked wheels exceed the supported total size.",
                    )
                actual_digest = self._file_digest(wheel_path)
                if actual_digest != expected_digest:
                    raise LockedPythonRuntimeError(
                        "dependency_hash_mismatch", f"Locked wheel hash does not match: {wheel_path_text}."
                    )
                seen_wheels.add(wheel_relative)
                wheels.append(
                    LockedWheel(wheel_relative, actual_digest, wheel_size, wheel_path)
                )
        if not wheels or len(wheels) > MAX_WHEELS:
            raise LockedPythonRuntimeError(
                "dependency_wheel_count_unsupported", "Dependency wheel count is outside the supported bounds."
            )
        normalized_setup = {
            "cache": "prepared",
            "timeoutSeconds": timeout_seconds,
            "steps": [
                {
                    "uses": "python-venv",
                    "cwd": cwd.relative_to(root).as_posix() or ".",
                    "requirements": sorted(lock_paths),
                }
            ],
        }
        key_payload = {
            "schema": PREPARED_RUNTIME_KEY_SCHEMA,
            "layout_version": LOCKED_PYTHON_RUNTIME_LAYOUT,
            "policy": {
                "schema": LOCKED_PYTHON_RUNTIME_POLICY_SCHEMA,
                "network": "disabled",
                "wheel_kind": "pure-python-only",
                "max_prepared_bytes": MAX_PREPARED_BYTES,
            },
            "source": {
                "snapshot_ref": str(package_snapshot),
                "config_relative_path": config_relative,
            },
            "component": {"kind": component_kind, "id": component_id},
            "setup": normalized_setup,
            "wheels": [wheel.identity() for wheel in wheels],
            "provider": self._provider.to_dict(),
        }
        return LockedPythonRuntimePlan(
            component_kind=component_kind,
            component_id=component_id,
            config_relative_path=config_relative,
            setup=normalized_setup,
            lock_relative_paths=tuple(sorted(lock_paths)),
            wheels=tuple(wheels),
            timeout_seconds=timeout_seconds,
            key_payload=key_payload,
        )

    @staticmethod
    def _parse_lock(path: Path) -> tuple[tuple[str, str], ...]:
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise LockedPythonRuntimeError(
                "dependency_lock_invalid", "Dependency lock must be bounded UTF-8 text."
            ) from error
        return parse_locked_python_wheel_lock(payload)

    @staticmethod
    def _safe_package_path(root: Path, base: Path, value: str, label: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute() or "\\" in value or "\x00" in value:
            raise LockedPythonRuntimeError(
                "dependency_path_invalid", f"{label} must be a package-relative path."
            )
        resolved = (base / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise LockedPythonRuntimeError(
                "dependency_path_invalid", f"{label} escapes the registered package."
            ) from error
        return resolved

    @staticmethod
    def _file_digest(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _payload_tree_manifest(root: Path) -> TreeManifest:
        """Compute the CAS identity of a stable private cache payload."""

        entries: list[TreeEntry] = []
        try:
            paths = sorted(
                root.rglob("*"),
                key=lambda item: item.relative_to(root).as_posix().encode("utf-8"),
            )
        except OSError as error:
            raise RealmConflict("Prepared Python runtime cache payload changed.") from error
        for path in paths:
            relative = path.relative_to(root).as_posix()
            try:
                validate_portable_path(relative)
                before = path.lstat()
            except (OSError, TypeError, ValueError, RuntimeError) as error:
                raise RealmConflict(
                    "Prepared Python runtime cache payload changed."
                ) from error
            if stat.S_ISDIR(before.st_mode):
                entries.append(TreeEntry.directory(relative))
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise RealmConflict(
                    "Prepared Python runtime cache payload contains a special file."
                )
            hasher = hashlib.sha256(b"optpilot/blob/v1\0")
            size = 0
            try:
                with path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        size += len(chunk)
                after = path.lstat()
            except OSError as error:
                raise RealmConflict(
                    "Prepared Python runtime cache payload changed."
                ) from error
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_nlink,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_nlink,
            )
            if identity_before != identity_after or size != before.st_size:
                raise RealmConflict("Prepared Python runtime cache payload changed.")
            entries.append(
                TreeEntry.file(
                    relative,
                    blob_ref=BlobRef(hasher.hexdigest()),
                    size=size,
                    executable=bool(before.st_mode & 0o111),
                )
            )
        return TreeManifest.build(entries)

    def _build(self, plan: LockedPythonRuntimePlan, payload_root: Path) -> None:
        started = time.monotonic()
        site_packages = payload_root / "site-packages"
        site_packages.mkdir(mode=0o700)
        installed_kinds: dict[str, str] = {}
        installed_spellings: dict[str, str] = {}
        explicit_entries: set[str] = set()
        total_bytes = 0
        total_files = 0
        distributions: set[str] = set()
        for wheel_index, wheel in enumerate(plan.wheels):
            self._check_deadline(started, plan.timeout_seconds)
            distribution = wheel.path.name.split("-", 1)[0].replace("_", "-").lower()
            if distribution in distributions:
                raise LockedPythonRuntimeError(
                    "dependency_distribution_duplicate", "Dependency lock contains duplicate distributions."
                )
            distributions.add(distribution)
            self._validate_wheel_tags(wheel.path.name, None)
            staged_wheel = self._stage_locked_wheel(
                wheel,
                payload_root / f".locked-wheel-{wheel_index}.whl",
                started=started,
                timeout_seconds=plan.timeout_seconds,
            )
            try:
                archive = zipfile.ZipFile(staged_wheel)
            except (OSError, zipfile.BadZipFile) as error:
                staged_wheel.unlink(missing_ok=True)
                raise LockedPythonRuntimeError(
                    "dependency_wheel_invalid", f"Locked wheel is invalid: {wheel.package_relative_path}."
                ) from error
            try:
                with archive:
                    infos = archive.infolist()
                    if len(infos) > MAX_WHEEL_FILES:
                        raise LockedPythonRuntimeError(
                            "dependency_output_oversized",
                            "Prepared dependencies exceed the supported file quota.",
                        )
                    wheel_metadata = [
                        item
                        for item in infos
                        if item.filename.endswith(".dist-info/WHEEL")
                    ]
                    if len(wheel_metadata) != 1:
                        raise LockedPythonRuntimeError(
                            "dependency_wheel_unsupported",
                            "Each wheel must contain one WHEEL metadata file.",
                        )
                    if wheel_metadata[0].file_size > MAX_WHEEL_METADATA_BYTES:
                        raise LockedPythonRuntimeError(
                            "dependency_wheel_invalid",
                            "Wheel metadata exceeds the supported size.",
                        )
                    try:
                        metadata = archive.read(wheel_metadata[0]).decode("utf-8")
                    except (KeyError, UnicodeError, zipfile.BadZipFile) as error:
                        raise LockedPythonRuntimeError(
                            "dependency_wheel_invalid", "Wheel metadata is invalid."
                        ) from error
                    self._validate_wheel_tags(wheel.path.name, metadata)
                    for info in infos:
                        self._check_deadline(started, plan.timeout_seconds)
                        output_relative = self._wheel_output_path(info.filename)
                        if output_relative is None:
                            continue
                        mode = info.external_attr >> 16
                        if stat.S_ISLNK(mode) or (
                            mode
                            and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
                        ):
                            raise LockedPythonRuntimeError(
                                "dependency_wheel_special_file",
                                "Wheel contains an unsupported special file.",
                            )
                        identity = output_relative.as_posix()
                        folded = identity.casefold()
                        prior_spelling = installed_spellings.get(folded)
                        if identity in explicit_entries or (
                            prior_spelling is not None and prior_spelling != identity
                        ):
                            raise LockedPythonRuntimeError(
                                "dependency_wheel_collision",
                                "Dependency wheels contain colliding installed paths.",
                            )
                        for parent in output_relative.parents:
                            if parent == PurePosixPath("."):
                                continue
                            parent_text = parent.as_posix()
                            parent_folded = parent_text.casefold()
                            parent_spelling = installed_spellings.get(parent_folded)
                            if (
                                installed_kinds.get(parent_folded) == "file"
                                or (
                                    parent_spelling is not None
                                    and parent_spelling != parent_text
                                )
                            ):
                                raise LockedPythonRuntimeError(
                                    "dependency_wheel_collision",
                                    "Dependency wheels contain colliding installed paths.",
                                )
                            installed_kinds[parent_folded] = "directory"
                            installed_spellings[parent_folded] = parent_text
                        if info.is_dir():
                            if installed_kinds.get(folded) == "file":
                                raise LockedPythonRuntimeError(
                                    "dependency_wheel_collision",
                                    "Dependency wheels contain colliding installed paths.",
                                )
                            installed_kinds[folded] = "directory"
                            installed_spellings[folded] = identity
                            explicit_entries.add(identity)
                            (site_packages / output_relative).mkdir(
                                parents=True, exist_ok=True
                            )
                            continue
                        if folded in installed_kinds:
                            raise LockedPythonRuntimeError(
                                "dependency_wheel_collision",
                                "Dependency wheels contain colliding installed paths.",
                            )
                        total_files += 1
                        total_bytes += int(info.file_size)
                        if (
                            total_files > MAX_WHEEL_FILES
                            or total_bytes > MAX_PREPARED_BYTES
                        ):
                            raise LockedPythonRuntimeError(
                                "dependency_output_oversized",
                                "Prepared dependencies exceed the supported quota.",
                            )
                        target = site_packages / output_relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        written = 0
                        try:
                            source = archive.open(info, "r")
                            with source, target.open("xb") as destination:
                                while True:
                                    self._check_deadline(
                                        started, plan.timeout_seconds
                                    )
                                    chunk = source.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    destination.write(chunk)
                                    written += len(chunk)
                                    if total_bytes - info.file_size + written > MAX_PREPARED_BYTES:
                                        raise LockedPythonRuntimeError(
                                            "dependency_output_oversized",
                                            "Prepared dependencies exceed the supported quota.",
                                        )
                        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                            raise LockedPythonRuntimeError(
                                "dependency_wheel_invalid",
                                "Wheel payload is invalid.",
                            ) from error
                        if written != info.file_size:
                            raise LockedPythonRuntimeError(
                                "dependency_wheel_invalid",
                                "Wheel entry size changed during extraction.",
                            )
                        installed_kinds[folded] = "file"
                        installed_spellings[folded] = identity
                        explicit_entries.add(identity)
            finally:
                staged_wheel.unlink(missing_ok=True)
        manifest = {
            "schema": "optpilot.prepared-python-wheel-layer.v1",
            "cache_key": self._cache.cache_key(plan.key_payload),
            "component": {
                "kind": plan.component_kind,
                "id": plan.component_id,
                "config_relative_path": plan.config_relative_path,
            },
            "network": "disabled",
            "provider": self._provider.to_dict(),
            "wheels": [wheel.identity() for wheel in plan.wheels],
            "file_count": total_files,
            "logical_bytes": total_bytes,
        }
        (payload_root / "prepared-runtime.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @staticmethod
    def _wheel_output_path(value: str) -> PurePosixPath | None:
        if not value or "\\" in value or "\x00" in value:
            raise LockedPythonRuntimeError(
                "dependency_wheel_path_invalid", "Wheel contains a non-portable path."
            )
        raw_parts = value[:-1].split("/") if value.endswith("/") else value.split("/")
        if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
            raise LockedPythonRuntimeError(
                "dependency_wheel_path_invalid", "Wheel contains a non-portable path."
            )
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise LockedPythonRuntimeError(
                "dependency_wheel_path_invalid", "Wheel contains a non-portable path."
            )
        parts = path.parts
        data_indexes = [index for index, part in enumerate(parts) if part.endswith(".data")]
        if data_indexes:
            if len(data_indexes) != 1 or data_indexes[0] + 1 >= len(parts):
                raise LockedPythonRuntimeError(
                    "dependency_wheel_layout_unsupported", "Wheel .data layout is unsupported."
                )
            index = data_indexes[0]
            if parts[index + 1] != "purelib":
                raise LockedPythonRuntimeError(
                    "dependency_wheel_layout_unsupported", "Only wheel purelib data is supported."
                )
            parts = parts[index + 2 :]
            if not parts:
                return None
            path = PurePosixPath(*parts)
        if path.name.endswith((".pyc", ".pyo")) or "__pycache__" in path.parts:
            return None
        if path.name.lower().endswith((".so", ".dylib", ".dll", ".pyd")):
            raise LockedPythonRuntimeError(
                "dependency_wheel_platform_unsupported",
                "Native extension payloads are not supported by this process runtime.",
            )
        try:
            validate_portable_path(path.as_posix())
        except (TypeError, ValueError, RuntimeError) as error:
            raise LockedPythonRuntimeError(
                "dependency_wheel_path_invalid",
                "Wheel contains a non-portable path.",
            ) from error
        return path

    @staticmethod
    def _check_deadline(started: float, timeout_seconds: int) -> None:
        if time.monotonic() - started > timeout_seconds:
            raise LockedPythonRuntimeError(
                "dependency_preparation_timeout",
                "Dependency preparation exceeded its time limit.",
            )

    @classmethod
    def _stage_locked_wheel(
        cls,
        wheel: LockedWheel,
        destination: Path,
        *,
        started: float,
        timeout_seconds: int,
    ) -> Path:
        hasher = hashlib.sha256()
        copied = 0
        try:
            with wheel.path.open("rb") as source, destination.open("xb") as target:
                while True:
                    cls._check_deadline(started, timeout_seconds)
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > wheel.size_bytes or copied > MAX_WHEEL_BYTES:
                        raise LockedPythonRuntimeError(
                            "dependency_source_changed",
                            "Locked wheel changed after its dependency plan was created.",
                        )
                    hasher.update(chunk)
                    target.write(chunk)
        except LockedPythonRuntimeError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise LockedPythonRuntimeError(
                "dependency_source_unavailable",
                "Locked wheel became unavailable during preparation.",
            ) from error
        if copied != wheel.size_bytes or hasher.hexdigest() != wheel.sha256:
            destination.unlink(missing_ok=True)
            raise LockedPythonRuntimeError(
                "dependency_source_changed",
                "Locked wheel changed after its dependency plan was created.",
            )
        os.chmod(destination, 0o400)
        return destination

    @staticmethod
    def _validate_wheel_tags(filename: str, metadata: str | None) -> None:
        if not filename.endswith(".whl"):
            raise LockedPythonRuntimeError(
                "dependency_wheel_platform_unsupported",
                "Only pure-Python none-any wheels are supported.",
            )
        parts = filename[:-4].rsplit("-", 3)
        if (
            len(parts) != 4
            or parts[-3] not in {"py3", "py2.py3"}
            or parts[-2] != "none"
            or parts[-1] != "any"
        ):
            raise LockedPythonRuntimeError(
                "dependency_wheel_platform_unsupported",
                "Only pure-Python none-any wheels are supported.",
            )
        if metadata is None:
            return
        root_values = [
            line.split(":", 1)[1].strip().lower()
            for line in metadata.splitlines()
            if line.lower().startswith("root-is-purelib:")
        ]
        tags = [
            line.split(":", 1)[1].strip()
            for line in metadata.splitlines()
            if line.lower().startswith("tag:")
        ]
        if root_values != ["true"] or not tags:
            raise LockedPythonRuntimeError(
                "dependency_wheel_platform_unsupported",
                "Only pure-Python wheels are supported by this process runtime.",
            )
        for tag in tags:
            tag_parts = tag.rsplit("-", 2)
            if (
                len(tag_parts) != 3
                or tag_parts[0] not in {"py3", "py2.py3"}
                or tag_parts[1] != "none"
                or tag_parts[2] != "any"
            ):
                raise LockedPythonRuntimeError(
                    "dependency_wheel_platform_unsupported",
                    "Only pure-Python none-any wheel tags are supported.",
                )

    def _capture_or_reuse(
        self,
        *,
        cache_key: str,
        payload_root: Path,
        capture_ttl_seconds: float,
    ) -> tuple[OwnerMembership, SourceAnchor]:
        expected_manifest = self._payload_tree_manifest(payload_root)
        owner_id = "locked-python-runtime-" + request_digest(
            {
                "actor_principal_id": self._actor,
                "cache_key": cache_key,
                "schema": LOCKED_PYTHON_RUNTIME_POLICY_SCHEMA,
            }
        )
        create_operation = f"locked-python-runtime/create/{cache_key}"
        self._ledger.create_owner(
            operation_id=create_operation,
            owner_id=owner_id,
            owner_kind=LOCKED_PYTHON_RUNTIME_OWNER_KIND,
            principal_id=self._actor,
        )
        owner = self._ledger.read_owner(
            actor_principal_id=self._actor,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        memberships = self._ledger.list_owner_memberships(
            actor_principal_id=self._actor,
            owner_id=owner_id,
        )
        if (
            owner.owner_kind != LOCKED_PYTHON_RUNTIME_OWNER_KIND
            or owner.principal_id != self._actor
            or owner.state is not OwnerState.ACTIVE
        ):
            raise RealmConflict("Prepared Python runtime owner facts changed.")
        if owner.revision == 1:
            return self._verify_retained_payload(
                owner_id=owner_id,
                owner=owner,
                memberships=memberships,
                expected_manifest=expected_manifest,
            )
        if owner.revision != 0 or memberships:
            raise RealmConflict("Prepared Python runtime owner revision changed.")
        try:
            change = self._ledger.begin_owner_change(
                operation_id=f"locked-python-runtime/begin/{cache_key}",
                actor_principal_id=self._actor,
                owner_id=owner_id,
                expected_owner_revision=0,
                ttl_seconds=capture_ttl_seconds,
            )
            seal = self._content_service.capture(
                actor_principal_id=self._actor,
                change_id=change.change_id,
                store_id=self._store_id,
            ).seal_tree(
                source=AllowedTreeSource(payload_root),
                operation_id=f"locked-python-runtime/seal/{cache_key}",
            )
            if seal.manifest != expected_manifest:
                raise RealmConflict(
                    "Prepared Python runtime capture differs from its cache payload."
                )
            membership = OwnerMembership(
                self._store_id,
                seal.snapshot_ref,
                LOCKED_PYTHON_RUNTIME_SOURCE_ROLE,
            )
            self._ledger.hold_owner_content(
                operation_id=f"locked-python-runtime/hold/{cache_key}",
                actor_principal_id=self._actor,
                change_id=change.change_id,
                memberships=(membership,),
            )
            self._ledger.commit_owner_change(
                operation_id=f"locked-python-runtime/commit/{cache_key}",
                actor_principal_id=self._actor,
                change_id=change.change_id,
                expected_owner_revision=0,
                additions=(membership,),
            )
        except RealmConflict:
            # A same-key preparer may have won the owner commit. Re-read only
            # the deterministic owner; never accept bytes from a mutable path.
            owner = self._ledger.read_owner(
                actor_principal_id=self._actor,
                owner_id=owner_id,
                permission=OwnerPermission.DERIVE,
            )
            memberships = self._ledger.list_owner_memberships(
                actor_principal_id=self._actor,
                owner_id=owner_id,
            )
            if owner.revision != 1:
                raise
            return self._verify_retained_payload(
                owner_id=owner_id,
                owner=owner,
                memberships=memberships,
                expected_manifest=expected_manifest,
            )
        owner = self._ledger.read_owner(
            actor_principal_id=self._actor,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        memberships = self._ledger.list_owner_memberships(
            actor_principal_id=self._actor,
            owner_id=owner_id,
        )
        return self._verify_retained_payload(
            owner_id=owner_id,
            owner=owner,
            memberships=memberships,
            expected_manifest=expected_manifest,
        )

    def _verify_retained_payload(
        self,
        *,
        owner_id: str,
        owner,
        memberships: tuple[OwnerMembership, ...],
        expected_manifest: TreeManifest,
    ) -> tuple[OwnerMembership, SourceAnchor]:
        if (
            owner.owner_kind != LOCKED_PYTHON_RUNTIME_OWNER_KIND
            or owner.principal_id != self._actor
            or owner.state is not OwnerState.ACTIVE
            or owner.revision != 1
            or len(memberships) != 1
        ):
            raise RealmConflict("Prepared Python runtime owner facts changed.")
        membership = memberships[0]
        if (
            membership.store_id != self._store_id
            or membership.role != LOCKED_PYTHON_RUNTIME_SOURCE_ROLE
            or not isinstance(membership.content_ref, SnapshotRef)
        ):
            raise RealmConflict("Prepared Python runtime membership changed.")
        actual_manifest = self._content_service.verify_owner_tree_manifest(
            actor_principal_id=self._actor,
            owner_id=owner_id,
            expected_owner_revision=1,
            membership=membership,
        )
        if actual_manifest != expected_manifest:
            raise RealmConflict(
                "Prepared Python runtime retained bytes differ from its cache key."
            )
        anchor = self._ledger.read_owner_source_anchor(
            actor_principal_id=self._actor,
            owner_id=owner_id,
            revision=1,
        )
        return membership, anchor


__all__ = [
    "ENVIRONMENT_PREPARED_PYTHON_SCOPE",
    "LOCKED_PYTHON_RUNTIME_OWNER_KIND",
    "LOCKED_PYTHON_RUNTIME_POLICY_SCHEMA",
    "LOCKED_PYTHON_RUNTIME_SOURCE_ROLE",
    "LockedPythonRuntimeError",
    "LockedPythonRuntimePlan",
    "LockedPythonRuntimePreparer",
    "METHOD_PREPARED_PYTHON_SCOPE",
    "PreparedPythonRuntime",
    "validate_locked_python_setup_declaration",
]
