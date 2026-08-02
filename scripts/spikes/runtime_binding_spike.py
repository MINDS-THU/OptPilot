"""Disposable runtime-binding and immutable-package architecture spike.

This module exercises three pre-production design gates:

* a path-free :class:`PortableRunSpec` binds to native and container providers;
* Preview derives from a terminal Debug Run selection with a fresh upper and
  audience-scoped credentials; and
* package prepare/validate/smoke/apply pin one immutable artifact digest.

It intentionally uses in-memory registries and synthetic paths.  It is not
imported by ``optpilot`` and must not be wired into the current run path.  The
point is to make the authority and identity boundaries executable before the
production WP5/WP6 APIs are chosen.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Dict, Iterable, Sequence, Tuple


JsonDict = Dict[str, Any]
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class SpikeInvariantError(RuntimeError):
    """Base error for a violated spike invariant."""


class InvalidPortableSpec(SpikeInvariantError):
    """A purported portable spec contains an invalid logical reference."""


class CredentialScopeError(SpikeInvariantError):
    """A launch requested a secret or grant outside its audience."""


class DebugSelectionError(SpikeInvariantError):
    """A Debug Run cannot be frozen into the requested selection."""


class StaleWorkspaceGeneration(SpikeInvariantError):
    """A package operation observed a different workspace generation."""


class ArtifactSubstitution(SpikeInvariantError):
    """A package phase did not receive its prepared immutable artifact."""


class PackagePhaseError(SpikeInvariantError):
    """Package phases were requested out of order."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def digest_record(value: Any) -> str:
    """Return the deterministic identity of a JSON-compatible record."""

    return _digest_bytes(_canonical_bytes(value))


def _require_name(value: str, *, label: str) -> None:
    if type(value) is not str or not _NAME_RE.fullmatch(value):
        raise InvalidPortableSpec(
            f"{label} must match {_NAME_RE.pattern!r}; received {value!r}"
        )


def _require_relative_path(value: str, *, label: str) -> None:
    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        raise InvalidPortableSpec(f"{label} must be a nonempty POSIX relative path")
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in raw_parts):
        raise InvalidPortableSpec(f"{label} must be normalized and contained: {value!r}")


@dataclass(frozen=True)
class ImmutableRef:
    """Logical identity of an immutable file/tree/runtime object."""

    kind: str
    digest: str

    def __post_init__(self) -> None:
        _require_name(self.kind, label="immutable-ref kind")
        if type(self.digest) is not str or not _DIGEST_RE.fullmatch(self.digest):
            raise InvalidPortableSpec(f"invalid immutable digest: {self.digest!r}")

    def to_record(self) -> JsonDict:
        return {"kind": self.kind, "digest": self.digest}


def fake_ref(label: str, *, kind: str = "tree") -> ImmutableRef:
    """Construct a deterministic fake immutable ref for spike scenarios."""

    return ImmutableRef(kind=kind, digest=_digest_bytes(label.encode("utf-8")))


@dataclass(frozen=True)
class LogicalScope:
    """One immutable input mounted under a provider-independent scope name."""

    name: str
    content: ImmutableRef
    subpath: str | None = None

    def __post_init__(self) -> None:
        _require_name(self.name, label="logical scope")
        if self.subpath is not None:
            _require_relative_path(self.subpath, label=f"scope {self.name!r} subpath")

    def to_record(self) -> JsonDict:
        record: JsonDict = {"name": self.name, "content": self.content.to_record()}
        if self.subpath is not None:
            record["subpath"] = self.subpath
        return record


@dataclass(frozen=True)
class RelativeEntrypoint:
    """An executable selected relative to one declared logical scope."""

    scope: str
    path: str

    def __post_init__(self) -> None:
        _require_name(self.scope, label="entrypoint scope")
        _require_relative_path(self.path, label="entrypoint path")

    def to_record(self) -> JsonDict:
        return {"scope": self.scope, "path": self.path}


@dataclass(frozen=True)
class PortableRunSpec:
    """Persistable runtime identity containing no host/container realization."""

    role: str
    scopes: Tuple[LogicalScope, ...]
    entrypoint: RelativeEntrypoint
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "scopes", tuple(self.scopes))
        _require_name(self.role, label="runtime role")
        if self.version != 1:
            raise InvalidPortableSpec("the spike supports PortableRunSpec version 1 only")
        names = [scope.name for scope in self.scopes]
        if not names or len(names) != len(set(names)):
            raise InvalidPortableSpec("PortableRunSpec scopes must be nonempty and unique")
        if self.entrypoint.scope not in names:
            raise InvalidPortableSpec(
                f"entrypoint scope {self.entrypoint.scope!r} is not declared"
            )

    def to_record(self) -> JsonDict:
        """Return the complete persistable record (deliberately path-free)."""

        return {
            "version": self.version,
            "role": self.role,
            "scopes": [
                scope.to_record()
                for scope in sorted(self.scopes, key=lambda item: item.name)
            ],
            "entrypoint": self.entrypoint.to_record(),
        }

    @property
    def identity(self) -> str:
        return digest_record(self.to_record())


@dataclass(frozen=True)
class BoundSecret:
    """A secret value held only by an ephemeral execution binding."""

    name: str
    value: str = field(repr=False)


@dataclass(frozen=True)
class BoundCredentials:
    """Audience-filtered credentials for exactly one invocation."""

    secrets: Tuple[BoundSecret, ...] = ()
    grants: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "secrets", tuple(self.secrets))
        object.__setattr__(self, "grants", frozenset(self.grants))

    @property
    def secret_names(self) -> Tuple[str, ...]:
        return tuple(secret.name for secret in self.secrets)


@dataclass(frozen=True)
class EphemeralOverlay:
    """One provider realization of a named writable upper."""

    overlay_id: str
    logical_name: str
    purpose: str
    host_path: Path = field(repr=False)

    def __post_init__(self) -> None:
        _require_name(self.logical_name, label="overlay logical name")
        _require_name(self.purpose, label="overlay purpose")
        if not self.host_path.is_absolute():
            raise SpikeInvariantError("an ephemeral overlay host path must be absolute")


class OverlayFactory:
    """Creates fresh upper identities; callers cannot provide an existing upper."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise SpikeInvariantError("overlay factory root must be absolute")
        self._root = root
        self._issued: set[str] = set()

    def create(self, *, logical_name: str, purpose: str) -> EphemeralOverlay:
        overlay_id = uuid.uuid4().hex
        if overlay_id in self._issued:  # pragma: no cover - UUID collision guard
            raise SpikeInvariantError("overlay identity collision")
        self._issued.add(overlay_id)
        return EphemeralOverlay(
            overlay_id=overlay_id,
            logical_name=logical_name,
            purpose=purpose,
            host_path=self._root / overlay_id,
        )


@dataclass(frozen=True)
class PhysicalScopeBinding:
    """Ephemeral mapping from a logical scope to provider paths."""

    logical_name: str
    content: ImmutableRef | None
    host_path: Path = field(repr=False)
    runtime_path: str = field(repr=False)
    writable: bool = False
    overlay_id: str | None = None


@dataclass(frozen=True)
class ExecutionBinding:
    """Concrete paths/credentials for one launch, never a persistable spec."""

    spec_identity: str
    invocation_id: str
    provider_kind: str
    enforcement: str
    scopes: Tuple[PhysicalScopeBinding, ...] = field(repr=False)
    entrypoint_path: str = field(repr=False)
    credentials: BoundCredentials = field(repr=False, default=BoundCredentials())

    def portable_evidence(self, spec: PortableRunSpec) -> JsonDict:
        """Return safe evidence with logical refs, names, and no physical values."""

        if spec.identity != self.spec_identity:
            raise SpikeInvariantError("binding/spec identity mismatch")
        provider = {"kind": self.provider_kind, "enforcement": self.enforcement}
        logical_map = [scope.to_record() for scope in spec.scopes]
        public_record = {
            "spec_identity": self.spec_identity,
            "provider": provider,
            "logical_map": logical_map,
            "entrypoint": spec.entrypoint.to_record(),
            "secret_names": sorted(self.credentials.secret_names),
            "grants": sorted(self.credentials.grants),
        }
        return {
            **public_record,
            "binding_fingerprint": digest_record(public_record),
        }


class NativeProcessProvider:
    """Trusted native-process realization with advisory filesystem isolation."""

    kind = "native-process"
    enforcement = "advisory"

    def __init__(self, managed_root: Path) -> None:
        if not managed_root.is_absolute():
            raise SpikeInvariantError("native managed root must be absolute")
        self._managed_root = managed_root

    def bind(
        self,
        spec: PortableRunSpec,
        *,
        invocation_id: str,
        overlays: Sequence[EphemeralOverlay] = (),
        credentials: BoundCredentials = BoundCredentials(),
    ) -> ExecutionBinding:
        _require_unique_overlay_names(overlays)
        immutable_names = {scope.name for scope in spec.scopes}
        if immutable_names.intersection(overlay.logical_name for overlay in overlays):
            raise SpikeInvariantError("a writable overlay cannot replace an immutable scope")
        scopes = []
        for scope in spec.scopes:
            host = self._managed_root / "objects" / scope.content.digest.removeprefix("sha256:")
            if scope.subpath:
                host /= scope.subpath
            scopes.append(
                PhysicalScopeBinding(
                    logical_name=scope.name,
                    content=scope.content,
                    host_path=host,
                    runtime_path=str(host),
                )
            )
        scopes.extend(_native_overlay_bindings(overlays))
        entry_scope = next(item for item in scopes if item.logical_name == spec.entrypoint.scope)
        return ExecutionBinding(
            spec_identity=spec.identity,
            invocation_id=invocation_id,
            provider_kind=self.kind,
            enforcement=self.enforcement,
            scopes=tuple(scopes),
            entrypoint_path=str(Path(entry_scope.runtime_path) / spec.entrypoint.path),
            credentials=credentials,
        )


def _native_overlay_bindings(
    overlays: Sequence[EphemeralOverlay],
) -> Tuple[PhysicalScopeBinding, ...]:
    _require_unique_overlay_names(overlays)
    return tuple(
        PhysicalScopeBinding(
            logical_name=overlay.logical_name,
            content=None,
            host_path=overlay.host_path,
            runtime_path=str(overlay.host_path),
            writable=True,
            overlay_id=overlay.overlay_id,
        )
        for overlay in overlays
    )


class ContainerProvider:
    """Container realization with private host mounts and fixed runtime paths."""

    kind = "container"
    enforcement = "kernel"

    def __init__(self, mount_source_root: Path, *, runtime_root: str = "/opt/optpilot") -> None:
        if not mount_source_root.is_absolute():
            raise SpikeInvariantError("container mount-source root must be absolute")
        runtime = PurePosixPath(runtime_root)
        if not runtime.is_absolute():
            raise SpikeInvariantError("container runtime root must be absolute")
        self._mount_source_root = mount_source_root
        self._runtime_root = runtime

    def bind(
        self,
        spec: PortableRunSpec,
        *,
        invocation_id: str,
        overlays: Sequence[EphemeralOverlay] = (),
        credentials: BoundCredentials = BoundCredentials(),
    ) -> ExecutionBinding:
        _require_unique_overlay_names(overlays)
        immutable_names = {scope.name for scope in spec.scopes}
        if immutable_names.intersection(overlay.logical_name for overlay in overlays):
            raise SpikeInvariantError("a writable overlay cannot replace an immutable scope")

        scopes = []
        for scope in spec.scopes:
            host = (
                self._mount_source_root
                / "objects"
                / scope.content.digest.removeprefix("sha256:")
            )
            if scope.subpath:
                host /= scope.subpath
            runtime = self._runtime_root / "scopes" / scope.name
            scopes.append(
                PhysicalScopeBinding(
                    logical_name=scope.name,
                    content=scope.content,
                    host_path=host,
                    runtime_path=str(runtime),
                )
            )
        for overlay in overlays:
            scopes.append(
                PhysicalScopeBinding(
                    logical_name=overlay.logical_name,
                    content=None,
                    host_path=overlay.host_path,
                    runtime_path=str(self._runtime_root / "volumes" / overlay.logical_name),
                    writable=True,
                    overlay_id=overlay.overlay_id,
                )
            )
        entry_scope = next(item for item in scopes if item.logical_name == spec.entrypoint.scope)
        entrypoint = PurePosixPath(entry_scope.runtime_path) / spec.entrypoint.path
        return ExecutionBinding(
            spec_identity=spec.identity,
            invocation_id=invocation_id,
            provider_kind=self.kind,
            enforcement=self.enforcement,
            scopes=tuple(scopes),
            entrypoint_path=str(entrypoint),
            credentials=credentials,
        )


def _require_unique_overlay_names(overlays: Sequence[EphemeralOverlay]) -> None:
    names = [overlay.logical_name for overlay in overlays]
    if len(names) != len(set(names)):
        raise SpikeInvariantError("writable overlay names must be unique")


@dataclass(frozen=True)
class ScopedSecret:
    """A secret stored with explicit launch audiences."""

    name: str
    value: str = field(repr=False)
    audiences: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "audiences", frozenset(self.audiences))


@dataclass(frozen=True)
class ScopedGrant:
    """A capability stored with explicit launch audiences."""

    capability: str
    audiences: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "audiences", frozenset(self.audiences))


class CredentialBroker:
    """Issues only explicitly requested credentials authorized for an audience."""

    def __init__(
        self,
        *,
        secrets: Iterable[ScopedSecret] = (),
        grants: Iterable[ScopedGrant] = (),
    ) -> None:
        secret_items = tuple(secrets)
        grant_items = tuple(grants)
        if len({item.name for item in secret_items}) != len(secret_items):
            raise SpikeInvariantError("secret names must be unique")
        if len({item.capability for item in grant_items}) != len(grant_items):
            raise SpikeInvariantError("grant capabilities must be unique")
        self._secrets = MappingProxyType({item.name: item for item in secret_items})
        self._grants = MappingProxyType({item.capability: item for item in grant_items})

    def issue(
        self,
        *,
        audience: str,
        required_secret_names: Sequence[str],
        required_grants: Sequence[str],
    ) -> BoundCredentials:
        bound_secrets = []
        for name in required_secret_names:
            secret = self._secrets.get(name)
            if secret is None or audience not in secret.audiences:
                raise CredentialScopeError(
                    f"secret {name!r} is unavailable to audience {audience!r}"
                )
            bound_secrets.append(BoundSecret(name=name, value=secret.value))

        issued_grants = set()
        for capability in required_grants:
            grant = self._grants.get(capability)
            if grant is None or audience not in grant.audiences:
                raise CredentialScopeError(
                    f"grant {capability!r} is unavailable to audience {audience!r}"
                )
            issued_grants.add(capability)
        return BoundCredentials(
            secrets=tuple(bound_secrets),
            grants=frozenset(issued_grants),
        )


@dataclass(frozen=True)
class TerminalDebugRun:
    """Terminal result plus its still-ephemeral evaluator upper."""

    debug_attempt_id: str
    state: str
    environment_ref: ImmutableRef
    candidate_ref: ImmutableRef
    terminal_view_ref: ImmutableRef | None
    evaluator_upper: EphemeralOverlay = field(repr=False)

    def select(self) -> "TerminalDebugSelection":
        if self.state != "terminal" or self.terminal_view_ref is None:
            raise DebugSelectionError("only a sealed terminal Debug Run is selectable")
        # Deliberately omit evaluator_upper. Preview can consume only immutable refs.
        return TerminalDebugSelection(
            debug_attempt_id=self.debug_attempt_id,
            environment_ref=self.environment_ref,
            candidate_ref=self.candidate_ref,
            terminal_view_ref=self.terminal_view_ref,
        )


@dataclass(frozen=True)
class TerminalDebugSelection:
    """Path-free Preview input derived from a terminal Debug Run."""

    debug_attempt_id: str
    environment_ref: ImmutableRef
    candidate_ref: ImmutableRef
    terminal_view_ref: ImmutableRef


@dataclass(frozen=True)
class InterfaceLaunchProfile:
    """Minimal interface declaration used by the Preview spike."""

    source_ref: ImmutableRef
    entrypoint: str
    required_secret_names: Tuple[str, ...] = ()
    required_grants: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_secret_names", tuple(self.required_secret_names))
        object.__setattr__(self, "required_grants", tuple(self.required_grants))
        _require_relative_path(self.entrypoint, label="interface entrypoint")
        if len(set(self.required_secret_names)) != len(self.required_secret_names):
            raise SpikeInvariantError("interface secret requirements must be unique")
        if len(set(self.required_grants)) != len(self.required_grants):
            raise SpikeInvariantError("interface grant requirements must be unique")


@dataclass(frozen=True)
class PreviewSession:
    selection: TerminalDebugSelection
    spec: PortableRunSpec
    binding: ExecutionBinding
    upper: EphemeralOverlay


class PreviewLauncher:
    """Launch Preview from immutable selection data and a newly allocated upper."""

    def __init__(
        self,
        *,
        provider: NativeProcessProvider | ContainerProvider,
        overlays: OverlayFactory,
        credentials: CredentialBroker,
    ) -> None:
        self._provider = provider
        self._overlays = overlays
        self._credentials = credentials

    def open(
        self,
        selection: TerminalDebugSelection,
        profile: InterfaceLaunchProfile,
        *,
        invocation_id: str,
    ) -> PreviewSession:
        spec = PortableRunSpec(
            role="preview",
            scopes=(
                LogicalScope("app", profile.source_ref),
                LogicalScope("target", selection.terminal_view_ref),
            ),
            entrypoint=RelativeEntrypoint("app", profile.entrypoint),
        )
        issued = self._credentials.issue(
            audience="preview",
            required_secret_names=profile.required_secret_names,
            required_grants=profile.required_grants,
        )
        # There is no overlay argument on this API: Preview always allocates its own.
        upper = self._overlays.create(logical_name="output", purpose="preview")
        binding = self._provider.bind(
            spec,
            invocation_id=invocation_id,
            overlays=(upper,),
            credentials=issued,
        )
        return PreviewSession(
            selection=selection,
            spec=spec,
            binding=binding,
            upper=upper,
        )


@dataclass(frozen=True)
class WorkspaceRevision:
    workspace_id: str
    generation: int
    snapshot_ref: ImmutableRef

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise SpikeInvariantError("workspace generation must be nonnegative")


class WorkspaceRegistry:
    """Tiny optimistic-generation authority used only by the package spike."""

    def __init__(self) -> None:
        self._current: dict[str, WorkspaceRevision] = {}

    def put(self, revision: WorkspaceRevision) -> None:
        current = self._current.get(revision.workspace_id)
        if current is not None and revision.generation <= current.generation:
            raise SpikeInvariantError("workspace generations must advance monotonically")
        self._current[revision.workspace_id] = revision

    def require(self, workspace_id: str, *, expected_generation: int) -> WorkspaceRevision:
        current = self._current.get(workspace_id)
        if current is None or current.generation != expected_generation:
            actual = None if current is None else current.generation
            raise StaleWorkspaceGeneration(
                f"workspace {workspace_id!r} expected generation {expected_generation}, "
                f"found {actual}"
            )
        return current


@dataclass(frozen=True)
class TreePlan:
    """Already-normalized package selection/rewrite policy."""

    include: Tuple[str, ...]
    exclude: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "include", tuple(self.include))
        object.__setattr__(self, "exclude", tuple(self.exclude))
        for index, path in enumerate((*self.include, *self.exclude)):
            _require_relative_path(path, label=f"TreePlan path {index}")
        if not self.include:
            raise SpikeInvariantError("TreePlan must include at least one path")

    def to_record(self) -> JsonDict:
        return {
            "include": sorted(set(self.include)),
            "exclude": sorted(set(self.exclude)),
        }

    @property
    def digest(self) -> str:
        return digest_record(self.to_record())


@dataclass(frozen=True)
class PackageArtifactRef:
    """Complete immutable identity pinned across all package phases."""

    digest: str
    source_snapshot_ref: ImmutableRef
    tree_plan_digest: str
    compiler_version: str
    resulting_snapshot_ref: ImmutableRef

    def __post_init__(self) -> None:
        if not _DIGEST_RE.fullmatch(self.digest):
            raise SpikeInvariantError("invalid package artifact digest")

    def verify_payload(self, payload: bytes) -> None:
        if _digest_bytes(payload) != self.digest:
            raise ArtifactSubstitution("package payload does not match artifact digest")
        try:
            record = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactSubstitution("package artifact manifest is not canonical JSON") from exc
        if not isinstance(record, dict):
            raise ArtifactSubstitution("package artifact manifest must be an object")
        expected_keys = {
            "source_snapshot",
            "tree_plan",
            "compiler_version",
            "resulting_snapshot",
        }
        if set(record) != expected_keys:
            raise ArtifactSubstitution("package artifact manifest has an unexpected shape")
        if record["source_snapshot"] != self.source_snapshot_ref.to_record():
            raise ArtifactSubstitution("package source snapshot provenance was substituted")
        if digest_record(record["tree_plan"]) != self.tree_plan_digest:
            raise ArtifactSubstitution("package TreePlan provenance was substituted")
        if record["compiler_version"] != self.compiler_version:
            raise ArtifactSubstitution("package compiler provenance was substituted")
        if record["resulting_snapshot"] != self.resulting_snapshot_ref.to_record():
            raise ArtifactSubstitution("package result snapshot provenance was substituted")


@dataclass(frozen=True)
class PackageArtifactLease:
    """Zero-copy read lease over bytes owned by the immutable artifact store."""

    ref: PackageArtifactRef
    payload: memoryview = field(repr=False)


class PackageArtifactStore:
    """Content-addressed spike store; leases never duplicate the stored payload."""

    def __init__(self) -> None:
        self._artifacts: dict[str, tuple[PackageArtifactRef, bytes]] = {}
        self.publish_count = 0
        self.lease_digests: list[str] = []
        self.lease_payload_object_ids: list[int] = []

    def publish(self, ref: PackageArtifactRef, payload: bytes) -> None:
        ref.verify_payload(payload)
        existing = self._artifacts.get(ref.digest)
        if existing is not None and existing != (ref, payload):
            raise ArtifactSubstitution("artifact digest already names different provenance")
        if existing is None:
            self._artifacts[ref.digest] = (ref, payload)
            self.publish_count += 1

    def lease(self, ref: PackageArtifactRef) -> PackageArtifactLease:
        stored = self._artifacts.get(ref.digest)
        if stored is None or stored[0] != ref:
            raise ArtifactSubstitution("package artifact ref was substituted or is unknown")
        stored_ref, payload = stored
        stored_ref.verify_payload(payload)
        self.lease_digests.append(ref.digest)
        self.lease_payload_object_ids.append(id(payload))
        return PackageArtifactLease(ref=stored_ref, payload=memoryview(payload))


class PackageCompiler:
    """Synthetic one-shot compiler with counters that expose accidental rebuilds."""

    def __init__(self, *, version: str = "spike-v1") -> None:
        self.version = version
        self.compile_count = 0
        self.source_projection_count = 0

    def compile(
        self,
        revision: WorkspaceRevision,
        plan: TreePlan,
    ) -> tuple[PackageArtifactRef, bytes]:
        self.compile_count += 1
        self.source_projection_count += 1
        result_record = {
            "source_snapshot": revision.snapshot_ref.to_record(),
            "tree_plan": plan.to_record(),
            "compiler_version": self.version,
        }
        resulting_snapshot = ImmutableRef(
            kind="tree",
            digest=digest_record({"resulting_tree": result_record}),
        )
        payload = _canonical_bytes(
            {
                **result_record,
                "resulting_snapshot": resulting_snapshot.to_record(),
            }
        )
        ref = PackageArtifactRef(
            digest=_digest_bytes(payload),
            source_snapshot_ref=revision.snapshot_ref,
            tree_plan_digest=plan.digest,
            compiler_version=self.version,
            resulting_snapshot_ref=resulting_snapshot,
        )
        return ref, payload


class CatalogPublisher:
    """Records an atomic catalog switch by ref; it never accepts source bytes."""

    def __init__(self) -> None:
        self.published_refs: list[PackageArtifactRef] = []

    def publish(self, ref: PackageArtifactRef) -> None:
        self.published_refs.append(ref)


@dataclass(frozen=True)
class PackagePhaseReceipt:
    phase: str
    artifact_digest: str


class PackageWorkflow:
    """State machine pinned to the artifact produced by one prepare operation."""

    _PHASES = ("validate", "smoke", "apply")

    def __init__(
        self,
        *,
        artifact_ref: PackageArtifactRef,
        source_revision: WorkspaceRevision,
        workspaces: WorkspaceRegistry,
        artifacts: PackageArtifactStore,
        publisher: CatalogPublisher,
    ) -> None:
        self.artifact_ref = artifact_ref
        self.source_revision = source_revision
        self._workspaces = workspaces
        self._artifacts = artifacts
        self._publisher = publisher
        self._next_phase = 0
        self.phase_receipts: list[PackagePhaseReceipt] = []

    def validate(self, artifact_ref: PackageArtifactRef) -> PackagePhaseReceipt:
        return self._consume("validate", artifact_ref)

    def smoke(self, artifact_ref: PackageArtifactRef) -> PackagePhaseReceipt:
        return self._consume("smoke", artifact_ref)

    def apply(self, artifact_ref: PackageArtifactRef) -> PackagePhaseReceipt:
        # Optimistic UI state cannot silently publish an artifact from a workspace
        # generation that has since changed; the operator must prepare a new flow.
        self._workspaces.require(
            self.source_revision.workspace_id,
            expected_generation=self.source_revision.generation,
        )
        receipt = self._consume("apply", artifact_ref)
        self._publisher.publish(self.artifact_ref)
        return receipt

    def _consume(
        self,
        phase: str,
        artifact_ref: PackageArtifactRef,
    ) -> PackagePhaseReceipt:
        expected = self._PHASES[self._next_phase] if self._next_phase < len(self._PHASES) else None
        if phase != expected:
            raise PackagePhaseError(f"expected package phase {expected!r}, received {phase!r}")
        if artifact_ref != self.artifact_ref:
            raise ArtifactSubstitution(
                f"phase {phase!r} must consume prepared digest {self.artifact_ref.digest}"
            )
        lease = self._artifacts.lease(artifact_ref)
        if lease.ref.digest != self.artifact_ref.digest:
            raise ArtifactSubstitution("artifact store returned a different digest")
        receipt = PackagePhaseReceipt(phase=phase, artifact_digest=lease.ref.digest)
        self.phase_receipts.append(receipt)
        self._next_phase += 1
        return receipt


class PackagePipeline:
    """Prepare exactly one workspace revision into one pinned package workflow."""

    def __init__(
        self,
        *,
        workspaces: WorkspaceRegistry,
        compiler: PackageCompiler,
        artifacts: PackageArtifactStore,
        publisher: CatalogPublisher,
    ) -> None:
        self._workspaces = workspaces
        self._compiler = compiler
        self._artifacts = artifacts
        self._publisher = publisher

    def prepare(
        self,
        *,
        workspace_id: str,
        expected_generation: int,
        tree_plan: TreePlan,
    ) -> PackageWorkflow:
        revision = self._workspaces.require(
            workspace_id,
            expected_generation=expected_generation,
        )
        artifact_ref, payload = self._compiler.compile(revision, tree_plan)
        self._artifacts.publish(artifact_ref, payload)
        return PackageWorkflow(
            artifact_ref=artifact_ref,
            source_revision=revision,
            workspaces=self._workspaces,
            artifacts=self._artifacts,
            publisher=self._publisher,
        )
