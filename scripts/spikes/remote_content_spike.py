"""Disposable remote ``ContentSource`` and transfer architecture spike.

The spike proves control-plane semantics for immutable remote content without
performing network I/O or allocating its synthetic multi-terabyte fixture.  It
is deliberately isolated from ``optpilot`` and is not a production provider.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Optional, Protocol, Sequence


GIB = 1024**3


class RemoteContentError(RuntimeError):
    """Base error for the disposable transfer spike."""


class RemoteContentConflict(RemoteContentError):
    """A registered identity or provider fact does not match the request."""


class RemoteContentUnavailable(RemoteContentError):
    """The registered provider cannot currently serve the content."""


class TransferCancelled(RemoteContentError):
    """A cancellable hydration/export stopped before durable completion."""


@dataclass(frozen=True)
class ContentSource:
    """Public remote branch: identity plus a registered provider binding only."""

    uri: str
    digest: str
    provider: str

    def __post_init__(self) -> None:
        if not self.uri or "://" not in self.uri:
            raise ValueError("uri must be an absolute provider URI")
        if not self.digest.startswith("sha256:"):
            raise ValueError("digest must be a sha256 identity")
        if not self.provider:
            raise ValueError("provider is required")


@dataclass(frozen=True)
class RemoteChunk:
    offset: int
    size_bytes: int
    fill_byte: int
    proof_digest: str

    def __post_init__(self) -> None:
        if self.offset < 0 or self.size_bytes <= 0 or not 0 <= self.fill_byte <= 255:
            raise ValueError("remote chunk has invalid bounds or virtual content")
        if self.proof_digest != _virtual_chunk_digest(
            offset=self.offset,
            size_bytes=self.size_bytes,
            fill_byte=self.fill_byte,
        ):
            raise ValueError("remote chunk proof is not content-derived")


@dataclass(frozen=True)
class RemoteManifest:
    uri: str
    total_size_bytes: int
    chunks: tuple[RemoteChunk, ...]

    def __post_init__(self) -> None:
        if not self.uri or "://" not in self.uri or self.total_size_bytes <= 0:
            raise ValueError("remote manifest requires an absolute URI and positive size")
        expected_offset = 0
        for chunk in self.chunks:
            if chunk.offset != expected_offset or chunk.size_bytes <= 0:
                raise ValueError("remote manifest chunks must be positive and contiguous")
            if not chunk.proof_digest.startswith("sha256:"):
                raise ValueError("remote manifest chunk proof must be a sha256 identity")
            expected_offset += chunk.size_bytes
        if expected_offset != self.total_size_bytes:
            raise ValueError("remote manifest chunks do not cover the declared size")

    @property
    def digest(self) -> str:
        return _content_manifest_digest(
            total_size_bytes=self.total_size_bytes,
            chunks=self.chunks,
        )


@dataclass(frozen=True)
class ManagedRemoteRef:
    ref: str
    owner_id: str
    source: ContentSource
    total_size_bytes: int


@dataclass(frozen=True)
class TransferPreflight:
    ref: str
    available: bool
    range_supported: bool
    local_bytes: int
    requested_bytes: int
    required_hydration_bytes: int
    worst_case_transfer_bytes: int
    estimated_egress_cost: float
    retention_guaranteed: bool
    cancellable: bool = True


@dataclass(frozen=True)
class ExportReceipt:
    ref: str
    digest: str
    total_size_bytes: int
    segment_count: int


class CancelToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def check(self) -> None:
        if self._cancelled:
            raise TransferCancelled("transfer cancelled")


class ExportSink(Protocol):
    def begin(self, *, expected_digest: str, total_size_bytes: int) -> None: ...

    def accept_proof(self, chunk: RemoteChunk) -> None: ...

    def finish(self) -> None: ...

    def abort(self) -> None: ...


class VirtualTransactionalExportSink:
    """Counts authenticated synthetic segments and publishes only on finish."""

    def __init__(self) -> None:
        self.expected_digest: Optional[str] = None
        self.expected_size = 0
        self.pending_bytes = 0
        self.pending_segments = 0
        self._pending_chunks: list[RemoteChunk] = []
        self.committed_bytes = 0
        self.committed_segments = 0
        self.complete = False
        self.aborted = False

    def begin(self, *, expected_digest: str, total_size_bytes: int) -> None:
        if self.expected_digest is not None:
            raise RemoteContentConflict("export sink already used")
        self.expected_digest = expected_digest
        self.expected_size = total_size_bytes

    def accept_proof(self, chunk: RemoteChunk) -> None:
        if self.complete or self.aborted or self.expected_digest is None:
            raise RemoteContentConflict("export sink is not writable")
        if chunk.proof_digest != _virtual_chunk_digest(
            offset=chunk.offset,
            size_bytes=chunk.size_bytes,
            fill_byte=chunk.fill_byte,
        ):
            raise RemoteContentConflict("export chunk proof is not content-authenticated")
        self.pending_bytes += chunk.size_bytes
        self.pending_segments += 1
        self._pending_chunks.append(chunk)

    def finish(self) -> None:
        if self.pending_bytes != self.expected_size:
            raise RemoteContentConflict(
                f"export is incomplete: {self.pending_bytes} of {self.expected_size} bytes"
            )
        actual_digest = _content_manifest_digest(
            total_size_bytes=self.expected_size,
            chunks=self._pending_chunks,
        )
        if actual_digest != self.expected_digest:
            raise RemoteContentConflict("exported virtual content does not match the source digest")
        self.committed_bytes = self.pending_bytes
        self.committed_segments = self.pending_segments
        self.pending_bytes = 0
        self.pending_segments = 0
        self._pending_chunks = []
        self.complete = True

    def abort(self) -> None:
        self.pending_bytes = 0
        self.pending_segments = 0
        self._pending_chunks = []
        self.aborted = True


class SyntheticRemoteProvider:
    """Deterministic registered-provider fake with virtual chunk proofs."""

    def __init__(
        self,
        manifest: RemoteManifest,
        *,
        available: bool = True,
        retention_guaranteed: bool = True,
        range_supported: bool = True,
        egress_cost_per_gib: float = 0.02,
    ) -> None:
        self.manifest = manifest
        self.available = available
        self.retention_guaranteed = retention_guaranteed
        self.range_supported = range_supported
        self.egress_cost_per_gib = float(egress_cost_per_gib)
        self.range_read_calls = 0
        self.export_proof_calls = 0

    def inspect(self, uri: str) -> RemoteManifest:
        if uri != self.manifest.uri:
            raise RemoteContentUnavailable(f"provider does not know URI: {uri}")
        return self.manifest

    def read_range(self, uri: str, offset: int, size_bytes: int) -> bytes:
        if not self.available:
            raise RemoteContentUnavailable("remote content is unavailable")
        if not self.range_supported:
            raise RemoteContentUnavailable("provider does not support range hydration")
        if uri != self.manifest.uri or offset < 0 or size_bytes < 0:
            raise RemoteContentConflict("invalid range request")
        if offset + size_bytes > self.manifest.total_size_bytes:
            raise RemoteContentConflict("range extends past remote content")
        self.range_read_calls += 1
        return _virtual_range_bytes(self.manifest, offset=offset, size_bytes=size_bytes)

    def export_proofs(self, uri: str) -> Iterable[RemoteChunk]:
        if not self.available:
            raise RemoteContentUnavailable("remote content is unavailable")
        if uri != self.manifest.uri:
            raise RemoteContentConflict("export URI does not match manifest")
        for chunk in self.manifest.chunks:
            self.export_proof_calls += 1
            yield chunk


Progress = Callable[[int, int], None]


class RemoteContentManagerSpike:
    """Minimal retain/preflight/hydrate/export control-plane proof."""

    def __init__(self, providers: Dict[str, SyntheticRemoteProvider]):
        self._providers = dict(providers)
        self._refs: Dict[str, ManagedRemoteRef] = {}
        self._owners: Dict[str, set[str]] = {}
        self._range_cache: Dict[tuple[str, int, int], bytes] = {}

    def retain(self, source: ContentSource, *, owner_id: str) -> ManagedRemoteRef:
        if not owner_id:
            raise ValueError("owner_id is required")
        provider = self._provider(source.provider)
        manifest = provider.inspect(source.uri)
        if manifest.digest != source.digest:
            raise RemoteContentConflict("source digest does not match provider manifest")
        if not provider.retention_guaranteed:
            raise RemoteContentConflict("registered provider does not guarantee retention")
        ref = _hash_json({
            "schema": "optpilot.managed-remote-ref.v1",
            "provider": source.provider,
            "uri": source.uri,
            "digest": source.digest,
        })
        managed = ManagedRemoteRef(
            ref=ref,
            owner_id=owner_id,
            source=source,
            total_size_bytes=manifest.total_size_bytes,
        )
        existing = self._refs.get(ref)
        if existing is not None and existing.source != source:
            raise RemoteContentConflict("managed ref identity collision")
        if existing is None:
            self._refs[ref] = managed
        self._owners.setdefault(ref, set()).add(owner_id)
        return managed

    def preflight(
        self,
        ref: str,
        *,
        owner_id: str,
        byte_range: Optional[tuple[int, int]] = None,
        full_export: bool = False,
    ) -> TransferPreflight:
        managed, provider, manifest = self._resolve(ref, owner_id=owner_id)
        if byte_range is not None and full_export:
            raise ValueError("choose a byte range or full export, not both")
        if full_export or byte_range is None:
            offset, requested = 0, manifest.total_size_bytes
        else:
            offset, requested = self._validate_range(byte_range, manifest.total_size_bytes)
        local_bytes = 0
        if byte_range is not None:
            local_bytes = len(self._range_cache.get((ref, offset, requested), b""))
        required = max(0, requested - local_bytes)
        return TransferPreflight(
            ref=managed.ref,
            available=provider.available,
            range_supported=provider.range_supported,
            local_bytes=local_bytes,
            requested_bytes=requested,
            required_hydration_bytes=required,
            worst_case_transfer_bytes=requested,
            estimated_egress_cost=(required / GIB) * provider.egress_cost_per_gib,
            retention_guaranteed=provider.retention_guaranteed,
        )

    def open_range(
        self,
        ref: str,
        *,
        owner_id: str,
        offset: int,
        size_bytes: int,
        cancel: Optional[CancelToken] = None,
        progress: Optional[Progress] = None,
        transfer_chunk_bytes: int = 4096,
        max_direct_range_bytes: int = 8 * 1024 * 1024,
    ) -> bytes:
        managed, provider, manifest = self._resolve(ref, owner_id=owner_id)
        offset, size_bytes = self._validate_range((offset, size_bytes), manifest.total_size_bytes)
        if transfer_chunk_bytes <= 0 or max_direct_range_bytes <= 0:
            raise ValueError("transfer chunk and direct-range limits must be positive")
        if size_bytes > max_direct_range_bytes:
            raise RemoteContentConflict("range exceeds bounded direct-hydration limit")
        cache_key = (managed.ref, offset, size_bytes)
        cached = self._range_cache.get(cache_key)
        if cached is not None:
            if progress is not None:
                progress(size_bytes, size_bytes)
            return cached
        if not provider.range_supported:
            raise RemoteContentUnavailable("provider does not support range hydration")
        token = cancel or CancelToken()
        parts = []
        completed = 0
        while completed < size_bytes:
            token.check()
            width = min(transfer_chunk_bytes, size_bytes - completed)
            part = provider.read_range(managed.source.uri, offset + completed, width)
            if len(part) != width:
                raise RemoteContentConflict("provider returned a short range")
            expected = _virtual_range_bytes(
                manifest,
                offset=offset + completed,
                size_bytes=width,
            )
            if part != expected:
                raise RemoteContentConflict("provider returned corrupt range content")
            parts.append(part)
            completed += width
            if progress is not None:
                progress(completed, size_bytes)
        token.check()
        content = b"".join(parts)
        self._range_cache[cache_key] = content
        return content

    def export_fully(
        self,
        ref: str,
        *,
        owner_id: str,
        sink: ExportSink,
        cancel: Optional[CancelToken] = None,
        progress: Optional[Progress] = None,
    ) -> ExportReceipt:
        managed, provider, manifest = self._resolve(ref, owner_id=owner_id)
        token = cancel or CancelToken()
        sink.begin(expected_digest=managed.source.digest, total_size_bytes=manifest.total_size_bytes)
        completed = 0
        count = 0
        try:
            proofs = iter(provider.export_proofs(managed.source.uri))
            for expected in manifest.chunks:
                token.check()
                try:
                    received = next(proofs)
                except StopIteration as exc:
                    raise RemoteContentConflict(
                        "provider export ended before the full manifest"
                    ) from exc
                if received != expected:
                    raise RemoteContentConflict("provider export proof differs from manifest")
                sink.accept_proof(received)
                completed += received.size_bytes
                count += 1
                if progress is not None:
                    progress(completed, manifest.total_size_bytes)
            token.check()
            try:
                next(proofs)
            except StopIteration:
                pass
            else:
                raise RemoteContentConflict("provider export contains extra segments")
            if count != len(manifest.chunks) or completed != manifest.total_size_bytes:
                raise RemoteContentConflict("provider export ended before the full manifest")
            sink.finish()
        except BaseException:
            sink.abort()
            raise
        return ExportReceipt(
            ref=managed.ref,
            digest=managed.source.digest,
            total_size_bytes=completed,
            segment_count=count,
        )

    def _provider(self, name: str) -> SyntheticRemoteProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise RemoteContentUnavailable(f"provider is not registered: {name}")
        return provider

    def _resolve(
        self,
        ref: str,
        *,
        owner_id: str,
    ) -> tuple[ManagedRemoteRef, SyntheticRemoteProvider, RemoteManifest]:
        managed = self._refs.get(ref)
        if managed is None or owner_id not in self._owners.get(ref, set()):
            # Authorization failure intentionally has the same shape as absence.
            raise RemoteContentUnavailable("managed remote ref is not available to this owner")
        provider = self._provider(managed.source.provider)
        manifest = provider.inspect(managed.source.uri)
        if manifest.digest != managed.source.digest:
            raise RemoteContentConflict("provider manifest changed after retention")
        return managed, provider, manifest

    @staticmethod
    def _validate_range(value: tuple[int, int], total: int) -> tuple[int, int]:
        offset, size_bytes = value
        if offset < 0 or size_bytes < 0 or offset + size_bytes > total:
            raise RemoteContentConflict("invalid byte range")
        return offset, size_bytes


def synthetic_manifest(
    *,
    uri: str,
    total_size_bytes: int,
    chunk_size_bytes: int = GIB,
    content_key: str = "optpilot-synthetic-content-v1",
) -> RemoteManifest:
    if total_size_bytes <= 0 or chunk_size_bytes <= 0 or not content_key:
        raise ValueError("synthetic manifest sizes must be positive")
    chunks = []
    offset = 0
    while offset < total_size_bytes:
        size_bytes = min(chunk_size_bytes, total_size_bytes - offset)
        fill_byte = hashlib.sha256(
            f"{content_key}:{offset}:{size_bytes}".encode("utf-8")
        ).digest()[0]
        chunks.append(RemoteChunk(
            offset=offset,
            size_bytes=size_bytes,
            fill_byte=fill_byte,
            proof_digest=_virtual_chunk_digest(
                offset=offset,
                size_bytes=size_bytes,
                fill_byte=fill_byte,
            ),
        ))
        offset += size_bytes
    return RemoteManifest(uri=uri, total_size_bytes=total_size_bytes, chunks=tuple(chunks))


def _virtual_chunk_digest(*, offset: int, size_bytes: int, fill_byte: int) -> str:
    """Authenticate all virtual bytes in one repeated-byte synthetic segment."""

    return _hash_json({
        "schema": "optpilot.virtual-content-segment.v1",
        "offset": offset,
        "size_bytes": size_bytes,
        "fill_byte": fill_byte,
    })


def _content_manifest_digest(
    *,
    total_size_bytes: int,
    chunks: Sequence[RemoteChunk],
) -> str:
    """Return a URI-independent identity for the synthetic content closure."""

    return _hash_json({
        "schema": "optpilot.synthetic-content-manifest.v1",
        "total_size_bytes": total_size_bytes,
        "chunks": [
            {
                "offset": chunk.offset,
                "size_bytes": chunk.size_bytes,
                "fill_byte": chunk.fill_byte,
                "proof_digest": chunk.proof_digest,
            }
            for chunk in chunks
        ],
    })


def _virtual_range_bytes(
    manifest: RemoteManifest,
    *,
    offset: int,
    size_bytes: int,
) -> bytes:
    """Materialize a bounded range from authenticated virtual chunk patterns."""

    if offset < 0 or size_bytes < 0 or offset + size_bytes > manifest.total_size_bytes:
        raise RemoteContentConflict("invalid virtual-content range")
    if size_bytes == 0:
        return b""
    end = offset + size_bytes
    parts = []
    covered = offset
    for chunk in manifest.chunks:
        chunk_end = chunk.offset + chunk.size_bytes
        start = max(offset, chunk.offset)
        stop = min(end, chunk_end)
        if start >= stop:
            continue
        if start != covered:
            raise RemoteContentConflict("virtual manifest has a range coverage gap")
        parts.append(bytes([chunk.fill_byte]) * (stop - start))
        covered = stop
        if covered == end:
            break
    if covered != end:
        raise RemoteContentConflict("virtual manifest did not cover the requested range")
    return b"".join(parts)


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
