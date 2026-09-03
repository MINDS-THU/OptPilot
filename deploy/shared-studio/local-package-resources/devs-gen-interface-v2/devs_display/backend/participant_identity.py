"""Durable, pseudonymous browser identities for the standalone interface.

The browser receives only a signed, opaque participant token.  Ownership is
enforced by the backend; JavaScript never needs to read or manufacture the
identity.  A small SQLite registry makes creation and last-seen metadata
durable without putting project artifacts in the database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


PARTICIPANT_COOKIE_NAME = "devs_trial_identity"
PARTICIPANT_IDENTITY_MODE_ENV = "DEVS_DISPLAY_IDENTITY_MODE"
COOKIE_IDENTITY_MODE = "cookie"
LAUNCH_IDENTITY_MODE = "launch"
DEFAULT_PARTICIPANT_COOKIE_TTL_SECONDS = 30 * 24 * 60 * 60
_MIN_COOKIE_TTL_SECONDS = 60 * 60
_MAX_COOKIE_TTL_SECONDS = 365 * 24 * 60 * 60
_LAST_SEEN_WRITE_INTERVAL_SECONDS = 5 * 60
_PARTICIPANT_ID_RE = re.compile(r"participant_[0-9a-f]{32}")


def participant_identity_mode() -> str:
    """Return the configured ownership boundary for this backend process."""

    mode = (
        os.getenv(PARTICIPANT_IDENTITY_MODE_ENV, COOKIE_IDENTITY_MODE)
        .strip()
        .lower()
    )
    if mode not in {COOKIE_IDENTITY_MODE, LAUNCH_IDENTITY_MODE}:
        raise RuntimeError(
            f"{PARTICIPANT_IDENTITY_MODE_ENV} must be 'cookie' or 'launch'."
        )
    return mode


def launch_participant_id(data_root: Path | str) -> str:
    """Derive one stable owner for an OptPilot launch-scoped data root."""

    canonical_root = str(Path(data_root).expanduser().resolve())
    digest = hashlib.sha256(canonical_root.encode("utf-8")).hexdigest()[:32]
    return f"participant_{digest}"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def participant_cookie_ttl_seconds() -> int:
    return max(
        _MIN_COOKIE_TTL_SECONDS,
        min(
            _MAX_COOKIE_TTL_SECONDS,
            _env_int(
                "DEVS_DISPLAY_PARTICIPANT_COOKIE_TTL_SECONDS",
                DEFAULT_PARTICIPANT_COOKIE_TTL_SECONDS,
            ),
        ),
    )


@dataclass(frozen=True)
class ParticipantIdentity:
    participant_id: str
    token: str
    expires_at: int
    set_cookie: bool


class ParticipantIdentityStore:
    """Issue and recover signed participant identities under one data root."""

    def __init__(self, data_root: Path | str):
        self.data_root = Path(data_root).expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.data_root / "participants.sqlite3"
        self.secret_path = self.data_root / ".participant-cookie-secret"
        self.ttl_seconds = participant_cookie_ttl_seconds()
        self._secret = self._load_or_create_secret()
        self._last_seen_cache: dict[str, int] = {}
        self._last_seen_lock = threading.Lock()
        self._initialize_database()

    def _load_or_create_secret(self) -> bytes:
        configured = os.getenv("DEVS_DISPLAY_PARTICIPANT_COOKIE_SECRET", "")
        if configured:
            return hashlib.sha256(configured.encode("utf-8")).digest()

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.secret_path, flags)
        except FileNotFoundError:
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                create_flags |= os.O_NOFOLLOW
            secret = secrets.token_bytes(32)
            try:
                descriptor = os.open(self.secret_path, create_flags, 0o600)
            except FileExistsError:
                descriptor = os.open(self.secret_path, flags)
            else:
                try:
                    os.fchmod(descriptor, 0o600)
                    os.write(descriptor, secret)
                finally:
                    os.close(descriptor)
                return secret

        try:
            secret = os.read(descriptor, 64)
        finally:
            os.close(descriptor)
        if len(secret) != 32:
            raise RuntimeError("Participant cookie secret is invalid.")
        return secret

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize_database(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS participants (
                        participant_id TEXT PRIMARY KEY,
                        created_at INTEGER NOT NULL,
                        last_seen_at INTEGER NOT NULL
                    )
                    """
                )
        os.chmod(self.database_path, 0o600)

    @staticmethod
    def _encode_signature(signature: bytes) -> str:
        return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")

    def _signature(self, participant_id: str, expires_at: int) -> str:
        payload = f"v1\0{participant_id}\0{expires_at}".encode("ascii")
        return self._encode_signature(
            hmac.new(self._secret, payload, hashlib.sha256).digest()
        )

    def _issue_token(self, participant_id: str, expires_at: int) -> str:
        return f"v1.{participant_id}.{expires_at}.{self._signature(participant_id, expires_at)}"

    def _decode_token(self, token: Optional[str], now: int) -> tuple[str, int] | None:
        if not token or len(token) > 512:
            return None
        try:
            version, participant_id, expires_raw, supplied_signature = token.split(
                ".", 3
            )
            expires_at = int(expires_raw)
        except (TypeError, ValueError):
            return None
        if (
            version != "v1"
            or _PARTICIPANT_ID_RE.fullmatch(participant_id) is None
            or expires_at < now
            or expires_at > now + _MAX_COOKIE_TTL_SECONDS
        ):
            return None
        expected = self._signature(participant_id, expires_at)
        if not hmac.compare_digest(supplied_signature, expected):
            return None
        return participant_id, expires_at

    def _touch_participant(self, participant_id: str, now: int) -> None:
        with self._last_seen_lock:
            previous = self._last_seen_cache.get(participant_id)
            if (
                previous is not None
                and now - previous < _LAST_SEEN_WRITE_INTERVAL_SECONDS
            ):
                return
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO participants(participant_id, created_at, last_seen_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(participant_id) DO UPDATE SET
                            last_seen_at = excluded.last_seen_at
                        """,
                        (participant_id, now, now),
                    )
            self._last_seen_cache[participant_id] = now

    def resolve(self, token: Optional[str]) -> ParticipantIdentity:
        now = int(time.time())
        decoded = self._decode_token(token, now)
        if decoded is None:
            participant_id = f"participant_{secrets.token_hex(16)}"
            expires_at = now + self.ttl_seconds
            set_cookie = True
        else:
            participant_id, expires_at = decoded
            renewal_window = max(60 * 60, self.ttl_seconds // 4)
            set_cookie = expires_at - now <= renewal_window
            if set_cookie:
                expires_at = now + self.ttl_seconds
        self._touch_participant(participant_id, now)
        return ParticipantIdentity(
            participant_id=participant_id,
            token=self._issue_token(participant_id, expires_at),
            expires_at=expires_at,
            set_cookie=set_cookie,
        )
