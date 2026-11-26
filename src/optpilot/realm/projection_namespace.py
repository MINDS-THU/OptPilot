"""Descriptor-safe operational namespaces for durable projections.

Portable projection identity remains the immutable spec and content refs.  This
module owns only local realization facts: a nonce-bound registered root and a
private wrapper containing ``claim.json`` plus the provider-created ``root/``
tree.  The wrapper marker is outside the exposed tree, so projection contents
remain an exact :class:`TreePlan`.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import prepare_private_directory
from .errors import RealmConflict, RealmIntegrityError
from .projection import _remove_tree_contents
from .refs import canonical_json_bytes

try:
    import fcntl
except ImportError:  # pragma: no cover - secure namespace v1 is POSIX-only
    fcntl = None  # type: ignore[assignment]


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
_ROOT_MARKER_NAME = ".optpilot-projection-root"
_OPPOSITE_ROOT_MARKER_NAME = ".optpilot-ephemeral-volume-root"
_REALIZATION_CLAIM_NAME = "claim.json"
_EXPOSED_TREE_NAME = "root"
_ROOT_LOCK_NAME = ".optpilot-storage-root.lock"
_RETIREMENT_MARKER_PREFIX = ".projection-retired-"
_RETIRED_NAMESPACE_PREFIX = ".projection-retiring-"
_RETIREMENT_PROOF_PREFIX = ".projection-retirement-proof-"
_ROOT_MARKER_SCHEMA = "optpilot.projection-root.v1"
_REALIZATION_CLAIM_SCHEMA = "optpilot.projection-claim.v1"
_RETIREMENT_MARKER_SCHEMA = "optpilot.projection-retirement.v1"
_RETIREMENT_PROOF_SCHEMA = "optpilot.projection-retirement-proof.v1"
_MAX_MARKER_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProjectionRootBinding:
    """One local configured path bound to a durable root marker and inode."""

    path: Path
    realm_id: str
    projection_root_id: str
    claim_nonce: str
    device_id: int
    inode: int

    def __post_init__(self) -> None:
        path = Path(self.path).expanduser().absolute()
        if not path.is_absolute():  # pragma: no cover - absolute() guarantees it
            raise ValueError("Projection root path must be absolute.")
        object.__setattr__(self, "path", path)
        for label, value in (
            ("realm_id", self.realm_id),
            ("projection_root_id", self.projection_root_id),
        ):
            _required_text(value, label)
        _lower_hex_digest(self.claim_nonce, "claim_nonce")
        _nonnegative_int(self.device_id, "root device id")
        _positive_int(self.inode, "root inode")

    def operational_record(self) -> dict[str, object]:
        return {
            "projection_root_id": self.projection_root_id,
            "canonical_path": str(self.path),
            "realm_id": self.realm_id,
            "claim_nonce": self.claim_nonce,
            "device_id": self.device_id,
            "inode": self.inode,
        }

    def portable_record(self) -> dict[str, object]:
        """A local realization root contributes no portable semantic identity."""

        return {}


@dataclass(frozen=True)
class ProjectionNamespaceClaim:
    realm_id: str
    projection_root_id: str
    realization_id: str
    claim_nonce: str

    def __post_init__(self) -> None:
        for label, value in (
            ("realm_id", self.realm_id),
            ("projection_root_id", self.projection_root_id),
            ("realization_id", self.realization_id),
        ):
            _required_text(value, label)
        _lower_hex_digest(self.claim_nonce, "claim_nonce")

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _REALIZATION_CLAIM_SCHEMA,
            "realm_id": self.realm_id,
            "projection_root_id": self.projection_root_id,
            "realization_id": self.realization_id,
            "claim_nonce": self.claim_nonce,
        }


@dataclass(frozen=True)
class ProjectionNamespaceIdentity:
    directory_name: str
    wrapper_device_id: int
    wrapper_inode: int
    tree_device_id: Optional[int] = None
    tree_inode: Optional[int] = None

    def __post_init__(self) -> None:
        _safe_component(self.directory_name, "projection directory name")
        _nonnegative_int(self.wrapper_device_id, "wrapper device id")
        _positive_int(self.wrapper_inode, "wrapper inode")
        if (self.tree_device_id is None) != (self.tree_inode is None):
            raise ValueError("Tree device and inode must be recorded together.")
        if self.tree_device_id is not None:
            _nonnegative_int(self.tree_device_id, "tree device id")
            _positive_int(self.tree_inode, "tree inode")

    def with_tree(self, *, device_id: int, inode: int) -> "ProjectionNamespaceIdentity":
        return ProjectionNamespaceIdentity(
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
            "tree_device_id": self.tree_device_id,
            "tree_inode": self.tree_inode,
        }


class AttachedProjectionNamespace:
    """Pinned no-follow view of one exact ready wrapper and exposed tree."""

    def __init__(
        self,
        *,
        binding: ProjectionRootBinding,
        claim: ProjectionNamespaceClaim,
        identity: ProjectionNamespaceIdentity,
        root_fd: int,
        wrapper_fd: int,
        tree_fd: int,
    ) -> None:
        self.binding = binding
        self.claim = claim
        self.identity = identity
        self._root_fd: Optional[int] = root_fd
        self._wrapper_fd: Optional[int] = wrapper_fd
        self._tree_fd: Optional[int] = tree_fd

    @property
    def root_path(self) -> Path:
        self.validate()
        return self.binding.path / self.identity.directory_name / _EXPOSED_TREE_NAME

    @property
    def closed(self) -> bool:
        return self._root_fd is None

    def validate(self) -> None:
        if self._root_fd is None or self._wrapper_fd is None or self._tree_fd is None:
            raise RealmIntegrityError("Projection namespace attachment is closed.")
        _validate_root_descriptor(self.binding, self._root_fd)
        _require_directory_link(
            self._root_fd,
            self.identity.directory_name,
            self._wrapper_fd,
            (self.identity.wrapper_device_id, self.identity.wrapper_inode),
            "projection wrapper",
        )
        _validate_claim(self._wrapper_fd, self.claim)
        assert self.identity.tree_device_id is not None
        assert self.identity.tree_inode is not None
        _require_directory_link(
            self._wrapper_fd,
            _EXPOSED_TREE_NAME,
            self._tree_fd,
            (self.identity.tree_device_id, self.identity.tree_inode),
            "projection tree",
        )

    def close(self) -> None:
        for name in ("_tree_fd", "_wrapper_fd", "_root_fd"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    def __enter__(self) -> "AttachedProjectionNamespace":
        self.validate()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def prepare_projection_root(path: Path, *, realm_id: str) -> ProjectionRootBinding:
    """Create or reopen one private, nonce-bound projection root."""

    if os.name == "nt":  # pragma: no cover - secure namespace v1 is POSIX-only
        raise NotImplementedError(
            "The managed verified-copy projection namespace requires POSIX descriptors."
        )
    _required_text(realm_id, "realm_id")
    root = prepare_private_directory(path)
    root_fd = _open_directory(root)
    try:
        # Marker publication uses a hard-link commit.  Serialize both that
        # commit and recovery of either possible temporary-file crash state.
        with _root_lock(root_fd, exclusive=True):
            marker = _load_or_create_root_marker(root_fd, realm_id=realm_id)
        info = os.fstat(root_fd)
        return ProjectionRootBinding(
            path=root,
            realm_id=realm_id,
            projection_root_id=str(marker["projection_root_id"]),
            claim_nonce=str(marker["claim_nonce"]),
            device_id=info.st_dev,
            inode=info.st_ino,
        )
    finally:
        os.close(root_fd)


def validate_projection_root(binding: ProjectionRootBinding) -> None:
    root_fd = _open_directory(binding.path)
    try:
        _validate_root_descriptor(binding, root_fd)
    finally:
        os.close(root_fd)


def create_projection_wrapper(
    binding: ProjectionRootBinding,
    *,
    directory_name: str,
    realization_id: str,
    claim_nonce: str,
) -> tuple[ProjectionNamespaceClaim, ProjectionNamespaceIdentity]:
    """Create one permanently unique wrapper and durable claim marker."""

    _safe_component(directory_name, "projection directory name")
    claim = ProjectionNamespaceClaim(
        binding.realm_id,
        binding.projection_root_id,
        realization_id,
        claim_nonce,
    )
    root_fd = _open_directory(binding.path)
    wrapper_fd: Optional[int] = None
    created = False
    created_identity: Optional[tuple[int, int]] = None
    try:
        _validate_root_descriptor(binding, root_fd)
        with _root_lock(root_fd, exclusive=True):
            retirement_name = _retirement_marker_name(claim)
            if _validate_optional_retired_claim(root_fd, retirement_name, claim):
                raise RealmConflict(
                    "Projection realization claim is permanently retired."
                )
            try:
                os.mkdir(directory_name, 0o700, dir_fd=root_fd)
                created = True
                created_info = os.stat(
                    directory_name, dir_fd=root_fd, follow_symlinks=False
                )
                created_identity = (created_info.st_dev, created_info.st_ino)
            except FileExistsError:
                created = False
            wrapper_fd = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
            info = os.fstat(wrapper_fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_dev != binding.device_id
                or (created_identity is not None
                    and (info.st_dev, info.st_ino) != created_identity)
            ):
                raise RealmIntegrityError("Projection wrapper has an unsafe identity.")
            if created:
                _write_file_exclusive(
                    wrapper_fd,
                    _REALIZATION_CLAIM_NAME,
                    canonical_json_bytes(claim.to_dict()),
                    mode=0o400,
                )
            else:
                try:
                    _validate_claim(wrapper_fd, claim)
                except RealmIntegrityError as error:
                    raise RealmConflict(
                        "Projection wrapper destination belongs to another or incomplete claim."
                    ) from error
            os.fsync(wrapper_fd)
            _require_directory_link(
                root_fd,
                directory_name,
                wrapper_fd,
                (info.st_dev, info.st_ino),
                "projection wrapper",
            )
            os.fsync(root_fd)
            _validate_claim(wrapper_fd, claim)
            return claim, ProjectionNamespaceIdentity(
                directory_name, info.st_dev, info.st_ino
            )
    except BaseException:
        if created and wrapper_fd is not None and created_identity is not None:
            try:
                _rollback_created_wrapper(
                    root_fd,
                    wrapper_fd,
                    directory_name=directory_name,
                    expected_identity=created_identity,
                )
            except (OSError, RealmIntegrityError):
                # An incomplete, nonempty, or replaced wrapper is recovery
                # debt.  Never remove a name after losing exact-link proof.
                pass
        raise
    finally:
        if wrapper_fd is not None:
            os.close(wrapper_fd)
        os.close(root_fd)


def find_projection_wrapper_identity(
    binding: ProjectionRootBinding,
    claim: ProjectionNamespaceClaim,
    *,
    directory_name: str,
    cleanup_token: Optional[str] = None,
) -> Optional[ProjectionNamespaceIdentity]:
    """Return one exact existing claimed wrapper without creating one.

    Ordinary inspection may return ``None`` for an absent public name.  Cleanup
    recovery is stricter: absence is accepted only when a durable retirement
    proof identifies the exact wrapper previously moved to its deterministic
    private cleanup name.  Mere public-name absence could instead mean that the
    live realization was renamed away, so it is never cleanup success.
    """

    _safe_component(directory_name, "projection directory name")
    if cleanup_token is not None:
        _lower_hex_digest(cleanup_token, "cleanup_token")
    if (
        claim.realm_id != binding.realm_id
        or claim.projection_root_id != binding.projection_root_id
    ):
        raise RealmIntegrityError("Projection claim belongs to another root or realm.")
    root_fd = _open_directory(binding.path)
    wrapper_fd: Optional[int] = None
    tree_fd: Optional[int] = None
    try:
        _validate_root_descriptor(binding, root_fd)
        with _root_lock(
            root_fd, exclusive=True, create=cleanup_token is not None
        ):
            candidate_name = directory_name
            if cleanup_token is not None:
                _publish_or_validate_retirement_marker(
                    root_fd,
                    _retirement_marker_name(claim),
                    _retirement_marker_payload(claim, cleanup_token),
                )
                proof = _load_optional_retirement_proof(
                    root_fd,
                    claim=claim,
                    cleanup_token=cleanup_token,
                    directory_name=directory_name,
                )
                if proof is not None:
                    if _path_exists(root_fd, directory_name):
                        raise RealmIntegrityError(
                            "A retired projection reappeared at its public name."
                        )
                    return proof
                retired_name = _retired_namespace_name(claim, cleanup_token)
                public_exists = _path_exists(root_fd, directory_name)
                retired_exists = _path_exists(root_fd, retired_name)
                if public_exists and retired_exists:
                    raise RealmIntegrityError(
                        "Projection occupies both public and retirement names."
                    )
                if not public_exists and not retired_exists:
                    raise RealmIntegrityError(
                        "Projection namespace is absent without exact retirement proof."
                    )
                candidate_name = directory_name if public_exists else retired_name
            try:
                wrapper_fd = os.open(
                    candidate_name, _DIRECTORY_FLAGS, dir_fd=root_fd
                )
            except FileNotFoundError:
                os.fsync(root_fd)
                try:
                    os.stat(candidate_name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if cleanup_token is not None:
                        raise RealmIntegrityError(
                            "Projection namespace disappeared without exact retirement proof."
                        )
                    return None
                raise RealmIntegrityError(
                    "Projection wrapper appeared while proving its absence."
                )
            except OSError as error:
                raise RealmIntegrityError(
                    "Projection wrapper exists but is unavailable or unsafe."
                ) from error
            info = os.fstat(wrapper_fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_dev != binding.device_id
                or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise RealmIntegrityError(
                    "Projection wrapper has an unsafe identity or permissions."
                )
            try:
                _validate_claim(wrapper_fd, claim)
            except RealmIntegrityError as error:
                if cleanup_token is not None:
                    raise RealmIntegrityError(
                        "Projection cleanup found a different or incomplete claim."
                    ) from error
                raise RealmConflict(
                    "Projection wrapper destination belongs to another or incomplete claim."
                ) from error
            identity = ProjectionNamespaceIdentity(
                directory_name, info.st_dev, info.st_ino
            )
            _require_directory_link(
                root_fd,
                candidate_name,
                wrapper_fd,
                (identity.wrapper_device_id, identity.wrapper_inode),
                "projection wrapper",
            )
            if cleanup_token is not None:
                # Capture the tree through the same root-lock acquisition that
                # proved the public-or-retired wrapper.  A concurrent exact
                # cleanup may remove the wrapper as soon as this lock is
                # released, so cleanup recovery must not reopen the public
                # name merely to learn these attachment-local descriptors.
                try:
                    tree_fd = os.open(
                        _EXPOSED_TREE_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd
                    )
                except FileNotFoundError:
                    tree_fd = None
                except OSError as error:
                    raise RealmIntegrityError(
                        "Projection tree exists but is unavailable or unsafe."
                    ) from error
                if tree_fd is not None:
                    tree_info = os.fstat(tree_fd)
                    if (
                        not stat.S_ISDIR(tree_info.st_mode)
                        or tree_info.st_dev != identity.wrapper_device_id
                        or stat.S_IMODE(tree_info.st_mode) & 0o222
                    ):
                        raise RealmIntegrityError(
                            "Projection tree has an unsafe identity."
                        )
                    _require_directory_link(
                        wrapper_fd,
                        _EXPOSED_TREE_NAME,
                        tree_fd,
                        (tree_info.st_dev, tree_info.st_ino),
                        "projection tree",
                    )
                    identity = identity.with_tree(
                        device_id=tree_info.st_dev,
                        inode=tree_info.st_ino,
                    )
                    os.fsync(tree_fd)
            os.fsync(wrapper_fd)
            os.fsync(root_fd)
            _require_directory_link(
                root_fd,
                candidate_name,
                wrapper_fd,
                (identity.wrapper_device_id, identity.wrapper_inode),
                "projection wrapper",
            )
            return identity
    except (RealmConflict, RealmIntegrityError):
        raise
    except OSError as error:
        raise RealmIntegrityError(
            "Projection wrapper identity could not be proven safely."
        ) from error
    finally:
        if tree_fd is not None:
            os.close(tree_fd)
        if wrapper_fd is not None:
            os.close(wrapper_fd)
        os.close(root_fd)


def record_projection_tree_identity(
    binding: ProjectionRootBinding,
    claim: ProjectionNamespaceClaim,
    identity: ProjectionNamespaceIdentity,
) -> ProjectionNamespaceIdentity:
    """Pin the provider-created ``root/`` identity after materialization."""

    root_fd, wrapper_fd = _open_claimed_wrapper(binding, claim, identity)
    tree_fd: Optional[int] = None
    try:
        tree_fd = os.open(_EXPOSED_TREE_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd)
        info = os.fstat(tree_fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_dev != identity.wrapper_device_id
            or stat.S_IMODE(info.st_mode) & 0o222
        ):
            raise RealmIntegrityError("Projection tree has an unsafe identity.")
        os.fsync(tree_fd)
        _require_directory_link(
            wrapper_fd,
            _EXPOSED_TREE_NAME,
            tree_fd,
            (info.st_dev, info.st_ino),
            "projection tree",
        )
        os.fsync(wrapper_fd)
        return identity.with_tree(device_id=info.st_dev, inode=info.st_ino)
    finally:
        if tree_fd is not None:
            os.close(tree_fd)
        os.close(wrapper_fd)
        os.close(root_fd)


def observe_ready_projection_namespace_identity(
    binding: ProjectionRootBinding,
    claim: ProjectionNamespaceClaim,
    *,
    directory_name: str,
) -> ProjectionNamespaceIdentity:
    """Bind a durable claim marker to this attachment's live descriptor facts."""

    identity = find_projection_wrapper_identity(
        binding,
        claim,
        directory_name=directory_name,
    )
    if identity is None:
        raise RealmIntegrityError("Projection namespace claim is absent.")
    # This validates the exact claim again through opened descriptors and
    # captures current device/inode observations for the ready tree.  Those
    # observations fence this attachment only and are never its durable id.
    return record_projection_tree_identity(binding, claim, identity)


def attach_projection_namespace(
    binding: ProjectionRootBinding,
    claim: ProjectionNamespaceClaim,
    identity: ProjectionNamespaceIdentity,
) -> AttachedProjectionNamespace:
    """Reopen one ready realization by marker and both persisted identities."""

    if identity.tree_device_id is None or identity.tree_inode is None:
        raise ValueError("A ready projection requires a recorded tree identity.")
    root_fd, wrapper_fd = _open_claimed_wrapper(binding, claim, identity)
    tree_fd: Optional[int] = None
    try:
        tree_fd = os.open(_EXPOSED_TREE_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd)
        attached = AttachedProjectionNamespace(
            binding=binding,
            claim=claim,
            identity=identity,
            root_fd=root_fd,
            wrapper_fd=wrapper_fd,
            tree_fd=tree_fd,
        )
        attached.validate()
        root_fd = wrapper_fd = tree_fd = None  # type: ignore[assignment]
        return attached
    except OSError as error:
        raise RealmIntegrityError(
            "Projection namespace could not be attached safely."
        ) from error
    finally:
        for descriptor in (tree_fd, wrapper_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def cleanup_projection_namespace(
    binding: ProjectionRootBinding,
    claim: ProjectionNamespaceClaim,
    identity: ProjectionNamespaceIdentity,
    *,
    cleanup_token: str,
) -> bool:
    """Retire and remove only the exact claimed wrapper.

    Cleanup first moves the authenticated wrapper to a token-derived private
    name and durably records that exact inode.  Recursive deletion starts only
    after that proof exists.  Therefore disappearance of the public name alone
    can never be mistaken for completed cleanup.
    """

    _lower_hex_digest(cleanup_token, "cleanup_token")
    tombstone_name = _cleanup_tombstone_name(claim, cleanup_token)
    tombstone_payload = _cleanup_tombstone_payload(claim, cleanup_token)
    retirement_name = _retirement_marker_name(claim)
    retirement_payload = _retirement_marker_payload(claim, cleanup_token)
    retired_name = _retired_namespace_name(claim, cleanup_token)
    root_fd = _open_directory(binding.path)
    wrapper_fd: Optional[int] = None
    tree_fd: Optional[int] = None
    try:
        _validate_root_descriptor(binding, root_fd)
        with _root_lock(root_fd, exclusive=True):
            _publish_or_validate_retirement_marker(
                root_fd, retirement_name, retirement_payload
            )
            tombstone_exists = _validate_optional_cleanup_tombstone(
                root_fd, tombstone_name, tombstone_payload
            )
            proof = _load_optional_retirement_proof(
                root_fd,
                claim=claim,
                cleanup_token=cleanup_token,
                directory_name=identity.directory_name,
            )
            if proof is not None and proof != identity:
                raise RealmIntegrityError(
                    "Projection retirement proof has a different namespace identity."
                )

            public_exists = _path_exists(root_fd, identity.directory_name)
            retired_exists = _path_exists(root_fd, retired_name)
            if proof is not None:
                if public_exists:
                    raise RealmIntegrityError(
                        "A retired projection reappeared at its public name."
                    )
            else:
                if public_exists and retired_exists:
                    raise RealmIntegrityError(
                        "Projection occupies both public and retirement names."
                    )
                if not public_exists and not retired_exists:
                    raise RealmIntegrityError(
                        "Projection disappeared without exact retirement proof."
                    )
                source_name = identity.directory_name if public_exists else retired_name
                try:
                    wrapper_fd = os.open(
                        source_name, _DIRECTORY_FLAGS, dir_fd=root_fd
                    )
                except OSError as error:
                    raise RealmIntegrityError(
                        "Projection wrapper exists but is unavailable or unsafe."
                    ) from error
                wrapper_info = os.fstat(wrapper_fd)
                if (
                    not stat.S_ISDIR(wrapper_info.st_mode)
                    or (wrapper_info.st_dev, wrapper_info.st_ino)
                    != (identity.wrapper_device_id, identity.wrapper_inode)
                    or wrapper_info.st_dev != binding.device_id
                    or (hasattr(os, "geteuid") and wrapper_info.st_uid != os.geteuid())
                    or stat.S_IMODE(wrapper_info.st_mode) & 0o077
                ):
                    raise RealmIntegrityError(
                        "Projection wrapper has an unsafe or unexpected identity."
                    )
                _require_directory_link(
                    root_fd,
                    source_name,
                    wrapper_fd,
                    (identity.wrapper_device_id, identity.wrapper_inode),
                    "projection wrapper",
                )
                entries = set(os.listdir(wrapper_fd))
                claim_present = _REALIZATION_CLAIM_NAME in entries
                if claim_present:
                    _validate_claim(wrapper_fd, claim)
                elif not tombstone_exists:
                    raise RealmIntegrityError(
                        "Projection wrapper lost both claim and cleanup tombstone."
                    )
                allowed = {_EXPOSED_TREE_NAME}
                if claim_present:
                    allowed.add(_REALIZATION_CLAIM_NAME)
                if not entries.issubset(allowed):
                    raise RealmIntegrityError(
                        "Projection wrapper contains unclaimed namespace entries."
                    )
                if (
                    identity.tree_device_id is not None
                    and identity.tree_inode is not None
                    and not tombstone_exists
                ):
                    try:
                        tree_fd = os.open(
                            _EXPOSED_TREE_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd
                        )
                    except OSError as error:
                        raise RealmIntegrityError(
                            "Projection tree disappeared before exact retirement."
                        ) from error
                    _require_directory_link(
                        wrapper_fd,
                        _EXPOSED_TREE_NAME,
                        tree_fd,
                        (identity.tree_device_id, identity.tree_inode),
                        "projection tree",
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
                        "retired projection wrapper",
                    )
                _publish_or_validate_retirement_proof(
                    root_fd,
                    claim=claim,
                    cleanup_token=cleanup_token,
                    identity=identity,
                )
                proof = identity

            if wrapper_fd is None:
                try:
                    wrapper_fd = os.open(
                        retired_name, _DIRECTORY_FLAGS, dir_fd=root_fd
                    )
                except FileNotFoundError:
                    if not tombstone_exists:
                        raise RealmIntegrityError(
                            "Retired projection disappeared before cleanup completed."
                        )
                    os.fsync(root_fd)
                    return False
                except OSError as error:
                    raise RealmIntegrityError(
                        "Retired projection is unavailable or unsafe."
                    ) from error
            _require_directory_link(
                root_fd,
                retired_name,
                wrapper_fd,
                (identity.wrapper_device_id, identity.wrapper_inode),
                "retired projection wrapper",
            )
            entries = set(os.listdir(wrapper_fd))
            claim_present = _REALIZATION_CLAIM_NAME in entries
            if claim_present:
                _validate_claim(wrapper_fd, claim)
            elif not tombstone_exists:
                raise RealmIntegrityError(
                    "Projection wrapper lost both claim and cleanup tombstone."
                )
            allowed = {_EXPOSED_TREE_NAME}
            if claim_present:
                allowed.add(_REALIZATION_CLAIM_NAME)
            if not entries.issubset(allowed):
                raise RealmIntegrityError(
                    "Projection wrapper contains unclaimed namespace entries."
                )
            if tombstone_exists and _EXPOSED_TREE_NAME in entries:
                raise RealmIntegrityError(
                    "Tombstoned projection unexpectedly still contains its tree."
                )
            try:
                if tree_fd is None:
                    tree_fd = os.open(
                        _EXPOSED_TREE_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd
                    )
            except FileNotFoundError:
                tree_fd = None
            if tree_fd is not None:
                if identity.tree_device_id is not None and identity.tree_inode is not None:
                    expected_tree_identity = (
                        identity.tree_device_id,
                        identity.tree_inode,
                    )
                    _require_directory_link(
                        wrapper_fd,
                        _EXPOSED_TREE_NAME,
                        tree_fd,
                        expected_tree_identity,
                        "projection tree",
                    )
                    expected_tree_device = identity.tree_device_id
                else:
                    linked = os.stat(
                        _EXPOSED_TREE_NAME,
                        dir_fd=wrapper_fd,
                        follow_symlinks=False,
                    )
                    opened = os.fstat(tree_fd)
                    expected_tree_identity = (opened.st_dev, opened.st_ino)
                    if (
                        not stat.S_ISDIR(linked.st_mode)
                        or (linked.st_dev, linked.st_ino)
                        != expected_tree_identity
                        or opened.st_dev != identity.wrapper_device_id
                    ):
                        raise RealmIntegrityError(
                            "Partial projection tree has an unsafe identity."
                        )
                    expected_tree_device = opened.st_dev
                _remove_tree_contents(
                    tree_fd, expected_device=expected_tree_device
                )
                os.fsync(tree_fd)
                _require_directory_link(
                    wrapper_fd,
                    _EXPOSED_TREE_NAME,
                    tree_fd,
                    expected_tree_identity,
                    "projection tree",
                )
                os.rmdir(_EXPOSED_TREE_NAME, dir_fd=wrapper_fd)
                os.fsync(wrapper_fd)
                os.close(tree_fd)
                tree_fd = None
            if claim_present:
                if tombstone_exists:
                    raise RealmIntegrityError(
                        "Projection cleanup has both a live claim and tombstone."
                    )
                _validate_claim(wrapper_fd, claim)
                os.rename(
                    _REALIZATION_CLAIM_NAME,
                    tombstone_name,
                    src_dir_fd=wrapper_fd,
                    dst_dir_fd=root_fd,
                )
                os.fsync(wrapper_fd)
                os.fsync(root_fd)
                tombstone_exists = _validate_optional_cleanup_tombstone(
                    root_fd, tombstone_name, tombstone_payload
                )
                if not tombstone_exists:  # pragma: no cover - rename just succeeded
                    raise RealmIntegrityError(
                        "Projection cleanup tombstone publication failed."
                    )
            if os.listdir(wrapper_fd):
                raise RealmIntegrityError("Projection wrapper changed during cleanup.")
            _require_directory_link(
                root_fd,
                retired_name,
                wrapper_fd,
                (identity.wrapper_device_id, identity.wrapper_inode),
                "retired projection wrapper",
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
            "Projection namespace cleanup could not prove an exact removable wrapper."
        ) from error
    finally:
        if tree_fd is not None:
            os.close(tree_fd)
        if wrapper_fd is not None:
            os.close(wrapper_fd)
        os.close(root_fd)


def complete_projection_cleanup_namespace(
    binding: ProjectionRootBinding,
    claim: ProjectionNamespaceClaim,
    *,
    cleanup_token: str,
) -> None:
    """Remove transient cleanup proofs after the ledger records ``cleaned``."""

    _lower_hex_digest(cleanup_token, "cleanup_token")
    tombstone_name = _cleanup_tombstone_name(claim, cleanup_token)
    retirement_name = _retirement_marker_name(claim)
    expected = _cleanup_tombstone_payload(claim, cleanup_token)
    retirement_expected = _retirement_marker_payload(claim, cleanup_token)
    root_fd = _open_directory(binding.path)
    try:
        _validate_root_descriptor(binding, root_fd)
        with _root_lock(root_fd, exclusive=True):
            _publish_or_validate_retirement_marker(
                root_fd, retirement_name, retirement_expected
            )
            tombstone = _validate_optional_cleanup_tombstone(
                root_fd, tombstone_name, expected
            )
            proof = _load_optional_retirement_proof(
                root_fd,
                claim=claim,
                cleanup_token=cleanup_token,
                directory_name=None,
            )
            if proof is None:
                if tombstone:
                    raise RealmIntegrityError(
                        "Projection cleanup tombstone has no retirement proof."
                    )
                return
            if _path_exists(root_fd, proof.directory_name) or _path_exists(
                root_fd, _retired_namespace_name(claim, cleanup_token)
            ):
                raise RealmIntegrityError(
                    "Projection cleanup proof still names a live namespace."
                )
            for name in (tombstone_name, _retirement_proof_name(claim, cleanup_token)):
                try:
                    os.unlink(name, dir_fd=root_fd)
                except FileNotFoundError:
                    continue
                os.fsync(root_fd)
    finally:
        os.close(root_fd)


def _open_claimed_wrapper(
    binding: ProjectionRootBinding,
    claim: ProjectionNamespaceClaim,
    identity: ProjectionNamespaceIdentity,
) -> tuple[int, int]:
    if (
        claim.realm_id != binding.realm_id
        or claim.projection_root_id != binding.projection_root_id
    ):
        raise RealmIntegrityError("Projection claim belongs to another root or realm.")
    root_fd = _open_directory(binding.path)
    wrapper_fd: Optional[int] = None
    try:
        _validate_root_descriptor(binding, root_fd)
        wrapper_fd = os.open(identity.directory_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        _require_directory_link(
            root_fd,
            identity.directory_name,
            wrapper_fd,
            (identity.wrapper_device_id, identity.wrapper_inode),
            "projection wrapper",
        )
        _validate_claim(wrapper_fd, claim)
        result = (root_fd, wrapper_fd)
        root_fd = wrapper_fd = None  # type: ignore[assignment]
        return result
    except RealmIntegrityError:
        raise
    except OSError as error:
        raise RealmIntegrityError(
            "Projection claimed wrapper is unavailable or unsafe."
        ) from error
    finally:
        if wrapper_fd is not None:
            os.close(wrapper_fd)
        if root_fd is not None:
            os.close(root_fd)


def _validate_root_descriptor(binding: ProjectionRootBinding, root_fd: int) -> None:
    try:
        path_info = os.stat(binding.path, follow_symlinks=False)
        opened = os.fstat(root_fd)
    except OSError as error:
        raise RealmIntegrityError("Projection root path identity changed.") from error
    expected = (binding.device_id, binding.inode)
    if (
        not stat.S_ISDIR(path_info.st_mode)
        or (path_info.st_dev, path_info.st_ino) != expected
        or (opened.st_dev, opened.st_ino) != expected
    ):
        raise RealmIntegrityError("Projection root path identity changed.")
    if os.name != "nt" and (
        (hasattr(os, "geteuid") and opened.st_uid != os.geteuid())
        or stat.S_IMODE(opened.st_mode) & 0o077
    ):
        raise RealmIntegrityError(
            "Projection root ownership or private permissions changed."
        )
    marker = _read_canonical_marker(
        root_fd, _ROOT_MARKER_NAME, label="projection root marker"
    )
    expected_marker = {
        "format": _ROOT_MARKER_SCHEMA,
        "realm_id": binding.realm_id,
        "projection_root_id": binding.projection_root_id,
        "claim_nonce": binding.claim_nonce,
    }
    if marker != expected_marker:
        raise RealmIntegrityError("Projection root marker identity changed.")


def _load_or_create_root_marker(root_fd: int, *, realm_id: str) -> dict[str, object]:
    _require_root_kind_available(root_fd)
    try:
        os.stat(_ROOT_MARKER_NAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        payload = canonical_json_bytes(
            {
                "format": _ROOT_MARKER_SCHEMA,
                "realm_id": realm_id,
                "projection_root_id": f"projection-root-{uuid.uuid4().hex}",
                "claim_nonce": secrets.token_hex(32),
            }
        )
        temporary = f".projection-root-{uuid.uuid4().hex}.tmp"
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
    _repair_root_marker_links(root_fd)
    marker = _read_canonical_marker(
        root_fd, _ROOT_MARKER_NAME, label="projection root marker"
    )
    if set(marker) != {"format", "realm_id", "projection_root_id", "claim_nonce"}:
        raise RealmIntegrityError("Projection root marker has an invalid shape.")
    if marker["format"] != _ROOT_MARKER_SCHEMA or marker["realm_id"] != realm_id:
        raise RealmIntegrityError("Projection root marker belongs to another realm.")
    for field in ("projection_root_id", "claim_nonce"):
        if field == "claim_nonce":
            _lower_hex_digest(marker[field], "projection root marker claim_nonce")
        else:
            _required_text(marker[field], "projection root marker projection_root_id")
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
            "Projection root kind cannot be inspected safely."
        ) from error
    raise RealmConflict(
        "A writable volume root cannot also be used as a read-only projection root."
    )


def _repair_root_marker_links(root_fd: int) -> None:
    """Remove reserved publication debris and prove one canonical marker link."""

    marker = os.stat(_ROOT_MARKER_NAME, dir_fd=root_fd, follow_symlinks=False)
    if not _is_private_read_only_file(marker):
        raise RealmIntegrityError("Projection root marker has unsafe links.")
    changed = False
    for name in os.listdir(root_fd):
        if not (name.startswith(".projection-root-") and name.endswith(".tmp")):
            continue
        try:
            candidate = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:  # pragma: no cover - root lock serializes peers
            continue
        if not _is_private_read_only_file(candidate):
            raise RealmIntegrityError(
                "Projection root marker temporary file is unsafe."
            )
        same_marker = (candidate.st_dev, candidate.st_ino) == (
            marker.st_dev,
            marker.st_ino,
        )
        if not same_marker and candidate.st_nlink != 1:
            raise RealmIntegrityError(
                "Projection root marker temporary file has unknown aliases."
            )
        os.unlink(name, dir_fd=root_fd)
        changed = True
    if changed:
        os.fsync(root_fd)
    repaired = os.stat(_ROOT_MARKER_NAME, dir_fd=root_fd, follow_symlinks=False)
    if not _is_private_read_only_file(repaired) or repaired.st_nlink != 1:
        raise RealmIntegrityError(
            "Projection root marker has an unknown hard-link alias."
        )


def _rollback_created_wrapper(
    root_fd: int,
    wrapper_fd: int,
    *,
    directory_name: str,
    expected_identity: tuple[int, int],
) -> None:
    """Best-effort rollback that never removes an unproven directory link."""

    with _root_lock(root_fd, exclusive=True):
        _require_directory_link(
            root_fd,
            directory_name,
            wrapper_fd,
            expected_identity,
            "projection wrapper",
        )
        try:
            os.unlink(_REALIZATION_CLAIM_NAME, dir_fd=wrapper_fd)
        except FileNotFoundError:
            pass
        os.fsync(wrapper_fd)
        if os.listdir(wrapper_fd):
            return
        _require_directory_link(
            root_fd,
            directory_name,
            wrapper_fd,
            expected_identity,
            "projection wrapper",
        )
        os.rmdir(directory_name, dir_fd=root_fd)
        os.fsync(root_fd)


def _validate_claim(wrapper_fd: int, expected: ProjectionNamespaceClaim) -> None:
    marker = _read_canonical_marker(
        wrapper_fd, _REALIZATION_CLAIM_NAME, label="projection realization claim"
    )
    if marker != expected.to_dict():
        raise RealmIntegrityError("Projection realization claim identity changed.")


def _cleanup_tombstone_name(
    claim: ProjectionNamespaceClaim, cleanup_token: str
) -> str:
    suffix = hashlib.sha256(
        f"{claim.realization_id}/{cleanup_token}".encode("utf-8")
    ).hexdigest()
    return f".projection-cleanup-{suffix}.json"


def _retired_namespace_name(
    claim: ProjectionNamespaceClaim, cleanup_token: str
) -> str:
    suffix = hashlib.sha256(
        b"optpilot/projection-retiring/v1\0"
        + canonical_json_bytes(claim.to_dict())
        + b"\0"
        + cleanup_token.encode("ascii")
    ).hexdigest()
    return f"{_RETIRED_NAMESPACE_PREFIX}{suffix}"


def _retirement_proof_name(
    claim: ProjectionNamespaceClaim, cleanup_token: str
) -> str:
    suffix = hashlib.sha256(
        b"optpilot/projection-retirement-proof/v1\0"
        + canonical_json_bytes(claim.to_dict())
        + b"\0"
        + cleanup_token.encode("ascii")
    ).hexdigest()
    return f"{_RETIREMENT_PROOF_PREFIX}{suffix}.json"


def _retirement_marker_name(claim: ProjectionNamespaceClaim) -> str:
    suffix = hashlib.sha256(canonical_json_bytes(claim.to_dict())).hexdigest()
    return f"{_RETIREMENT_MARKER_PREFIX}{suffix}.json"


def _retirement_marker_payload(
    claim: ProjectionNamespaceClaim, cleanup_token: str
) -> dict[str, object]:
    return {
        "format": _RETIREMENT_MARKER_SCHEMA,
        "claim": claim.to_dict(),
        "cleanup_token": cleanup_token,
    }


def _cleanup_tombstone_payload(
    claim: ProjectionNamespaceClaim, _cleanup_token: str
) -> dict[str, object]:
    # The cleanup token is bound into the unguessable deterministic filename;
    # the renamed immutable claim remains the tombstone contents.
    return claim.to_dict()


def _retirement_proof_payload(
    claim: ProjectionNamespaceClaim,
    cleanup_token: str,
    identity: ProjectionNamespaceIdentity,
) -> dict[str, object]:
    return {
        "format": _RETIREMENT_PROOF_SCHEMA,
        "claim": claim.to_dict(),
        "cleanup_token": cleanup_token,
        "retired_name": _retired_namespace_name(claim, cleanup_token),
        "identity": identity.operational_record(),
    }


def _load_optional_retirement_proof(
    root_fd: int,
    *,
    claim: ProjectionNamespaceClaim,
    cleanup_token: str,
    directory_name: Optional[str],
) -> Optional[ProjectionNamespaceIdentity]:
    proof_name = _retirement_proof_name(claim, cleanup_token)
    try:
        os.stat(proof_name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    value = _read_canonical_marker(
        root_fd,
        proof_name,
        label="projection retirement proof",
    )
    if set(value) != {
        "format",
        "claim",
        "cleanup_token",
        "retired_name",
        "identity",
    } or (
        value["format"] != _RETIREMENT_PROOF_SCHEMA
        or value["claim"] != claim.to_dict()
        or value["cleanup_token"] != cleanup_token
        or value["retired_name"] != _retired_namespace_name(claim, cleanup_token)
    ):
        raise RealmIntegrityError(
            "Projection retirement proof belongs to another namespace."
        )
    raw_identity = value["identity"]
    if not isinstance(raw_identity, dict) or set(raw_identity) != {
        "directory_name",
        "wrapper_device_id",
        "wrapper_inode",
        "tree_device_id",
        "tree_inode",
    }:
        raise RealmIntegrityError("Projection retirement proof identity is malformed.")
    try:
        identity = ProjectionNamespaceIdentity(
            directory_name=_safe_component(
                raw_identity["directory_name"], "retirement proof directory name"
            ),
            wrapper_device_id=_nonnegative_int(
                raw_identity["wrapper_device_id"],
                "retirement proof wrapper device id",
            ),
            wrapper_inode=_positive_int(
                raw_identity["wrapper_inode"], "retirement proof wrapper inode"
            ),
            tree_device_id=(
                None
                if raw_identity["tree_device_id"] is None
                else _nonnegative_int(
                    raw_identity["tree_device_id"],
                    "retirement proof tree device id",
                )
            ),
            tree_inode=(
                None
                if raw_identity["tree_inode"] is None
                else _positive_int(
                    raw_identity["tree_inode"], "retirement proof tree inode"
                )
            ),
        )
    except ValueError as error:
        raise RealmIntegrityError(
            "Projection retirement proof identity is malformed."
        ) from error
    if directory_name is not None and identity.directory_name != directory_name:
        raise RealmIntegrityError("Projection retirement proof names another namespace.")
    return identity


def _publish_or_validate_retirement_proof(
    root_fd: int,
    *,
    claim: ProjectionNamespaceClaim,
    cleanup_token: str,
    identity: ProjectionNamespaceIdentity,
) -> None:
    existing = _load_optional_retirement_proof(
        root_fd,
        claim=claim,
        cleanup_token=cleanup_token,
        directory_name=identity.directory_name,
    )
    if existing is not None:
        if existing != identity:
            raise RealmIntegrityError(
                "Projection retirement proof has a different namespace identity."
            )
        return
    name = _retirement_proof_name(claim, cleanup_token)
    payload = canonical_json_bytes(
        _retirement_proof_payload(claim, cleanup_token, identity)
    )
    try:
        _write_file_exclusive(root_fd, name, payload, mode=0o400)
    except FileExistsError:
        existing = _load_optional_retirement_proof(
            root_fd,
            claim=claim,
            cleanup_token=cleanup_token,
            directory_name=identity.directory_name,
        )
        if existing != identity:
            raise RealmIntegrityError(
                "Projection retirement proof publication raced unsafely."
            )
    os.fsync(root_fd)


def _path_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _validate_optional_cleanup_tombstone(
    root_fd: int,
    name: str,
    expected: dict[str, object],
) -> bool:
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    value = _read_canonical_marker(
        root_fd, name, label="projection cleanup tombstone"
    )
    if value != expected:
        raise RealmIntegrityError(
            "Projection cleanup tombstone belongs to another realization."
        )
    return True


def _validate_optional_retirement_marker(
    root_fd: int,
    name: str,
    expected: dict[str, object],
) -> bool:
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    value = _read_canonical_marker(
        root_fd, name, label="projection retirement marker"
    )
    if value != expected:
        raise RealmIntegrityError(
            "Projection retirement marker belongs to another realization."
        )
    return True


def _validate_optional_retired_claim(
    root_fd: int,
    name: str,
    claim: ProjectionNamespaceClaim,
) -> bool:
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    value = _read_canonical_marker(
        root_fd, name, label="projection retirement marker"
    )
    if (
        set(value) != {"format", "claim", "cleanup_token"}
        or value.get("format") != _RETIREMENT_MARKER_SCHEMA
        or value.get("claim") != claim.to_dict()
    ):
        raise RealmIntegrityError(
            "Projection retirement marker belongs to another realization."
        )
    _lower_hex_digest(value.get("cleanup_token"), "retirement cleanup_token")
    return True


def _publish_or_validate_retirement_marker(
    root_fd: int,
    name: str,
    expected: dict[str, object],
) -> None:
    if _validate_optional_retirement_marker(root_fd, name, expected):
        return
    try:
        _write_file_exclusive(
            root_fd,
            name,
            canonical_json_bytes(expected),
            mode=0o400,
        )
    except FileExistsError:
        if not _validate_optional_retirement_marker(root_fd, name, expected):
            raise RealmIntegrityError(
                "Projection retirement marker publication raced unsafely."
            )
    os.fsync(root_fd)


@contextmanager
def _root_lock(root_fd: int, *, exclusive: bool, create: bool = True):
    if fcntl is None:  # pragma: no cover - guarded by POSIX namespace setup
        raise NotImplementedError("Projection namespace locks require POSIX flock.")
    try:
        flags = (
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        if create:
            flags |= os.O_CREAT
        descriptor = os.open(
            _ROOT_LOCK_NAME,
            flags,
            0o600,
            dir_fd=root_fd,
        )
    except OSError as error:
        raise RealmIntegrityError(
            "Projection root lock is unavailable or unsafe."
        ) from error
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
            raise RealmIntegrityError("Projection root lock has an unsafe identity.")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        os.fsync(root_fd)
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        locked = True
        linked = os.stat(_ROOT_LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(linked.st_mode)
            or (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise RealmIntegrityError(
                "Projection root lock path identity changed while acquiring it."
            )
        yield
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_canonical_marker(directory_fd: int, name: str, *, label: str) -> dict[str, object]:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not _is_private_read_only_file(info) or info.st_nlink != 1:
            raise RealmIntegrityError(f"{label.capitalize()} has an unsafe file type.")
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
        try:
            opened = os.fstat(descriptor)
            if (
                not _is_private_read_only_file(opened)
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or opened.st_nlink != 1
                or opened.st_size > _MAX_MARKER_BYTES
            ):
                raise RealmIntegrityError(f"{label.capitalize()} changed while opening.")
            raw = b""
            while len(raw) <= _MAX_MARKER_BYTES:
                chunk = os.read(descriptor, min(65536, _MAX_MARKER_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            if len(raw) > _MAX_MARKER_BYTES:
                raise RealmIntegrityError(f"{label.capitalize()} is too large.")
        finally:
            os.close(descriptor)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RealmIntegrityError(f"{label.capitalize()} is unreadable.") from error
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise RealmIntegrityError(f"{label.capitalize()} is not canonical.") from error
    if not isinstance(value, dict) or canonical != raw:
        raise RealmIntegrityError(f"{label.capitalize()} is not canonical.")
    return value


def _is_private_read_only_file(info: os.stat_result) -> bool:
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o400:
        return False
    return not hasattr(os, "geteuid") or info.st_uid == os.geteuid()


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
        if os.name != "nt":
            os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Projection marker write made no progress.")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_directory_link(
    parent_fd: int,
    name: str,
    opened_fd: int,
    expected: tuple[int, int],
    label: str,
) -> None:
    try:
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(opened_fd)
    except OSError as error:
        raise RealmIntegrityError(f"{label.capitalize()} path identity changed.") from error
    if (
        not stat.S_ISDIR(linked.st_mode)
        or (linked.st_dev, linked.st_ino) != expected
        or (opened.st_dev, opened.st_ino) != expected
    ):
        raise RealmIntegrityError(f"{label.capitalize()} path identity changed.")


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise RealmIntegrityError("Projection root is unavailable or unsafe.") from error


def _safe_component(value: object, label: str) -> str:
    value = _required_text(value, label)
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} must be one safe path component.")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty string.")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _lower_hex_digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters.")
    return value


__all__ = [
    "AttachedProjectionNamespace",
    "ProjectionNamespaceClaim",
    "ProjectionNamespaceIdentity",
    "ProjectionRootBinding",
    "attach_projection_namespace",
    "cleanup_projection_namespace",
    "complete_projection_cleanup_namespace",
    "create_projection_wrapper",
    "find_projection_wrapper_identity",
    "observe_ready_projection_namespace_identity",
    "prepare_projection_root",
    "record_projection_tree_identity",
    "validate_projection_root",
]
