"""Real SQLite contention during fresh Realm/provider initialization."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from unittest.mock import patch

from optpilot.realm._sqlite_bootstrap import enable_wal
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.local_process_supervisor import LocalProcessSupervisor
from optpilot.realm.local_runtime import LocalRealmRuntime


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class SQLiteBootstrapTest(unittest.TestCase):
    def test_fresh_ledger_and_provider_wait_for_a_concurrent_initializer(self) -> None:
        for kind in ("ledger", "provider"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                database = root / (
                    "realm.sqlite3" if kind == "ledger" else "process_registry.sqlite3"
                )
                holder = sqlite3.connect(database, isolation_level=None)
                holder.execute("BEGIN IMMEDIATE")
                started = threading.Event()
                real_connect = sqlite3.connect

                def observed_connect(*args, **kwargs):
                    connection = real_connect(*args, **kwargs)
                    connection.set_trace_callback(
                        lambda statement: started.set()
                        if statement == "PRAGMA journal_mode = WAL"
                        else None
                    )
                    return connection

                def open_database():
                    if kind == "ledger":
                        with RealmLedger(database) as ledger:
                            connection = ledger._connect()
                            try:
                                return (
                                    ledger.realm_id,
                                    connection.execute("PRAGMA journal_mode").fetchone()[0],
                                    connection.execute("PRAGMA synchronous").fetchone()[0],
                                    connection.execute("PRAGMA busy_timeout").fetchone()[0],
                                )
                            finally:
                                connection.close()
                    provider = LocalProcessSupervisor(root)
                    connection = provider._connect()
                    try:
                        return (
                            provider.provider_generation,
                            connection.execute("PRAGMA journal_mode").fetchone()[0],
                            connection.execute("PRAGMA synchronous").fetchone()[0],
                            connection.execute("PRAGMA busy_timeout").fetchone()[0],
                        )
                    finally:
                        connection.close()

                with patch("sqlite3.connect", side_effect=observed_connect):
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(open_database)
                        try:
                            self.assertTrue(started.wait(timeout=5))
                            # The old WAL transition fails immediately here,
                            # even with its 10/30-second SQLite busy timeout.
                            with self.assertRaises(TimeoutError):
                                future.result(timeout=0.05)
                        finally:
                            holder.rollback()
                            holder.close()
                        identity, mode, synchronous, timeout_ms = future.result(timeout=5)
                self.assertTrue(identity)
                self.assertEqual(mode, "wal")
                self.assertEqual(synchronous, 2)  # FULL remains in force.
                self.assertEqual(timeout_ms, 10_000 if kind == "ledger" else 30_000)

    def test_wal_contention_expires_and_restores_the_existing_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "realm.sqlite3"
            holder = sqlite3.connect(database, isolation_level=None)
            connection = sqlite3.connect(database, isolation_level=None)
            try:
                holder.execute("BEGIN IMMEDIATE")
                connection.execute("PRAGMA busy_timeout = 40")
                started = time.monotonic()
                with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                    enable_wal(connection)
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 0.03)
                self.assertLess(elapsed, 1.0)
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 40)
                holder.rollback()
                self.assertEqual(enable_wal(connection), "wal")
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 40)
            finally:
                connection.close()
                holder.close()

    def test_failed_ledger_bootstrap_closes_its_connection(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "realm.sqlite3"
            holder = sqlite3.connect(database, isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")
            opened = []
            real_connect = sqlite3.connect

            def observed_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                opened.append(connection)
                return connection

            try:
                with patch("sqlite3.connect", side_effect=observed_connect):
                    with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                        RealmLedger(database, busy_timeout_ms=40)
                self.assertTrue(opened)
                for connection in opened:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute("SELECT 1")
            finally:
                holder.close()

    def test_legacy_busy_message_is_retried_but_other_lock_errors_are_not(self) -> None:
        for message, code, retried in (
            ("database is locked", None, True),
            ("database table is locked", None, False),
            ("database is locked", 6, False),  # SQLITE_LOCKED, not SQLITE_BUSY
            ("attempt to write a readonly database", None, False),
        ):
            with self.subTest(message=message, code=code), tempfile.TemporaryDirectory() as folder:
                failure = sqlite3.OperationalError(message)
                if code is not None:
                    failure.sqlite_errorcode = code
                attempts = []

                class OneFailureConnection(sqlite3.Connection):
                    def execute(self, statement, *args, **kwargs):
                        if statement == "PRAGMA journal_mode = WAL":
                            attempts.append(statement)
                            if len(attempts) == 1:
                                raise failure
                        return super().execute(statement, *args, **kwargs)

                connection = sqlite3.connect(
                    Path(folder) / "fresh.sqlite3",
                    isolation_level=None,
                    factory=OneFailureConnection,
                )
                try:
                    connection.execute("PRAGMA busy_timeout = 100")
                    if retried:
                        self.assertEqual(enable_wal(connection), "wal")
                        self.assertEqual(len(attempts), 2)
                    else:
                        with self.assertRaises(sqlite3.OperationalError) as caught:
                            enable_wal(connection)
                        self.assertIs(caught.exception, failure)
                        self.assertEqual(len(attempts), 1)
                    self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 100)
                finally:
                    connection.close()

    def test_non_busy_database_error_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "invalid.sqlite3"
            database.write_bytes(b"not a SQLite database" * 100)
            connection = sqlite3.connect(database, isolation_level=None)
            try:
                connection.execute("PRAGMA busy_timeout = 40")
                with patch("optpilot.realm._sqlite_bootstrap.time.sleep") as sleep:
                    with self.assertRaisesRegex(sqlite3.DatabaseError, "not a database"):
                        enable_wal(connection)
                sleep.assert_not_called()
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 40)
            finally:
                connection.close()

    def test_concurrent_fresh_runtimes_share_one_canonical_realm(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "realm"
            barrier = threading.Barrier(4)

            def open_runtime(_index):
                barrier.wait(timeout=5)
                with LocalRealmRuntime.open(realm_root=root, refresh_capacity=False) as runtime:
                    return runtime.ledger.realm_id, runtime.content_store.root_marker

            with ThreadPoolExecutor(max_workers=4) as executor:
                identities = list(executor.map(open_runtime, range(4)))
            self.assertEqual(len(set(identities)), 1)
            with LocalRealmRuntime.open(realm_root=root, refresh_capacity=False) as runtime:
                self.assertEqual((runtime.ledger.realm_id, runtime.content_store.root_marker), identities[0])
                self.assertEqual(runtime.ledger.integrity_check()["journal_mode"], "wal")
