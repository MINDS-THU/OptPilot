from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import ContentRejected
from optpilot.realm.interface_outputs import (
    INTERFACE_OUTPUT_SCHEMA,
    InterfaceOutputKind,
    InterfaceOutputRecord,
    read_interface_output_records,
    require_idempotent_generation,
    seal_interface_output_generation,
)
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.service import RealmContentService
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


class RealmInterfaceOutputsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.outputs = self.root / "launch-output"
        self.outputs.mkdir()
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="interface-output/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="interface-output/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.ledger.create_owner(
            operation_id="interface-output/owner",
            owner_id="interface-session-owner",
            owner_kind="interface-session",
            principal_id="operator",
        )
        self.change = self.ledger.begin_owner_change(
            operation_id="interface-output/begin",
            actor_principal_id="operator",
            owner_id="interface-session-owner",
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        self.content = RealmContentService(
            self.ledger, local_stores={self.store.store_id: self.store}
        )
        self.capture = self.content.capture(
            actor_principal_id="operator",
            change_id=self.change.change_id,
            store_id=self.store.store_id,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    @staticmethod
    def _record(**overrides):
        value = {
            "schema_version": INTERFACE_OUTPUT_SCHEMA,
            "id": "generated-project-01",
            "label": "Generated simulator",
            "kind": "tree",
            "root": "output",
            "path": "generation-01",
        }
        value.update(overrides)
        return value

    def _control(self, *records, trailing: bytes = b"") -> Path:
        path = self.root / "outputs.jsonl"
        payload = b"".join(
            json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
            for record in records
        )
        path.write_bytes(payload + trailing)
        return path

    def test_reads_complete_records_and_ignores_unterminated_tail(self) -> None:
        path = self._control(
            self._record(),
            trailing=b'{"schema_version":"optpilot.interface.output.v1"',
        )

        records = read_interface_output_records(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].output_id, "generated-project-01")
        self.assertIs(records[0].kind, InterfaceOutputKind.TREE)
        only_tail = self.root / "only-tail.jsonl"
        only_tail.write_text(json.dumps(self._record()), encoding="utf-8")
        self.assertEqual(read_interface_output_records(only_tail), ())

    def test_exact_duplicate_is_idempotent_but_conflicting_id_is_rejected(self) -> None:
        record = self._record()
        self.assertEqual(
            len(read_interface_output_records(self._control(record, record))), 1
        )

        with self.assertRaisesRegex(ContentRejected, "reused"):
            read_interface_output_records(
                self._control(record, self._record(path="generation-02"))
            )
        with self.assertRaisesRegex(ContentRejected, "exceeds 1 records"):
            read_interface_output_records(
                self._control(record, record), max_records=1
            )

    def test_rejects_extra_fields_traversal_unknown_kind_and_oversized_control(self) -> None:
        cases = (
            self._record(host_path="/tmp/escape"),
            self._record(path="../escape"),
            self._record(kind="workspace"),
        )
        for index, record in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(ContentRejected):
                    read_interface_output_records(self._control(record))
        with self.assertRaisesRegex(ContentRejected, "exceeds"):
            read_interface_output_records(
                self._control(self._record()), max_control_bytes=16
            )

    def test_control_file_symlink_is_rejected(self) -> None:
        target = self._control(self._record())
        link = self.root / "outputs-link.jsonl"
        link.symlink_to(target)
        with self.assertRaises(ContentRejected):
            read_interface_output_records(link)

    def test_seals_tree_and_file_below_granted_root_without_paths_in_receipt(self) -> None:
        tree = self.outputs / "generation-01"
        tree.mkdir()
        (tree / "run.py").write_text("print('ready')\n", encoding="utf-8")
        file_path = self.outputs / "summary.json"
        file_path.write_text('{"ok":true}\n', encoding="utf-8")
        tree_record = InterfaceOutputRecord.from_dict(self._record())
        file_record = InterfaceOutputRecord.from_dict(
            self._record(
                id="summary-01",
                label="Summary",
                kind="file",
                path="summary.json",
            )
        )

        sealed_tree = seal_interface_output_generation(
            self.capture,
            record=tree_record,
            root_handles={"output": self.outputs},
            operation_id="interface-output/seal/tree",
        )
        sealed_file = seal_interface_output_generation(
            self.capture,
            record=file_record,
            root_handles={"output": self.outputs},
        )

        self.assertEqual(sealed_tree.logical_bytes, len("print('ready')\n"))
        self.assertEqual(sealed_file.logical_bytes, len('{"ok":true}\n'))
        serialized = json.dumps(
            [sealed_tree.to_dict(), sealed_file.to_dict()], sort_keys=True
        )
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("relative_path", serialized)
        self.store.verify_tree(sealed_tree.content_ref, verify_children=True)

    def test_ungranted_handle_and_symlink_selection_fail_capture(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        (self.outputs / "escape").symlink_to(outside, target_is_directory=True)
        record = InterfaceOutputRecord.from_dict(self._record(path="escape"))

        with self.assertRaisesRegex(ContentRejected, "not granted"):
            seal_interface_output_generation(
                self.capture,
                record=record,
                root_handles={"workspace": self.outputs},
            )
        with self.assertRaises(ContentRejected):
            seal_interface_output_generation(
                self.capture,
                record=record,
                root_handles={"output": self.outputs},
            )

    def test_sealed_duplicate_id_requires_same_content_identity(self) -> None:
        first = self.outputs / "first"
        second = self.outputs / "second"
        first.mkdir()
        second.mkdir()
        (first / "run.py").write_text("print(1)\n", encoding="utf-8")
        (second / "run.py").write_text("print(2)\n", encoding="utf-8")
        first_record = InterfaceOutputRecord.from_dict(self._record(path="first"))
        second_record = InterfaceOutputRecord.from_dict(self._record(path="second"))
        sealed_first = seal_interface_output_generation(
            self.capture,
            record=first_record,
            root_handles={"output": self.outputs},
            operation_id="interface-output/seal/first",
        )
        sealed_first_replay = seal_interface_output_generation(
            self.capture,
            record=first_record,
            root_handles={"output": self.outputs},
            operation_id="interface-output/seal/first",
        )
        self.assertEqual(
            require_idempotent_generation(sealed_first, sealed_first_replay),
            sealed_first,
        )

        sealed_second = seal_interface_output_generation(
            self.capture,
            record=second_record,
            root_handles={"output": self.outputs},
            operation_id="interface-output/seal/second",
        )
        with self.assertRaisesRegex(ContentRejected, "different sealed generation"):
            require_idempotent_generation(sealed_first, sealed_second)


if __name__ == "__main__":
    unittest.main()
