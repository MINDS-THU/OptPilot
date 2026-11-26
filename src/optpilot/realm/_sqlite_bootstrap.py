"""Bounded initialization of SQLite WAL mode, before application transactions."""

from __future__ import annotations

import sqlite3
import time


def enable_wal(connection: sqlite3.Connection) -> str:
    """Retry only a contended WAL transition within the current busy timeout.

    Concurrent fresh openers can both hold a read lock while journal_mode tries
    to promote one to an exclusive lock. SQLite may report SQLITE_BUSY without
    invoking its busy handler in that case. Repeating this idempotent PRAGMA
    after the other opener advances is safe; replaying an application operation
    or an entire transaction would not be.
    """

    timeout_ms = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        while True:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            connection.execute(f"PRAGMA busy_timeout = {remaining_ms:d}")
            try:
                return str(
                    connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                ).lower()
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", None)
                # Python 3.10 has no sqlite_errorcode; match only SQLite's
                # exact BUSY message, never other database/permission errors.
                busy = (
                    str(error) == "database is locked"
                    if code is None
                    else code & 0xFF == 5  # SQLITE_BUSY primary result code
                )
                remaining = deadline - time.monotonic()
                if not busy or remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))
    finally:
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms:d}")
