"""Focused tests for the disposable remote ContentSource transfer spike."""

from __future__ import annotations

import dataclasses
import unittest

from scripts.spikes.remote_content_spike import (
    GIB,
    CancelToken,
    ContentSource,
    RemoteContentConflict,
    RemoteContentManagerSpike,
    RemoteContentUnavailable,
    SyntheticRemoteProvider,
    TransferCancelled,
    VirtualTransactionalExportSink,
    synthetic_manifest,
)


class RemoteContentSpikeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.total_size = 3 * 1024**4  # 3 TiB, represented by 3,072 virtual proofs.
        self.manifest = synthetic_manifest(
            uri="s3://benchmarks.example/cases-v4.bin",
            total_size_bytes=self.total_size,
            chunk_size_bytes=GIB,
        )
        self.provider = SyntheticRemoteProvider(
            self.manifest,
            egress_cost_per_gib=0.025,
        )
        self.manager = RemoteContentManagerSpike({"lab-archive": self.provider})
        self.source = ContentSource(
            uri=self.manifest.uri,
            digest=self.manifest.digest,
            provider="lab-archive",
        )
        self.owner_id = "workspace:a"
        self.managed = self.manager.retain(self.source, owner_id=self.owner_id)

    def test_public_source_has_identity_and_provider_but_no_realization_policy(self) -> None:
        self.assertEqual(
            [field.name for field in dataclasses.fields(ContentSource)],
            ["uri", "digest", "provider"],
        )

    def test_content_digest_is_independent_of_provider_location(self) -> None:
        relocated = synthetic_manifest(
            uri="gs://mirror.example/cases-v4.bin",
            total_size_bytes=self.total_size,
            chunk_size_bytes=GIB,
        )
        self.assertNotEqual(relocated.uri, self.manifest.uri)
        self.assertEqual(relocated.chunks, self.manifest.chunks)
        self.assertEqual(relocated.digest, self.manifest.digest)

    def test_multi_terabyte_source_retains_by_ref_without_eager_hydration(self) -> None:
        preflight = self.manager.preflight(
            self.managed.ref,
            owner_id=self.owner_id,
            full_export=True,
        )

        self.assertEqual(self.managed.total_size_bytes, self.total_size)
        self.assertEqual(preflight.local_bytes, 0)
        self.assertEqual(preflight.requested_bytes, self.total_size)
        self.assertEqual(preflight.required_hydration_bytes, self.total_size)
        self.assertEqual(preflight.worst_case_transfer_bytes, self.total_size)
        self.assertAlmostEqual(preflight.estimated_egress_cost, 3072 * 0.025)
        self.assertTrue(preflight.available)
        self.assertTrue(preflight.retention_guaranteed)
        self.assertTrue(preflight.cancellable)
        self.assertEqual(self.provider.range_read_calls, 0)
        self.assertEqual(self.provider.export_proof_calls, 0)

    def test_requested_range_hydrates_once_and_reports_actual_local_bytes(self) -> None:
        progress = []
        content = self.manager.open_range(
            self.managed.ref,
            owner_id=self.owner_id,
            offset=GIB + 123,
            size_bytes=10_000,
            transfer_chunk_bytes=2048,
            progress=lambda completed, total: progress.append((completed, total)),
        )

        self.assertEqual(len(content), 10_000)
        self.assertEqual(progress[-1], (10_000, 10_000))
        calls = self.provider.range_read_calls
        preflight = self.manager.preflight(
            self.managed.ref,
            owner_id=self.owner_id,
            byte_range=(GIB + 123, 10_000),
        )
        self.assertEqual(preflight.local_bytes, 10_000)
        self.assertEqual(preflight.required_hydration_bytes, 0)

        again = self.manager.open_range(
            self.managed.ref,
            owner_id=self.owner_id,
            offset=GIB + 123,
            size_bytes=10_000,
        )
        self.assertEqual(again, content)
        self.assertEqual(self.provider.range_read_calls, calls)

    def test_cancelled_range_does_not_publish_a_partial_cache_entry(self) -> None:
        token = CancelToken()
        progress = []

        def observe(completed: int, total: int) -> None:
            progress.append((completed, total))
            token.cancel()

        with self.assertRaises(TransferCancelled):
            self.manager.open_range(
                self.managed.ref,
                owner_id=self.owner_id,
                offset=50,
                size_bytes=20_000,
                cancel=token,
                progress=observe,
                transfer_chunk_bytes=4096,
            )

        self.assertEqual(progress, [(4096, 20_000)])
        preflight = self.manager.preflight(
            self.managed.ref,
            owner_id=self.owner_id,
            byte_range=(50, 20_000),
        )
        self.assertEqual(preflight.local_bytes, 0)
        self.assertEqual(preflight.required_hydration_bytes, 20_000)

    def test_range_hydration_rejects_corrupt_same_length_bytes_and_zero_chunks(self) -> None:
        class CorruptRangeProvider(SyntheticRemoteProvider):
            def read_range(self, uri: str, offset: int, size_bytes: int) -> bytes:
                content = super().read_range(uri, offset, size_bytes)
                return bytes([content[0] ^ 1]) + content[1:] if content else content

        provider = CorruptRangeProvider(self.manifest)
        manager = RemoteContentManagerSpike({"corrupt": provider})
        retained = manager.retain(
            ContentSource(
                uri=self.source.uri,
                digest=self.source.digest,
                provider="corrupt",
            ),
            owner_id=self.owner_id,
        )
        with self.assertRaisesRegex(RemoteContentConflict, "corrupt range"):
            manager.open_range(
                retained.ref,
                owner_id=self.owner_id,
                offset=0,
                size_bytes=128,
            )
        with self.assertRaisesRegex(ValueError, "positive"):
            self.manager.open_range(
                self.managed.ref,
                owner_id=self.owner_id,
                offset=0,
                size_bytes=128,
                transfer_chunk_bytes=0,
            )

    def test_full_virtual_export_is_transactional_and_cancellable(self) -> None:
        token = CancelToken()
        cancelled_sink = VirtualTransactionalExportSink()

        def cancel_after_five_segments(completed: int, total: int) -> None:
            self.assertEqual(total, self.total_size)
            if completed >= 5 * GIB:
                token.cancel()

        with self.assertRaises(TransferCancelled):
            self.manager.export_fully(
                self.managed.ref,
                owner_id=self.owner_id,
                sink=cancelled_sink,
                cancel=token,
                progress=cancel_after_five_segments,
            )
        self.assertTrue(cancelled_sink.aborted)
        self.assertFalse(cancelled_sink.complete)
        self.assertEqual(cancelled_sink.committed_bytes, 0)
        self.assertEqual(cancelled_sink.pending_bytes, 0)

        complete_sink = VirtualTransactionalExportSink()
        progress = []
        receipt = self.manager.export_fully(
            self.managed.ref,
            owner_id=self.owner_id,
            sink=complete_sink,
            progress=lambda completed, total: progress.append((completed, total)),
        )
        self.assertTrue(complete_sink.complete)
        self.assertEqual(complete_sink.committed_bytes, self.total_size)
        self.assertEqual(complete_sink.committed_segments, 3072)
        self.assertEqual(receipt.digest, self.source.digest)
        self.assertEqual(receipt.total_size_bytes, self.total_size)
        self.assertEqual(receipt.segment_count, 3072)
        self.assertEqual(progress[-1], (self.total_size, self.total_size))

    def test_full_export_rejects_content_valid_for_a_different_manifest(self) -> None:
        alternate = synthetic_manifest(
            uri=self.manifest.uri,
            total_size_bytes=self.total_size,
            chunk_size_bytes=GIB,
            content_key="different-content",
        )

        class SubstitutingProvider(SyntheticRemoteProvider):
            def export_proofs(self, uri: str):
                self.export_proof_calls += len(alternate.chunks)
                yield from alternate.chunks

        provider = SubstitutingProvider(self.manifest)
        manager = RemoteContentManagerSpike({"substitute": provider})
        retained = manager.retain(
            ContentSource(
                uri=self.source.uri,
                digest=self.source.digest,
                provider="substitute",
            ),
            owner_id=self.owner_id,
        )
        sink = VirtualTransactionalExportSink()
        with self.assertRaisesRegex(RemoteContentConflict, "differs from manifest"):
            manager.export_fully(
                retained.ref,
                owner_id=self.owner_id,
                sink=sink,
            )
        self.assertTrue(sink.aborted)
        self.assertEqual(sink.committed_bytes, 0)

    def test_provider_facts_control_retention_availability_and_range_support(self) -> None:
        bad_source = ContentSource(
            uri=self.source.uri,
            digest="sha256:" + "0" * 64,
            provider=self.source.provider,
        )
        with self.assertRaisesRegex(RemoteContentConflict, "digest"):
            self.manager.retain(bad_source, owner_id="workspace:b")

        nonretaining = SyntheticRemoteProvider(
            self.manifest,
            retention_guaranteed=False,
        )
        with self.assertRaisesRegex(RemoteContentConflict, "retention"):
            RemoteContentManagerSpike({"temporary": nonretaining}).retain(
                ContentSource(
                    uri=self.source.uri,
                    digest=self.source.digest,
                    provider="temporary",
                ),
                owner_id="workspace:b",
            )

        unavailable = SyntheticRemoteProvider(self.manifest, available=False)
        unavailable_manager = RemoteContentManagerSpike({"offline": unavailable})
        unavailable_ref = unavailable_manager.retain(
            ContentSource(
                uri=self.source.uri,
                digest=self.source.digest,
                provider="offline",
            ),
            owner_id="workspace:b",
        )
        self.assertFalse(unavailable_manager.preflight(
            unavailable_ref.ref,
            owner_id="workspace:b",
        ).available)
        with self.assertRaises(RemoteContentUnavailable):
            unavailable_manager.open_range(
                unavailable_ref.ref,
                owner_id="workspace:b",
                offset=0,
                size_bytes=1,
            )

        no_ranges = SyntheticRemoteProvider(self.manifest, range_supported=False)
        no_range_manager = RemoteContentManagerSpike({"archive": no_ranges})
        no_range_ref = no_range_manager.retain(
            ContentSource(
                uri=self.source.uri,
                digest=self.source.digest,
                provider="archive",
            ),
            owner_id="workspace:b",
        )
        preflight = no_range_manager.preflight(
            no_range_ref.ref,
            owner_id="workspace:b",
            byte_range=(0, 10),
        )
        self.assertFalse(preflight.range_supported)
        with self.assertRaisesRegex(RemoteContentUnavailable, "range"):
            no_range_manager.open_range(
                no_range_ref.ref,
                owner_id="workspace:b",
                offset=0,
                size_bytes=10,
            )

    def test_unregistered_or_unretained_sources_cannot_be_opened(self) -> None:
        manager = RemoteContentManagerSpike({})
        with self.assertRaisesRegex(RemoteContentUnavailable, "registered"):
            manager.retain(self.source, owner_id="workspace:a")
        with self.assertRaisesRegex(RemoteContentUnavailable, "not available"):
            self.manager.open_range(
                "sha256:missing",
                owner_id=self.owner_id,
                offset=0,
                size_bytes=1,
            )

    def test_content_identity_is_not_authorization(self) -> None:
        with self.assertRaisesRegex(RemoteContentUnavailable, "not available"):
            self.manager.preflight(self.managed.ref, owner_id="workspace:other")

        second = self.manager.retain(self.source, owner_id="workspace:other")
        self.assertEqual(second.ref, self.managed.ref)
        preflight = self.manager.preflight(second.ref, owner_id="workspace:other")
        self.assertEqual(preflight.requested_bytes, self.total_size)


if __name__ == "__main__":
    unittest.main()
