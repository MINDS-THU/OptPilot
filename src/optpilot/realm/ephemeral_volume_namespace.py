"""Descriptor-safe local namespaces for ephemeral writable volumes.

The provider path and inode facts in this module are operational cleanup
identity only.  A volume's writable ``data/`` directory is wrapped by an
immutable claim marker so retries can distinguish the exact allocation from a
replaced path.  Permanent retirement markers make every allocated name
single-use even after physical cleanup.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from .config import prepare_private_directory
from .errors import RealmConflict, RealmIntegrityError
from .filesystem_quota import FilesystemQuota
from .projection import _projection_mount_identity, _remove_tree_contents
from .refs import canonical_json_bytes

try:
    import fcntl
except ImportError:  # pragma: no cover - secure namespace v1 is POSIX-only
    fcntl = None  # type: ignore[assignment]


LOCAL_DIRECTORY_VOLUME_PROVIDER = "local-ephemeral-directory-v1"

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_ROOT_MARKER_NAME = ".optpilot-ephemeral-volume-root"
_OPPOSITE_ROOT_MARKER_NAME = ".optpilot-projection-root"
_ROOT_LOCK_NAME = ".optpilot-storage-root.lock"
_CLAIM_NAME = "claim.json"
_DATA_NAME = "data"
_INITIALIZATION_PROOF_NAME = ".optpilot-provider-initialization.json"
_INITIALIZATION_TEMP_NAME = ".optpilot-provider-initialization.tmp"
_ROOT_SCHEMA = "optpilot.ephemeral-volume-root.v1"
_CLAIM_SCHEMA = "optpilot.ephemeral-volume-claim.v1"
_RETIREMENT_SCHEMA = "optpilot.ephemeral-volume-retirement.v1"
_RETIREMENT_PROOF_SCHEMA = "optpilot.ephemeral-volume-retirement-proof.v1"
_CLEANUP_TOMBSTONE_SCHEMA = "optpilot.ephemeral-volume-cleanup-tombstone.v1"
_MAX_MARKER_BYTES = 64 * 1024


@dataclass(frozen=True)
class EphemeralVolumeRootBinding:
    path: Path
    realm_id: str
    volume_root_id: str
    claim_nonce: str
    device_id: int
    inode: int
    provider_kind: str = LOCAL_DIRECTORY_VOLUME_PROVIDER

    def __post_init__(self) -> None:
        path = Path(self.path).expanduser().absolute()
        object.__setattr__(self, "path", path)
        _required_text(self.realm_id, "realm_id")
        _required_text(self.volume_root_id, "ephemeral volume root id")
        _required_text(self.provider_kind, "ephemeral volume provider kind")
        _lower_hex_digest(self.claim_nonce, "ephemeral volume root claim nonce")
        _nonnegative_int(self.device_id, "ephemeral volume root device id")
        _positive_int(self.inode, "ephemeral volume root inode")

    def operational_record(self) -> dict[str, object]:
        return {
            "volume_root_id": self.volume_root_id,
            "canonical_path": str(self.path),
            "realm_id": self.realm_id,
            "claim_nonce": self.claim_nonce,
            "device_id": self.device_id,
            "inode": self.inode,
            "provider_kind": self.provider_kind,
        }

    def portable_record(self) -> dict[str, object]:
        return {}


@dataclass(frozen=True)
class EphemeralVolumeNamespaceClaim:
    realm_id: str
    volume_root_id: str
    volume_id: str
    claim_nonce: str

    def __post_init__(self) -> None:
        _required_text(self.realm_id, "realm_id")
        _required_text(self.volume_root_id, "ephemeral volume root id")
        _required_text(self.volume_id, "ephemeral volume id")
        _lower_hex_digest(self.claim_nonce, "ephemeral volume claim nonce")

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _CLAIM_SCHEMA,
            "realm_id": self.realm_id,
            "volume_root_id": self.volume_root_id,
            "volume_id": self.volume_id,
            "claim_nonce": self.claim_nonce,
        }


@dataclass(frozen=True)
class EphemeralVolumeNamespaceIdentity:
    directory_name: str
    wrapper_device_id: int
    wrapper_inode: int
    data_device_id: Optional[int] = None
    data_inode: Optional[int] = None

    def __post_init__(self) -> None:
        _safe_component(self.directory_name, "ephemeral volume directory name")
        _nonnegative_int(self.wrapper_device_id, "ephemeral volume wrapper device id")
        _positive_int(self.wrapper_inode, "ephemeral volume wrapper inode")
        if (self.data_device_id is None) != (self.data_inode is None):
            raise ValueError("ephemeral volume data identity must be complete.")
        if self.data_device_id is not None:
            _nonnegative_int(self.data_device_id, "ephemeral volume data device id")
            _positive_int(self.data_inode, "ephemeral volume data inode")

    def with_data(self, *, device_id: int, inode: int) -> "EphemeralVolumeNamespaceIdentity":
        return EphemeralVolumeNamespaceIdentity(
            self.directory_name,
            self.wrapper_device_id,
            self.wrapper_inode,
            device_id,
            inode,
        )

    def operational_record(self) -> dict[str, object]:
        return {
            "directory_name": self.directory_name,
            "wrapper_device_id": self.wrapper_device_id,
            "wrapper_inode": self.wrapper_inode,
            "data_device_id": self.data_device_id,
            "data_inode": self.data_inode,
        }


class AttachedEphemeralVolumeNamespace:
    """Pinned no-follow view of one exact writable volume directory."""

    def __init__(
        self,
        *,
        binding: EphemeralVolumeRootBinding,
        claim: EphemeralVolumeNamespaceClaim,
        identity: EphemeralVolumeNamespaceIdentity,
        root_fd: int,
        wrapper_fd: int,
        data_fd: int,
    ) -> None:
        self.binding = binding
        self.claim = claim
        self.identity = identity
        self._root_fd: Optional[int] = root_fd
        self._wrapper_fd: Optional[int] = wrapper_fd
        self._data_fd: Optional[int] = data_fd

    @property
    def path(self) -> Path:
        self.validate()
        return self.binding.path / self.identity.directory_name / _DATA_NAME

    @property
    def closed(self) -> bool:
        return self._root_fd is None

    def validate(self) -> None:
        if self._root_fd is None or self._wrapper_fd is None or self._data_fd is None:
            raise RealmIntegrityError("Ephemeral volume namespace attachment is closed.")
        _validate_root_descriptor(self.binding, self._root_fd)
        _require_directory_link(
            self._root_fd,
            self.identity.directory_name,
            self._wrapper_fd,
            (self.identity.wrapper_device_id, self.identity.wrapper_inode),
            "ephemeral volume wrapper",
        )
        _validate_claim(self._wrapper_fd, self.claim)
        if self.identity.data_device_id is None or self.identity.data_inode is None:
            raise RealmIntegrityError("Ephemeral volume data identity is missing.")
        _require_directory_link(
            self._wrapper_fd,
            _DATA_NAME,
            self._data_fd,
            (self.identity.data_device_id, self.identity.data_inode),
            "ephemeral volume data directory",
        )

    def validate_quota(self, quota: FilesystemQuota) -> None:
        """Descriptor-scan this exact tree at one advisory checkpoint."""

        if not isinstance(quota, FilesystemQuota):
            raise TypeError("quota must be FilesystemQuota.")
        self.validate()
        assert self._data_fd is not None
        data = os.fstat(self._data_fd)
        _scan_writable_tree(
            self._data_fd,
            quota=quota,
            expected_device=data.st_dev,
            expected_mount_identity=_projection_mount_identity(self._data_fd),
        )
        self.validate()

    def initialize_once(
        self,
        *,
        proof: Mapping[str, object],
        realize: Callable[[int], None],
        validate_existing: Callable[[int], None],
        authorize_publication: Callable[[], None],
        progress: Callable[[], None] | None = None,
    ) -> bool:
        """Initialize ``data/`` once and publish provider-private proof.

        The proof lives in the wrapper, never in the runtime-visible writable
        root.  Its presence is a permanent provider decision: a later attach
        validates only its exact identity and never interprets user changes to
        ``data/`` as a request to seed the volume again.
        """

        if not isinstance(proof, Mapping):
            raise TypeError("ephemeral volume initialization proof must be a mapping.")
        expected = dict(proof)
        encoded = canonical_json_bytes(expected)
        if len(encoded) > _MAX_MARKER_BYTES:
            raise ValueError("ephemeral volume initialization proof is too large.")
        if (
            not callable(realize)
            or not callable(validate_existing)
            or not callable(authorize_publication)
        ):
            raise TypeError("ephemeral volume initialization callbacks must be callable.")
        self.validate()
        if self._wrapper_fd is None or self._data_fd is None:  # pragma: no cover
            raise RealmIntegrityError("Ephemeral volume namespace attachment is closed.")
        _acquire_descriptor_lock(self._wrapper_fd, progress=progress)
        try:
            self.validate()
            try:
                existing = _read_canonical_marker(
                    self._wrapper_fd,
                    _INITIALIZATION_PROOF_NAME,
                    label="ephemeral volume initialization proof",
                )
            except FileNotFoundError:
                existing = None
            if existing is not None:
                _require_initialization_temp_absent(self._wrapper_fd)
                if canonical_json_bytes(existing) != encoded:
                    raise RealmIntegrityError(
                        "Ephemeral volume initialization proof has a different identity."
                    )
                try:
                    validate_existing(self._data_fd)
                except RealmIntegrityError as error:
                    # A concurrent preparer may have committed and launched
                    # while this one waited for the wrapper lock.  Never erase
                    # a possibly live trial merely because preflight has not
                    # yet refreshed that authority in this process.
                    raise RealmIntegrityError(
                        "Proved layered volume changed before binding preflight."
                    ) from error
                return False

            _remove_safe_initialization_temp(self._wrapper_fd)
            realize(self._data_fd)
            self.validate()
            authorize_publication()
            self.validate()
            validate_existing(self._data_fd)
            _write_file_exclusive(
                self._wrapper_fd,
                _INITIALIZATION_TEMP_NAME,
                encoded,
                mode=0o400,
            )
            os.fsync(self._wrapper_fd)
            try:
                os.rename(
                    _INITIALIZATION_TEMP_NAME,
                    _INITIALIZATION_PROOF_NAME,
                    src_dir_fd=self._wrapper_fd,
                    dst_dir_fd=self._wrapper_fd,
                )
            except FileExistsError as error:
                raise RealmIntegrityError(
                    "Ephemeral volume initialization proof publication raced."
                ) from error
            os.fsync(self._wrapper_fd)
            persisted = _read_canonical_marker(
                self._wrapper_fd,
                _INITIALIZATION_PROOF_NAME,
                label="ephemeral volume initialization proof",
            )
            if canonical_json_bytes(persisted) != encoded:
                raise RealmIntegrityError(
                    "Ephemeral volume initialization proof publication changed."
                )
            validate_existing(self._data_fd)
            self.validate()
            return True
        except OSError as error:
            raise RealmIntegrityError(
                "Ephemeral volume initialization could not be completed safely."
            ) from error
        finally:
            _release_descriptor_lock(self._wrapper_fd)

    def require_initialization_proof(
        self,
        *,
        proof: Mapping[str, object],
        progress: Callable[[], None] | None = None,
    ) -> None:
        """Require exact provider proof without inspecting mutable ``data/``."""

        if not isinstance(proof, Mapping):
            raise TypeError("ephemeral volume initialization proof must be a mapping.")
        expected = dict(proof)
        self.validate()
        if self._wrapper_fd is None:  # pragma: no cover
            raise RealmIntegrityError("Ephemeral volume namespace attachment is closed.")
        _acquire_descriptor_lock(self._wrapper_fd, progress=progress)
        try:
            self.validate()
            try:
                persisted = _read_canonical_marker(
                    self._wrapper_fd,
                    _INITIALIZATION_PROOF_NAME,
                    label="ephemeral volume initialization proof",
                )
            except FileNotFoundError as error:
                raise RealmIntegrityError(
                    "Ephemeral volume initialization proof is missing."
                ) from error
            _require_initialization_temp_absent(self._wrapper_fd)
            if canonical_json_bytes(persisted) != canonical_json_bytes(expected):
                raise RealmIntegrityError(
                    "Ephemeral volume initialization proof has a different identity."
                )
        finally:
            _release_descriptor_lock(self._wrapper_fd)

    def close(self) -> None:
        for field in ("_data_fd", "_wrapper_fd", "_root_fd"):
            descriptor = getattr(self, field)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, field, None)

    def __enter__(self) -> "AttachedEphemeralVolumeNamespace":
        self.validate()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def prepare_ephemeral_volume_root(
    path: Path, *, realm_id: str
) -> EphemeralVolumeRootBinding:
    if os.name == "nt":  # pragma: no cover - secure namespace v1 is POSIX-only
        raise NotImplementedError(
            "Managed ephemeral writable volumes require POSIX descriptors."
        )
    _required_text(realm_id, "realm_id")
    root = prepare_private_directory(path)
    root_fd = _open_directory(root)
    try:
        with _root_lock(root_fd, exclusive=True):
            marker = _load_or_create_root_marker(root_fd, realm_id=realm_id)
        info = os.fstat(root_fd)
        return EphemeralVolumeRootBinding(
            path=root,
            realm_id=realm_id,
            volume_root_id=str(marker["volume_root_id"]),
            claim_nonce=str(marker["claim_nonce"]),
            device_id=info.st_dev,
            inode=info.st_ino,
        )
    finally:
        os.close(root_fd)


def validate_ephemeral_volume_root(binding: EphemeralVolumeRootBinding) -> None:
    root_fd = _open_directory(binding.path)
    try:
        _validate_root_descriptor(binding, root_fd)
    finally:
        os.close(root_fd)


def create_ephemeral_volume_namespace(
    binding: EphemeralVolumeRootBinding,
    *,
    directory_name: str,
    volume_id: str,
    claim_nonce: str,
) -> tuple[EphemeralVolumeNamespaceClaim, EphemeralVolumeNamespaceIdentity]:
    """Create or exactly replay one still-empty, permanently unique volume."""

    _safe_component(directory_name, "ephemeral volume directory name")
    claim = EphemeralVolumeNamespaceClaim(
        binding.realm_id,
        binding.volume_root_id,
        volume_id,
        claim_nonce,
    )
    build_name = _build_name(claim)
    root_fd = _open_directory(binding.path)
    wrapper_fd: Optional[int] = None
    data_fd: Optional[int] = None
    try:
        _validate_root_descriptor(binding, root_fd)
        with _root_lock(root_fd, exclusive=True):
            if _retirement_exists(root_fd, claim):
                raise RealmConflict("Ephemeral volume claim is permanently retired.")
            try:
                wrapper_fd = os.open(
                    directory_name, _DIRECTORY_FLAGS, dir_fd=root_fd
                )
                published = True
            except FileNotFoundError:
                published = False
                try:
                    os.mkdir(build_name, 0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
                wrapper_fd = os.open(build_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
            wrapper_info = os.fstat(wrapper_fd)
            _require_safe_private_directory(
                wrapper_info, binding.device_id, "volume wrapper"
            )
            entries = set(os.listdir(wrapper_fd))
            if published:
                if not {_CLAIM_NAME, _DATA_NAME}.issubset(entries) or not entries.issubset(
                    _published_wrapper_entries()
                ):
                    raise RealmIntegrityError(
                        "Published ephemeral volume wrapper is incomplete or changed."
                    )
                _validate_claim(wrapper_fd, claim)
                try:
                    os.stat(build_name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise RealmIntegrityError(
                        "Published ephemeral volume still has a build namespace."
                    )
            else:
                # The build name is never exposed to a runtime.  An empty
                # private build directory is the one safe crash state before
                # its immutable claim marker was published.
                if _CLAIM_NAME not in entries:
                    if entries:
                        raise RealmIntegrityError(
                            "Ephemeral volume build namespace is unclaimed and nonempty."
                        )
                    _write_file_exclusive(
                        wrapper_fd,
                        _CLAIM_NAME,
                        canonical_json_bytes(claim.to_dict()),
                        mode=0o400,
                    )
                    entries.add(_CLAIM_NAME)
                else:
                    _validate_claim(wrapper_fd, claim)
                if not entries.issubset({_CLAIM_NAME, _DATA_NAME}):
                    raise RealmIntegrityError(
                        "Ephemeral volume build namespace contains unknown entries."
                    )
                if _DATA_NAME not in entries:
                    os.mkdir(_DATA_NAME, 0o700, dir_fd=wrapper_fd)
                    entries.add(_DATA_NAME)
                if entries != {_CLAIM_NAME, _DATA_NAME}:
                    raise RealmIntegrityError(
                        "Ephemeral volume build namespace is incomplete."
                    )
            if not published:
                _validate_claim(wrapper_fd, claim)
            try:
                data_fd = os.open(_DATA_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd)
            except FileNotFoundError as error:
                raise RealmIntegrityError(
                    "Ephemeral volume data directory disappeared during allocation."
                ) from error
            data_info = os.fstat(data_fd)
            _require_safe_private_directory(
                data_info, wrapper_info.st_dev, "volume data"
            )
            if os.listdir(data_fd):
                raise RealmIntegrityError(
                    "An allocating ephemeral volume is not fresh and empty."
                )
            os.fsync(data_fd)
            os.fsync(wrapper_fd)
            if not published:
                _require_directory_link(
                    root_fd,
                    build_name,
                    wrapper_fd,
                    (wrapper_info.st_dev, wrapper_info.st_ino),
                    "ephemeral volume build wrapper",
                )
                try:
                    os.rename(
                        build_name,
                        directory_name,
                        src_dir_fd=root_fd,
                        dst_dir_fd=root_fd,
                    )
                except FileExistsError as error:
                    raise RealmConflict(
                        "Ephemeral volume publication destination was occupied."
                    ) from error
                os.fsync(root_fd)
            identity = EphemeralVolumeNamespaceIdentity(
                directory_name,
                wrapper_info.st_dev,
                wrapper_info.st_ino,
                data_info.st_dev,
                data_info.st_ino,
            )
            _require_directory_link(
                root_fd,
                directory_name,
                wrapper_fd,
                (wrapper_info.st_dev, wrapper_info.st_ino),
                "ephemeral volume wrapper",
            )
            _require_directory_link(
                wrapper_fd,
                _DATA_NAME,
                data_fd,
                (data_info.st_dev, data_info.st_ino),
                "ephemeral volume data directory",
            )
            _validate_claim(wrapper_fd, claim)
            os.fsync(root_fd)
            return claim, identity
    except BaseException as error:
        if isinstance(error, OSError):
            raise RealmIntegrityError(
                "Ephemeral volume namespace could not be created safely."
            ) from error
        raise
    finally:
        if data_fd is not None:
            os.close(data_fd)
        if wrapper_fd is not None:
            os.close(wrapper_fd)
        os.close(root_fd)


def find_ephemeral_volume_namespace_identity(
    binding: EphemeralVolumeRootBinding,
    claim: EphemeralVolumeNamespaceClaim,
    *,
    directory_name: str,
    cleanup_token: str,
) -> Optional[EphemeralVolumeNamespaceIdentity]:
    """Fence allocation and recover one exact namespace cleanup identity.

    Mere absence of the public name is never treated as proof that cleanup is
    complete.  A replay may recover either the deterministic build name, the
    deterministic private retirement name, or a durable retirement/tombstone
    proof.  Otherwise the volume must be quarantined: an exact tree may have
    been renamed out of the managed namespace.
    """

    _safe_component(directory_name, "ephemeral volume directory name")
    _lower_hex_digest(cleanup_token, "ephemeral volume cleanup token")
    _require_claim_binding(binding, claim)
    root_fd = _open_directory(binding.path)
    wrapper_fd: Optional[int] = None
    data_fd: Optional[int] = None
    try:
        _validate_root_descriptor(binding, root_fd)
        with _root_lock(root_fd, exclusive=True):
            _publish_or_validate_retirement(root_fd, claim, cleanup_token)
            proof = _load_optional_cleanup_marker(
                root_fd,
                _retirement_proof_name(claim, cleanup_token),
                expected_format=_RETIREMENT_PROOF_SCHEMA,
                claim=claim,
                cleanup_token=cleanup_token,
                directory_name=None,
                label="ephemeral volume retirement proof",
            )
            tombstone = _load_optional_cleanup_marker(
                root_fd,
                _tombstone_name(claim, cleanup_token),
                expected_format=_CLEANUP_TOMBSTONE_SCHEMA,
                claim=claim,
                cleanup_token=cleanup_token,
                directory_name=None,
                label="ephemeral volume cleanup tombstone",
            )
            if proof is not None and tombstone is not None and proof != tombstone:
                raise RealmIntegrityError(
                    "Ephemeral volume cleanup proofs disagree about namespace identity."
                )
            durable_identity = tombstone or proof
            candidate_names = (
                directory_name,
                _build_name(claim),
                _retired_namespace_name(claim, cleanup_token),
            )
            present = []
            for candidate in candidate_names:
                try:
                    os.stat(candidate, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                present.append(candidate)
            if durable_identity is not None:
                public_names = set(present).intersection(candidate_names[:2])
                if public_names:
                    raise RealmIntegrityError(
                        "A retired ephemeral volume reappeared at a public name."
                    )
                return durable_identity
            if len(present) != 1:
                if not present:
                    raise RealmIntegrityError(
                        "Ephemeral volume namespace is absent without exact cleanup proof."
                    )
                raise RealmIntegrityError(
                    "Ephemeral volume occupies multiple allocation namespaces."
                )
            candidate_name = present[0]
            build_namespace = candidate_name == _build_name(claim)
            retired_namespace = candidate_name == _retired_namespace_name(
                claim, cleanup_token
            )
            try:
                wrapper_fd = os.open(
                    candidate_name, _DIRECTORY_FLAGS, dir_fd=root_fd
                )
            except OSError as error:
                raise RealmIntegrityError(
                    "Ephemeral volume exists but is unavailable or unsafe."
                ) from error
            wrapper_info = os.fstat(wrapper_fd)
            _require_safe_private_directory(wrapper_info, binding.device_id, "volume wrapper")
            _acquire_descriptor_lock(wrapper_fd)
            entries = set(os.listdir(wrapper_fd))
            if build_namespace and _CLAIM_NAME not in entries:
                if entries:
                    raise RealmIntegrityError(
                        "Unclaimed ephemeral volume build namespace is nonempty."
                    )
                _write_file_exclusive(
                    wrapper_fd,
                    _CLAIM_NAME,
                    canonical_json_bytes(claim.to_dict()),
                    mode=0o400,
                )
                entries.add(_CLAIM_NAME)
            _validate_claim(wrapper_fd, claim)
            if build_namespace and _DATA_NAME not in entries:
                if not entries.issubset({_CLAIM_NAME}):
                    raise RealmIntegrityError(
                        "Ephemeral volume build namespace contains unknown entries."
                    )
                os.mkdir(_DATA_NAME, 0o700, dir_fd=wrapper_fd)
                entries.add(_DATA_NAME)
            allowed_entries = (
                {_CLAIM_NAME, _DATA_NAME}
                if build_namespace
                else _published_wrapper_entries()
            )
            if not entries.issubset(allowed_entries):
                raise RealmIntegrityError(
                    "Ephemeral volume wrapper contains unknown entries."
                )
            try:
                data_fd = os.open(_DATA_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd)
            except FileNotFoundError:
                data_fd = None
            if data_fd is None:
                identity = EphemeralVolumeNamespaceIdentity(
                    directory_name if retired_namespace else candidate_name,
                    wrapper_info.st_dev,
                    wrapper_info.st_ino,
                )
            else:
                data_info = os.fstat(data_fd)
                _require_safe_private_directory(
                    data_info, wrapper_info.st_dev, "volume data"
                )
                identity = EphemeralVolumeNamespaceIdentity(
                    directory_name if retired_namespace else candidate_name,
                    wrapper_info.st_dev,
                    wrapper_info.st_ino,
                    data_info.st_dev,
                    data_info.st_ino,
                )
            _require_directory_link(
                root_fd,
                candidate_name,
                wrapper_fd,
                (identity.wrapper_device_id, identity.wrapper_inode),
                "ephemeral volume wrapper",
            )
            return identity
    except RealmIntegrityError:
        raise
    except OSError as error:
        raise RealmIntegrityError(
            "Ephemeral volume identity could not be proven safely."
        ) from error
    finally:
        if data_fd is not None:
            os.close(data_fd)
        if wrapper_fd is not None:
            os.close(wrapper_fd)
        os.close(root_fd)


def attach_ephemeral_volume_namespace(
    binding: EphemeralVolumeRootBinding,
    claim: EphemeralVolumeNamespaceClaim,
    identity: EphemeralVolumeNamespaceIdentity,
) -> AttachedEphemeralVolumeNamespace:
    if identity.data_device_id is None or identity.data_inode is None:
        raise ValueError("An active ephemeral volume requires a data identity.")
    _require_claim_binding(binding, claim)
    root_fd = _open_directory(binding.path)
    wrapper_fd: Optional[int] = None
    data_fd: Optional[int] = None
    try:
        _validate_root_descriptor(binding, root_fd)
        wrapper_fd = os.open(identity.directory_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        _require_directory_link(
            root_fd,
            identity.directory_name,
            wrapper_fd,
            (identity.wrapper_device_id, identity.wrapper_inode),
            "ephemeral volume wrapper",
        )
        _validate_claim(wrapper_fd, claim)
        data_fd = os.open(_DATA_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd)
        attached = AttachedEphemeralVolumeNamespace(
            binding=binding,
            claim=claim,
            identity=identity,
            root_fd=root_fd,
            wrapper_fd=wrapper_fd,
            data_fd=data_fd,
        )
        attached.validate()
        root_fd = wrapper_fd = data_fd = None  # type: ignore[assignment]
        return attached
    except OSError as error:
        raise RealmIntegrityError(
            "Ephemeral volume namespace could not be attached safely."
        ) from error
    finally:
        for descriptor in (data_fd, wrapper_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def observe_active_ephemeral_volume_namespace_identity(
    binding: EphemeralVolumeRootBinding,
    claim: EphemeralVolumeNamespaceClaim,
    *,
    directory_name: str,
) -> EphemeralVolumeNamespaceIdentity:
    """Bind a durable volume claim to this attachment's live descriptor facts."""

    _safe_component(directory_name, "ephemeral volume directory name")
    _require_claim_binding(binding, claim)
    root_fd = _open_directory(binding.path)
    wrapper_fd: Optional[int] = None
    data_fd: Optional[int] = None
    try:
        _validate_root_descriptor(binding, root_fd)
        wrapper_fd = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        wrapper_info = os.fstat(wrapper_fd)
        _require_safe_private_directory(
            wrapper_info, binding.device_id, "volume wrapper"
        )
        _require_directory_link(
            root_fd,
            directory_name,
            wrapper_fd,
            (wrapper_info.st_dev, wrapper_info.st_ino),
            "ephemeral volume wrapper",
        )
        _validate_claim(wrapper_fd, claim)
        data_fd = os.open(_DATA_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd)
        data_info = os.fstat(data_fd)
        _require_safe_private_directory(
            data_info, wrapper_info.st_dev, "volume data"
        )
        _require_directory_link(
            wrapper_fd,
            _DATA_NAME,
            data_fd,
            (data_info.st_dev, data_info.st_ino),
            "ephemeral volume data directory",
        )
        return EphemeralVolumeNamespaceIdentity(
            directory_name,
            wrapper_info.st_dev,
            wrapper_info.st_ino,
            data_info.st_dev,
            data_info.st_ino,
        )
    except OSError as error:
        raise RealmIntegrityError(
            "Ephemeral volume namespace claim could not be observed safely."
        ) from error
    finally:
        for descriptor in (data_fd, wrapper_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def cleanup_ephemeral_volume_namespace(
    binding: EphemeralVolumeRootBinding,
    claim: EphemeralVolumeNamespaceClaim,
    identity: EphemeralVolumeNamespaceIdentity,
    *,
    cleanup_token: str,
) -> bool:
    """Atomically retire and delete only the exact fenced namespace.

    The exact wrapper is first renamed to a cleanup-token-derived private name
    and that identity is durably recorded.  Only then may recursive deletion
    begin.  Consequently, public-name absence by itself can never turn into a
    successful cleanup receipt.
    """

    _lower_hex_digest(cleanup_token, "ephemeral volume cleanup token")
    _require_claim_binding(binding, claim)
    root_fd = _open_directory(binding.path)
    wrapper_fd: Optional[int] = None
    data_fd: Optional[int] = None
    try:
        _validate_root_descriptor(binding, root_fd)
        with _root_lock(root_fd, exclusive=True):
            _publish_or_validate_retirement(root_fd, claim, cleanup_token)
            retired_name = _retired_namespace_name(claim, cleanup_token)
            proof_name = _retirement_proof_name(claim, cleanup_token)
            tombstone_name = _tombstone_name(claim, cleanup_token)
            proof = _load_optional_cleanup_marker(
                root_fd,
                proof_name,
                expected_format=_RETIREMENT_PROOF_SCHEMA,
                claim=claim,
                cleanup_token=cleanup_token,
                directory_name=identity.directory_name,
                label="ephemeral volume retirement proof",
            )
            tombstone = _load_optional_cleanup_marker(
                root_fd,
                tombstone_name,
                expected_format=_CLEANUP_TOMBSTONE_SCHEMA,
                claim=claim,
                cleanup_token=cleanup_token,
                directory_name=identity.directory_name,
                label="ephemeral volume cleanup tombstone",
            )
            for persisted in (proof, tombstone):
                if persisted is not None and persisted != identity:
                    raise RealmIntegrityError(
                        "Ephemeral volume cleanup proof has a different identity."
                    )
            if proof is not None and tombstone is not None and proof != tombstone:
                raise RealmIntegrityError(
                    "Ephemeral volume cleanup proofs disagree about namespace identity."
                )

            public_exists = _path_exists(root_fd, identity.directory_name)
            retired_exists = _path_exists(root_fd, retired_name)
            if proof is not None or tombstone is not None:
                if public_exists:
                    raise RealmIntegrityError(
                        "Retired ephemeral volume reappeared at its public name."
                    )
            else:
                if public_exists and retired_exists:
                    raise RealmIntegrityError(
                        "Ephemeral volume exists at both public and retirement names."
                    )
                source_name = identity.directory_name if public_exists else retired_name
                if not public_exists and not retired_exists:
                    raise RealmIntegrityError(
                        "Ephemeral volume disappeared without exact retirement proof."
                    )
                wrapper_fd, data_fd = _open_exact_cleanup_namespace(
                    root_fd,
                    source_name,
                    claim=claim,
                    identity=identity,
                    require_data=True,
                )
                if public_exists:
                    os.rename(
                        identity.directory_name,
                        retired_name,
                        src_dir_fd=root_fd,
                        dst_dir_fd=root_fd,
                    )
                    os.fsync(root_fd)
                    _require_directory_link(
                        root_fd,
                        retired_name,
                        wrapper_fd,
                        (identity.wrapper_device_id, identity.wrapper_inode),
                        "retired ephemeral volume wrapper",
                    )
                _publish_or_validate_cleanup_marker(
                    root_fd,
                    proof_name,
                    expected_format=_RETIREMENT_PROOF_SCHEMA,
                    claim=claim,
                    cleanup_token=cleanup_token,
                    identity=identity,
                    label="ephemeral volume retirement proof",
                )
                proof = identity

            if wrapper_fd is None:
                try:
                    wrapper_fd = os.open(
                        retired_name, _DIRECTORY_FLAGS, dir_fd=root_fd
                    )
                except FileNotFoundError:
                    if tombstone is None:
                        raise RealmIntegrityError(
                            "Retired ephemeral volume disappeared before cleanup proof."
                        )
                    os.fsync(root_fd)
                    return False
                except OSError as error:
                    raise RealmIntegrityError(
                        "Retired ephemeral volume is unavailable or unsafe."
                    ) from error
                _require_directory_link(
                    root_fd,
                    retired_name,
                    wrapper_fd,
                    (identity.wrapper_device_id, identity.wrapper_inode),
                    "retired ephemeral volume wrapper",
                )
                _require_safe_private_directory(
                    os.fstat(wrapper_fd), binding.device_id, "retired volume wrapper"
                )
                _acquire_descriptor_lock(wrapper_fd)

            _remove_provider_initialization_entries(wrapper_fd)
            entries = set(os.listdir(wrapper_fd))
            if tombstone is None:
                if _CLAIM_NAME not in entries:
                    raise RealmIntegrityError(
                        "Retired ephemeral volume lost its immutable claim."
                    )
                _validate_claim(wrapper_fd, claim)
                if not entries.issubset({_CLAIM_NAME, _DATA_NAME}):
                    raise RealmIntegrityError(
                        "Retired ephemeral volume contains unclaimed entries."
                    )
                if data_fd is None and _DATA_NAME in entries:
                    try:
                        data_fd = os.open(
                            _DATA_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd
                        )
                    except OSError as error:
                        raise RealmIntegrityError(
                            "Retired ephemeral volume data is unavailable or unsafe."
                        ) from error
                if data_fd is not None:
                    if identity.data_device_id is None or identity.data_inode is None:
                        raise RealmIntegrityError(
                            "Ephemeral volume cleanup lacks exact data identity."
                        )
                    data_info = os.fstat(data_fd)
                    expected = (identity.data_device_id, identity.data_inode)
                    _require_directory_link(
                        wrapper_fd,
                        _DATA_NAME,
                        data_fd,
                        expected,
                        "ephemeral volume data directory",
                    )
                    if data_info.st_dev != identity.wrapper_device_id:
                        raise RealmIntegrityError(
                            "Ephemeral volume cleanup crossed a filesystem boundary."
                        )
                    _remove_tree_contents(data_fd, expected_device=data_info.st_dev)
                    os.fsync(data_fd)
                    _require_directory_link(
                        wrapper_fd,
                        _DATA_NAME,
                        data_fd,
                        expected,
                        "ephemeral volume data directory",
                    )
                    os.rmdir(_DATA_NAME, dir_fd=wrapper_fd)
                    os.fsync(wrapper_fd)
                    os.close(data_fd)
                    data_fd = None
                entries = set(os.listdir(wrapper_fd))
                if entries != {_CLAIM_NAME}:
                    raise RealmIntegrityError(
                        "Retired ephemeral volume changed before tombstoning."
                    )
                _validate_claim(wrapper_fd, claim)
                _publish_or_validate_cleanup_marker(
                    root_fd,
                    tombstone_name,
                    expected_format=_CLEANUP_TOMBSTONE_SCHEMA,
                    claim=claim,
                    cleanup_token=cleanup_token,
                    identity=identity,
                    label="ephemeral volume cleanup tombstone",
                )
                tombstone = identity
            else:
                if _DATA_NAME in entries:
                    raise RealmIntegrityError(
                        "Tombstoned ephemeral volume unexpectedly contains data."
                    )
                if not entries.issubset({_CLAIM_NAME}):
                    raise RealmIntegrityError(
                        "Tombstoned ephemeral volume contains unknown entries."
                    )

            if _CLAIM_NAME in set(os.listdir(wrapper_fd)):
                _validate_claim(wrapper_fd, claim)
                os.unlink(_CLAIM_NAME, dir_fd=wrapper_fd)
                os.fsync(wrapper_fd)
            if os.listdir(wrapper_fd):
                raise RealmIntegrityError(
                    "Retired ephemeral volume wrapper changed during cleanup."
                )
            _require_directory_link(
                root_fd,
                retired_name,
                wrapper_fd,
                (identity.wrapper_device_id, identity.wrapper_inode),
                "retired ephemeral volume wrapper",
            )
            os.rmdir(retired_name, dir_fd=root_fd)
            os.fsync(root_fd)
            os.close(wrapper_fd)
            wrapper_fd = None
            return True
    except RealmIntegrityError:
        raise
    except OSError as error:
        raise RealmIntegrityError(
            "Ephemeral volume cleanup could not prove an exact removable namespace."
        ) from error
    finally:
        if data_fd is not None:
            os.close(data_fd)
        if wrapper_fd is not None:
            os.close(wrapper_fd)
        os.close(root_fd)


def complete_ephemeral_volume_cleanup_namespace(
    binding: EphemeralVolumeRootBinding,
    claim: EphemeralVolumeNamespaceClaim,
    *,
    cleanup_token: str,
) -> None:
    """Remove local cleanup proofs only after durable ledger completion."""

    _lower_hex_digest(cleanup_token, "ephemeral volume cleanup token")
    _require_claim_binding(binding, claim)
    root_fd = _open_directory(binding.path)
    try:
        _validate_root_descriptor(binding, root_fd)
        with _root_lock(root_fd, exclusive=True):
            _publish_or_validate_retirement(root_fd, claim, cleanup_token)
            proof = _load_optional_cleanup_marker(
                root_fd,
                _retirement_proof_name(claim, cleanup_token),
                expected_format=_RETIREMENT_PROOF_SCHEMA,
                claim=claim,
                cleanup_token=cleanup_token,
                directory_name=None,
                label="ephemeral volume retirement proof",
            )
            tombstone = _load_optional_cleanup_marker(
                root_fd,
                _tombstone_name(claim, cleanup_token),
                expected_format=_CLEANUP_TOMBSTONE_SCHEMA,
                claim=claim,
                cleanup_token=cleanup_token,
                directory_name=None,
                label="ephemeral volume cleanup tombstone",
            )
            if proof is not None and tombstone is not None and proof != tombstone:
                raise RealmIntegrityError(
                    "Ephemeral volume cleanup proofs disagree about namespace identity."
                )
            for name in (
                _tombstone_name(claim, cleanup_token),
                _retirement_proof_name(claim, cleanup_token),
            ):
                try:
                    os.unlink(name, dir_fd=root_fd)
                except FileNotFoundError:
                    continue
                os.fsync(root_fd)
    finally:
        os.close(root_fd)


def _require_claim_binding(
    binding: EphemeralVolumeRootBinding,
    claim: EphemeralVolumeNamespaceClaim,
) -> None:
    if (
        claim.realm_id != binding.realm_id
        or claim.volume_root_id != binding.volume_root_id
    ):
        raise RealmIntegrityError("Ephemeral volume claim belongs to another root.")


def _validate_root_descriptor(
    binding: EphemeralVolumeRootBinding, root_fd: int
) -> None:
    path_info = os.stat(binding.path, follow_symlinks=False)
    opened = os.fstat(root_fd)
    expected = (binding.device_id, binding.inode)
    if (
        not stat.S_ISDIR(path_info.st_mode)
        or (path_info.st_dev, path_info.st_ino) != expected
        or (opened.st_dev, opened.st_ino) != expected
    ):
        raise RealmIntegrityError("Ephemeral volume root path identity changed.")
    _require_safe_private_directory(opened, binding.device_id, "volume root")
    marker = _read_canonical_marker(
        root_fd, _ROOT_MARKER_NAME, label="ephemeral volume root marker"
    )
    if marker != {
        "format": _ROOT_SCHEMA,
        "realm_id": binding.realm_id,
        "volume_root_id": binding.volume_root_id,
        "claim_nonce": binding.claim_nonce,
    }:
        raise RealmIntegrityError("Ephemeral volume root marker identity changed.")


def _load_or_create_root_marker(
    root_fd: int, *, realm_id: str
) -> dict[str, object]:
    _require_root_kind_available(root_fd)
    try:
        os.stat(_ROOT_MARKER_NAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        payload = canonical_json_bytes(
            {
                "format": _ROOT_SCHEMA,
                "realm_id": realm_id,
                "volume_root_id": f"ephemeral-volume-root-{uuid.uuid4().hex}",
                "claim_nonce": secrets.token_hex(32),
            }
        )
        temporary = f".ephemeral-volume-root-{uuid.uuid4().hex}.tmp"
        try:
            _write_file_exclusive(root_fd, temporary, payload, mode=0o400)
            try:
                os.link(
                    temporary,
                    _ROOT_MARKER_NAME,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                    follow_symlinks=False,
                )
                os.fsync(root_fd)
            except FileExistsError:
                pass
        finally:
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            os.fsync(root_fd)
    _repair_root_marker_temps(root_fd)
    marker = _read_canonical_marker(
        root_fd, _ROOT_MARKER_NAME, label="ephemeral volume root marker"
    )
    if set(marker) != {"format", "realm_id", "volume_root_id", "claim_nonce"}:
        raise RealmIntegrityError("Ephemeral volume root marker has an invalid shape.")
    if marker["format"] != _ROOT_SCHEMA or marker["realm_id"] != realm_id:
        raise RealmIntegrityError("Ephemeral volume root belongs to another realm.")
    _required_text(marker["volume_root_id"], "ephemeral volume root marker id")
    _lower_hex_digest(marker["claim_nonce"], "ephemeral volume root marker nonce")
    return marker


def _require_root_kind_available(root_fd: int) -> None:
    try:
        os.stat(
            _OPPOSITE_ROOT_MARKER_NAME,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise RealmIntegrityError(
            "Ephemeral volume root kind cannot be inspected safely."
        ) from error
    raise RealmConflict(
        "A read-only projection root cannot also be used as a writable volume root."
    )


def _repair_root_marker_temps(root_fd: int) -> None:
    marker = os.stat(_ROOT_MARKER_NAME, dir_fd=root_fd, follow_symlinks=False)
    if not _is_private_read_only_file(marker):
        raise RealmIntegrityError("Ephemeral volume root marker is unsafe.")
    changed = False
    for name in os.listdir(root_fd):
        if not (
            name.startswith(".ephemeral-volume-root-") and name.endswith(".tmp")
        ):
            continue
        candidate = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if not _is_private_read_only_file(candidate):
            raise RealmIntegrityError(
                "Ephemeral volume root marker temporary file is unsafe."
            )
        same_marker = (candidate.st_dev, candidate.st_ino) == (
            marker.st_dev,
            marker.st_ino,
        )
        if not same_marker and candidate.st_nlink != 1:
            raise RealmIntegrityError(
                "Ephemeral volume root marker temporary file has unknown aliases."
            )
        os.unlink(name, dir_fd=root_fd)
        changed = True
    if changed:
        os.fsync(root_fd)
    marker = os.stat(_ROOT_MARKER_NAME, dir_fd=root_fd, follow_symlinks=False)
    if not _is_private_read_only_file(marker) or marker.st_nlink != 1:
        raise RealmIntegrityError("Ephemeral volume root marker has unsafe aliases.")


def _validate_claim(
    wrapper_fd: int, expected: EphemeralVolumeNamespaceClaim
) -> None:
    marker = _read_canonical_marker(
        wrapper_fd, _CLAIM_NAME, label="ephemeral volume claim"
    )
    if marker != expected.to_dict():
        raise RealmIntegrityError("Ephemeral volume claim identity changed.")


def _retirement_name(claim: EphemeralVolumeNamespaceClaim) -> str:
    digest = hashlib.sha256(canonical_json_bytes(claim.to_dict())).hexdigest()
    return f".ephemeral-volume-retired-{digest}.json"


def _build_name(claim: EphemeralVolumeNamespaceClaim) -> str:
    digest = hashlib.sha256(
        b"optpilot/ephemeral-volume-build/v1\0"
        + canonical_json_bytes(claim.to_dict())
    ).hexdigest()
    return f".ephemeral-volume-build-{digest}"


def _retired_namespace_name(
    claim: EphemeralVolumeNamespaceClaim, cleanup_token: str
) -> str:
    digest = hashlib.sha256(
        b"optpilot/ephemeral-volume-retiring/v1\0"
        + canonical_json_bytes(claim.to_dict())
        + b"\0"
        + cleanup_token.encode("ascii")
    ).hexdigest()
    return f".ephemeral-volume-retiring-{digest}"


def _retirement_proof_name(
    claim: EphemeralVolumeNamespaceClaim, cleanup_token: str
) -> str:
    digest = hashlib.sha256(
        b"optpilot/ephemeral-volume-retirement-proof/v1\0"
        + canonical_json_bytes(claim.to_dict())
        + b"\0"
        + cleanup_token.encode("ascii")
    ).hexdigest()
    return f".ephemeral-volume-retirement-proof-{digest}.json"


def _cleanup_marker_payload(
    *,
    marker_format: str,
    claim: EphemeralVolumeNamespaceClaim,
    cleanup_token: str,
    identity: EphemeralVolumeNamespaceIdentity,
) -> dict[str, object]:
    return {
        "format": marker_format,
        "claim": claim.to_dict(),
        "cleanup_token": cleanup_token,
        "retired_name": _retired_namespace_name(claim, cleanup_token),
        "identity": identity.operational_record(),
    }


def _identity_from_cleanup_marker(
    value: dict[str, object],
    *,
    expected_format: str,
    claim: EphemeralVolumeNamespaceClaim,
    cleanup_token: str,
    directory_name: Optional[str],
    label: str,
) -> EphemeralVolumeNamespaceIdentity:
    if set(value) != {
        "format",
        "claim",
        "cleanup_token",
        "retired_name",
        "identity",
    }:
        raise RealmIntegrityError(f"{label.capitalize()} has an invalid shape.")
    if (
        value["format"] != expected_format
        or value["claim"] != claim.to_dict()
        or value["cleanup_token"] != cleanup_token
        or value["retired_name"]
        != _retired_namespace_name(claim, cleanup_token)
    ):
        raise RealmIntegrityError(f"{label.capitalize()} has a different identity.")
    raw_identity = value["identity"]
    if not isinstance(raw_identity, dict) or set(raw_identity) != {
        "directory_name",
        "wrapper_device_id",
        "wrapper_inode",
        "data_device_id",
        "data_inode",
    }:
        raise RealmIntegrityError(f"{label.capitalize()} identity is malformed.")
    try:
        identity = EphemeralVolumeNamespaceIdentity(
            directory_name=_safe_component(
                raw_identity["directory_name"],
                "cleanup marker directory name",
            ),
            wrapper_device_id=_nonnegative_int(
                raw_identity["wrapper_device_id"],
                "cleanup marker wrapper device id",
            ),
            wrapper_inode=_positive_int(
                raw_identity["wrapper_inode"],
                "cleanup marker wrapper inode",
            ),
            data_device_id=(
                None
                if raw_identity["data_device_id"] is None
                else _nonnegative_int(
                    raw_identity["data_device_id"],
                    "cleanup marker data device id",
                )
            ),
            data_inode=(
                None
                if raw_identity["data_inode"] is None
                else _positive_int(
                    raw_identity["data_inode"],
                    "cleanup marker data inode",
                )
            ),
        )
    except ValueError as error:
        raise RealmIntegrityError(f"{label.capitalize()} identity is malformed.") from error
    if directory_name is not None and identity.directory_name != directory_name:
        raise RealmIntegrityError(f"{label.capitalize()} names another namespace.")
    return identity


def _load_optional_cleanup_marker(
    root_fd: int,
    name: str,
    *,
    expected_format: str,
    claim: EphemeralVolumeNamespaceClaim,
    cleanup_token: str,
    directory_name: Optional[str],
    label: str,
) -> Optional[EphemeralVolumeNamespaceIdentity]:
    try:
        value = _read_canonical_marker(root_fd, name, label=label)
    except FileNotFoundError:
        return None
    return _identity_from_cleanup_marker(
        value,
        expected_format=expected_format,
        claim=claim,
        cleanup_token=cleanup_token,
        directory_name=directory_name,
        label=label,
    )


def _publish_or_validate_cleanup_marker(
    root_fd: int,
    name: str,
    *,
    expected_format: str,
    claim: EphemeralVolumeNamespaceClaim,
    cleanup_token: str,
    identity: EphemeralVolumeNamespaceIdentity,
    label: str,
) -> None:
    existing = _load_optional_cleanup_marker(
        root_fd,
        name,
        expected_format=expected_format,
        claim=claim,
        cleanup_token=cleanup_token,
        directory_name=identity.directory_name,
        label=label,
    )
    if existing is not None:
        if existing != identity:
            raise RealmIntegrityError(f"{label.capitalize()} has a different identity.")
        return
    expected = _cleanup_marker_payload(
        marker_format=expected_format,
        claim=claim,
        cleanup_token=cleanup_token,
        identity=identity,
    )
    try:
        _write_file_exclusive(
            root_fd, name, canonical_json_bytes(expected), mode=0o400
        )
    except FileExistsError:
        existing = _load_optional_cleanup_marker(
            root_fd,
            name,
            expected_format=expected_format,
            claim=claim,
            cleanup_token=cleanup_token,
            directory_name=identity.directory_name,
            label=label,
        )
        if existing != identity:
            raise RealmIntegrityError(
                f"{label.capitalize()} publication raced unsafely."
            )
    os.fsync(root_fd)


def _path_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _open_exact_cleanup_namespace(
    root_fd: int,
    name: str,
    *,
    claim: EphemeralVolumeNamespaceClaim,
    identity: EphemeralVolumeNamespaceIdentity,
    require_data: bool,
) -> tuple[int, Optional[int]]:
    wrapper_fd: Optional[int] = None
    data_fd: Optional[int] = None
    try:
        wrapper_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        _require_directory_link(
            root_fd,
            name,
            wrapper_fd,
            (identity.wrapper_device_id, identity.wrapper_inode),
            "ephemeral volume wrapper",
        )
        _require_safe_private_directory(
            os.fstat(wrapper_fd), os.fstat(root_fd).st_dev, "volume wrapper"
        )
        _acquire_descriptor_lock(wrapper_fd)
        entries = set(os.listdir(wrapper_fd))
        if _CLAIM_NAME not in entries:
            raise RealmIntegrityError("Ephemeral volume immutable claim is missing.")
        _validate_claim(wrapper_fd, claim)
        if not entries.issubset(_published_wrapper_entries()):
            raise RealmIntegrityError(
                "Ephemeral volume wrapper contains unclaimed entries."
            )
        if _DATA_NAME not in entries:
            if require_data:
                raise RealmIntegrityError(
                    "Ephemeral volume data disappeared before retirement."
                )
            return wrapper_fd, None
        if identity.data_device_id is None or identity.data_inode is None:
            raise RealmIntegrityError(
                "Ephemeral volume cleanup lacks exact data identity."
            )
        data_fd = os.open(_DATA_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd)
        _require_directory_link(
            wrapper_fd,
            _DATA_NAME,
            data_fd,
            (identity.data_device_id, identity.data_inode),
            "ephemeral volume data directory",
        )
        _require_safe_private_directory(
            os.fstat(data_fd), identity.wrapper_device_id, "volume data"
        )
        result = (wrapper_fd, data_fd)
        wrapper_fd = data_fd = None
        return result
    finally:
        if data_fd is not None:
            os.close(data_fd)
        if wrapper_fd is not None:
            os.close(wrapper_fd)


def _scan_writable_tree(
    directory_fd: int,
    *,
    quota: FilesystemQuota,
    expected_device: int,
    expected_mount_identity: tuple[object, ...],
    usage: Optional[list[int]] = None,
) -> tuple[int, int]:
    """Measure one live no-follow tree and reject observed unsafe state.

    Writable volumes may have a supervised process changing ordinary entries
    while an advisory checkpoint is running.  A directory listing is therefore
    only a set of names to inspect, not a transaction over the whole tree.
    Each name is opened no-follow and the opened descriptor is measured.  A
    name that vanishes before it can be opened is simply no longer part of this
    observation; a replacement is inspected as the object that was actually
    opened.  This is the strongest meaningful accounting for an advisory quota
    without a filesystem snapshot or cooperation from the writer.

    The volume-root, wrapper, immutable claim, and data-directory replacement
    fences are deliberately outside this function and are validated before and
    after the scan.  Descriptor device and mount checks below likewise remain
    fail-closed.  Only mutation of content *inside* the pinned data directory is
    treated as normal live-writer activity.
    """

    if usage is None:
        usage = [0, 0]
    current = os.fstat(directory_fd)
    if (
        current.st_dev != expected_device
        or _projection_mount_identity(directory_fd) != expected_mount_identity
    ):
        raise RealmIntegrityError(
            "Ephemeral volume quota scan crossed a filesystem boundary."
        )
    for name in sorted(os.listdir(directory_fd)):
        try:
            descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            # Atomic publication commonly removes its private temporary name
            # after the directory listing but before this checkpoint opens it.
            continue
        except OSError:
            # O_NOFOLLOW rejects a symlink before a descriptor exists.  If the
            # offending name vanished concurrently it is benign churn;
            # otherwise it is an observed unsupported entry and must fail.
            try:
                rejected = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                continue
            if rejected.st_dev != expected_device:
                raise RealmIntegrityError(
                    "Ephemeral volume quota scan encountered an external "
                    "filesystem."
                ) from None
            if not (
                stat.S_ISDIR(rejected.st_mode)
                or stat.S_ISREG(rejected.st_mode)
            ):
                raise RealmIntegrityError(
                    "Ephemeral volume contains an unsupported filesystem entry."
                ) from None
            raise
        try:
            opened = os.fstat(descriptor)
            if opened.st_dev != expected_device:
                raise RealmIntegrityError(
                    "Ephemeral volume quota scan encountered an external filesystem."
                )
            if not (
                stat.S_ISDIR(opened.st_mode) or stat.S_ISREG(opened.st_mode)
            ):
                raise RealmIntegrityError(
                    "Ephemeral volume contains an unsupported filesystem entry."
                )

            usage[0] += 1
            if usage[0] > quota.max_entries:
                raise RealmIntegrityError(
                    "Ephemeral volume exceeds its advisory entry quota."
                )
            if stat.S_ISDIR(opened.st_mode):
                if _projection_mount_identity(descriptor) != expected_mount_identity:
                    raise RealmIntegrityError(
                        "Ephemeral volume quota scan encountered a nested mount."
                    )
                _scan_writable_tree(
                    descriptor,
                    quota=quota,
                    expected_device=expected_device,
                    expected_mount_identity=expected_mount_identity,
                    usage=usage,
                )
            else:
                # Count the largest size observed while this descriptor is
                # pinned.  The file may still grow after the checkpoint because
                # enforcement is explicitly advisory.
                after = os.fstat(descriptor)
                size = max(int(opened.st_size), int(after.st_size))
                if size > quota.max_file_bytes:
                    raise RealmIntegrityError(
                        "Ephemeral volume exceeds its advisory per-file quota."
                    )
                usage[1] += size
                if usage[1] > quota.max_total_bytes:
                    raise RealmIntegrityError(
                        "Ephemeral volume exceeds its advisory total-byte quota."
                    )
        finally:
            os.close(descriptor)
    return usage[0], usage[1]


def _retirement_payload(
    claim: EphemeralVolumeNamespaceClaim, cleanup_token: str
) -> dict[str, object]:
    return {
        "format": _RETIREMENT_SCHEMA,
        "claim": claim.to_dict(),
        "cleanup_token": cleanup_token,
    }


def _retirement_exists(
    root_fd: int, claim: EphemeralVolumeNamespaceClaim
) -> bool:
    name = _retirement_name(claim)
    try:
        value = _read_canonical_marker(
            root_fd, name, label="ephemeral volume retirement marker"
        )
    except FileNotFoundError:
        return False
    except RealmIntegrityError:
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        raise
    if (
        set(value) != {"format", "claim", "cleanup_token"}
        or value["format"] != _RETIREMENT_SCHEMA
        or value["claim"] != claim.to_dict()
    ):
        raise RealmIntegrityError("Ephemeral volume retirement marker is malformed.")
    _lower_hex_digest(value["cleanup_token"], "retirement cleanup token")
    return True


def _publish_or_validate_retirement(
    root_fd: int,
    claim: EphemeralVolumeNamespaceClaim,
    cleanup_token: str,
) -> None:
    name = _retirement_name(claim)
    expected = _retirement_payload(claim, cleanup_token)
    if _validate_optional_marker(
        root_fd, name, expected, "ephemeral volume retirement marker"
    ):
        return
    try:
        _write_file_exclusive(
            root_fd, name, canonical_json_bytes(expected), mode=0o400
        )
    except FileExistsError:
        if not _validate_optional_marker(
            root_fd, name, expected, "ephemeral volume retirement marker"
        ):
            raise RealmIntegrityError(
                "Ephemeral volume retirement publication raced unsafely."
            )
    os.fsync(root_fd)


def _tombstone_name(
    claim: EphemeralVolumeNamespaceClaim, cleanup_token: str
) -> str:
    digest = hashlib.sha256(
        f"{claim.volume_id}/{cleanup_token}".encode("utf-8")
    ).hexdigest()
    return f".ephemeral-volume-cleanup-{digest}.json"


def _validate_optional_marker(
    directory_fd: int,
    name: str,
    expected: dict[str, object],
    label: str,
) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if _read_canonical_marker(directory_fd, name, label=label) != expected:
        raise RealmIntegrityError(f"{label.capitalize()} has a different identity.")
    return True


@contextmanager
def _root_lock(root_fd: int, *, exclusive: bool):
    if fcntl is None:  # pragma: no cover
        raise NotImplementedError("Ephemeral volume locks require POSIX flock.")
    descriptor = os.open(
        _ROOT_LOCK_NAME,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        0o600,
        dir_fd=root_fd,
    )
    locked = False
    try:
        info = os.fstat(descriptor)
        linked = os.stat(_ROOT_LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not stat.S_ISREG(linked.st_mode)
            or (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino)
            or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
        ):
            raise RealmIntegrityError("Ephemeral volume root lock is unsafe.")
        os.fchmod(descriptor, 0o600)
        os.fsync(root_fd)
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        locked = True
        linked = os.stat(_ROOT_LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
        if (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
            raise RealmIntegrityError("Ephemeral volume lock path was replaced.")
        yield
    except OSError as error:
        raise RealmIntegrityError("Ephemeral volume root lock is unavailable.") from error
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise RealmIntegrityError("Ephemeral volume directory is unavailable.") from error


def _read_canonical_marker(
    directory_fd: int, name: str, *, label: str
) -> dict[str, object]:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise RealmIntegrityError(f"{label.capitalize()} is unreadable.") from error
    if not _is_private_read_only_file(info) or info.st_nlink != 1:
        raise RealmIntegrityError(f"{label.capitalize()} has an unsafe file type.")
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
        try:
            opened = os.fstat(descriptor)
            if (
                (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or not _is_private_read_only_file(opened)
                or opened.st_nlink != 1
                or opened.st_size > _MAX_MARKER_BYTES
            ):
                raise RealmIntegrityError(f"{label.capitalize()} changed while opening.")
            raw = b""
            while len(raw) <= _MAX_MARKER_BYTES:
                chunk = os.read(
                    descriptor, min(65536, _MAX_MARKER_BYTES + 1 - len(raw))
                )
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(descriptor)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
            raise ValueError("non-canonical marker")
        return value
    except RealmIntegrityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RealmIntegrityError(f"{label.capitalize()} is unreadable.") from error


def _write_file_exclusive(
    directory_fd: int, name: str, payload: bytes, *, mode: int
) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short marker write")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _published_wrapper_entries() -> set[str]:
    return {
        _CLAIM_NAME,
        _DATA_NAME,
        _INITIALIZATION_PROOF_NAME,
        _INITIALIZATION_TEMP_NAME,
    }


def _acquire_descriptor_lock(
    descriptor: int,
    *,
    progress: Callable[[], None] | None = None,
) -> None:
    """Acquire one wrapper flock while allowing lease progress when blocked."""

    if fcntl is None:  # pragma: no cover
        raise NotImplementedError("Ephemeral volume locks require POSIX flock.")
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if progress is not None:
                progress()
            time.sleep(0.001)
        except OSError as error:
            raise RealmIntegrityError(
                "Ephemeral volume initialization lock is unavailable."
            ) from error


def _release_descriptor_lock(descriptor: int) -> None:
    if fcntl is None:  # pragma: no cover
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError as error:
        raise RealmIntegrityError(
            "Ephemeral volume initialization lock could not be released."
        ) from error


def _remove_safe_initialization_temp(wrapper_fd: int) -> None:
    try:
        info = os.stat(
            _INITIALIZATION_TEMP_NAME,
            dir_fd=wrapper_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
        or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
    ):
        raise RealmIntegrityError(
            "Ephemeral volume initialization temporary marker is unsafe."
        )
    os.unlink(_INITIALIZATION_TEMP_NAME, dir_fd=wrapper_fd)
    os.fsync(wrapper_fd)


def _require_initialization_temp_absent(wrapper_fd: int) -> None:
    try:
        os.stat(
            _INITIALIZATION_TEMP_NAME,
            dir_fd=wrapper_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    raise RealmIntegrityError(
        "Ephemeral volume initialization proof has a leftover temporary marker."
    )


def _remove_provider_initialization_entries(wrapper_fd: int) -> None:
    """Remove only the two fixed provider-private entries during retirement."""

    _remove_safe_initialization_temp(wrapper_fd)
    try:
        _read_canonical_marker(
            wrapper_fd,
            _INITIALIZATION_PROOF_NAME,
            label="ephemeral volume initialization proof",
        )
    except FileNotFoundError:
        return
    os.unlink(_INITIALIZATION_PROOF_NAME, dir_fd=wrapper_fd)
    os.fsync(wrapper_fd)


def _require_directory_link(
    parent_fd: int,
    name: str,
    opened_fd: int,
    expected: tuple[int, int],
    label: str,
) -> None:
    try:
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise RealmIntegrityError(f"{label.capitalize()} disappeared.") from error
    opened = os.fstat(opened_fd)
    if (
        not stat.S_ISDIR(linked.st_mode)
        or (linked.st_dev, linked.st_ino) != expected
        or (opened.st_dev, opened.st_ino) != expected
    ):
        raise RealmIntegrityError(f"{label.capitalize()} path was replaced.")


def _require_safe_private_directory(
    info: os.stat_result, expected_device: int, label: str
) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_dev != expected_device
        or stat.S_IMODE(info.st_mode) & 0o077
        or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
    ):
        raise RealmIntegrityError(f"Ephemeral {label} has unsafe identity or permissions.")


def _is_private_read_only_file(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o400
        and (not hasattr(os, "geteuid") or info.st_uid == os.geteuid())
    )


def _safe_component(value: object, label: str) -> str:
    text = _required_text(value, label)
    if text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"{label} must be one safe path component.")
    return text


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be nonempty text.")
    return value


def _lower_hex_digest(value: object, label: str) -> str:
    text = _required_text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
    return text


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


__all__ = [
    "AttachedEphemeralVolumeNamespace",
    "EphemeralVolumeNamespaceClaim",
    "EphemeralVolumeNamespaceIdentity",
    "EphemeralVolumeRootBinding",
    "LOCAL_DIRECTORY_VOLUME_PROVIDER",
    "attach_ephemeral_volume_namespace",
    "cleanup_ephemeral_volume_namespace",
    "complete_ephemeral_volume_cleanup_namespace",
    "create_ephemeral_volume_namespace",
    "find_ephemeral_volume_namespace_identity",
    "observe_active_ephemeral_volume_namespace_identity",
    "prepare_ephemeral_volume_root",
    "validate_ephemeral_volume_root",
]
