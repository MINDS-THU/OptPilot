"""Small server-side login boundary for shared OptPilot deployments."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Callable, Optional


COOKIE_NAME = "__Host-optpilot_session"
_CREDENTIAL_SCHEMA = "optpilot.shared-login-credentials.v1"
_SESSION_SCHEMA = "optpilot.shared-login-sessions.v1"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


class SharedAuthConfigurationError(ValueError):
    """Raised when the operator supplied an unsafe or malformed auth config."""


@dataclass(frozen=True)
class AuthPrincipal:
    """One authenticated Studio account, never a browser-supplied identity."""

    account_id: str
    username: str
    display_name: str
    role: str


@dataclass(frozen=True)
class SharedLoginCredentials:
    username: str
    salt: bytes
    password_hash: bytes
    n: int
    r: int
    p: int
    dklen: int

    @classmethod
    def load(cls, path: Path) -> "SharedLoginCredentials":
        candidate = path.expanduser()
        if candidate.is_symlink():
            raise SharedAuthConfigurationError(
                "Shared-login credentials must not be a symlink."
            )
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise SharedAuthConfigurationError(
                "Shared-login credentials must be a regular file."
            )
        if metadata.st_mode & 0o077:
            raise SharedAuthConfigurationError(
                "Shared-login credentials must not be readable by group or others."
            )
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            scrypt = payload["scrypt"]
            username = str(payload["username"]).strip()
            salt = base64.b64decode(scrypt["salt"], validate=True)
            password_hash = base64.b64decode(scrypt["hash"], validate=True)
            n = int(scrypt["n"])
            r = int(scrypt["r"])
            p = int(scrypt["p"])
            dklen = int(scrypt["dklen"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SharedAuthConfigurationError(
                "Shared-login credentials are malformed."
            ) from error
        if payload.get("schema") != _CREDENTIAL_SCHEMA:
            raise SharedAuthConfigurationError(
                "Shared-login credentials use an unsupported schema."
            )
        if not username or len(username) > 128:
            raise SharedAuthConfigurationError("Shared-login username is invalid.")
        if (
            len(salt) < 16
            or len(password_hash) != dklen
            or n < _SCRYPT_N
            or n & (n - 1)
            or r < 1
            or p < 1
            or dklen < 32
        ):
            raise SharedAuthConfigurationError(
                "Shared-login scrypt parameters are too weak or invalid."
            )
        return cls(username, salt, password_hash, n, r, p, dklen)

    def verifies(self, username: str, password: str) -> bool:
        candidate = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=self.salt,
            n=self.n,
            r=self.r,
            p=self.p,
            dklen=self.dklen,
        )
        username_matches = hmac.compare_digest(
            str(username).encode("utf-8"), self.username.encode("utf-8")
        )
        return username_matches and hmac.compare_digest(candidate, self.password_hash)


def write_credentials(path: Path, *, username: str, password: str) -> None:
    """Create one private shared-login verifier without storing the password."""

    username = str(username).strip()
    if not username or len(username) > 128:
        raise SharedAuthConfigurationError("Username must contain 1-128 characters.")
    if len(password) < 12:
        raise SharedAuthConfigurationError("Password must contain at least 12 characters.")
    destination = Path(os.path.abspath(path.expanduser()))
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if destination.is_symlink():
        raise SharedAuthConfigurationError("Credentials path must not be a symlink.")
    salt = secrets.token_bytes(24)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    payload = {
        "schema": _CREDENTIAL_SCHEMA,
        "username": username,
        "scrypt": {
            "salt": base64.b64encode(salt).decode("ascii"),
            "hash": base64.b64encode(password_hash).decode("ascii"),
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
            "dklen": _SCRYPT_DKLEN,
        },
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SharedAuthConfigurationError(
                "Credentials path must be a regular file."
            )
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(destination, 0o600)


class SharedAuth:
    """Verify one shared account and retain opaque sessions in SQLite."""

    def __init__(
        self,
        *,
        credentials: SharedLoginCredentials,
        database_path: Path,
        session_ttl_seconds: int = 12 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if session_ttl_seconds < 300:
            raise SharedAuthConfigurationError(
                "Shared-login session lifetime must be at least five minutes."
            )
        self.credentials = credentials
        database_candidate = database_path.expanduser()
        if database_candidate.is_symlink():
            raise SharedAuthConfigurationError("Session database must not be a symlink.")
        self.database_path = database_candidate.resolve()
        self.session_ttl_seconds = int(session_ttl_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._global_attempts: deque[float] = deque()
        self.database_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.database_path.is_symlink():
            raise SharedAuthConfigurationError("Session database must not be a symlink.")
        if not self.database_path.exists():
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.database_path, flags, 0o600)
            os.close(descriptor)
        metadata = self.database_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise SharedAuthConfigurationError(
                "Session database must be a private regular file."
            )
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO metadata(key, value)
                VALUES ('schema', 'optpilot.shared-login-sessions.v1');
                CREATE TABLE IF NOT EXISTS sessions (
                    token_digest TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_expiry
                ON sessions(expires_at);
                """
            )
            schema = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema'"
            ).fetchone()
            if not schema or schema[0] != _SESSION_SCHEMA:
                raise SharedAuthConfigurationError(
                    "Shared-login session database uses an unsupported schema."
                )
        os.chmod(self.database_path, 0o600)

    @classmethod
    def from_files(
        cls,
        *,
        credentials_path: Path,
        database_path: Path,
        session_ttl_seconds: int = 12 * 60 * 60,
    ) -> "SharedAuth":
        return cls(
            credentials=SharedLoginCredentials.load(credentials_path),
            database_path=database_path,
            session_ttl_seconds=session_ttl_seconds,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _prune_attempts(self, now: float) -> None:
        cutoff = now - 60
        while self._global_attempts and self._global_attempts[0] < cutoff:
            self._global_attempts.popleft()
        for key, attempts in list(self._attempts.items()):
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if not attempts:
                self._attempts.pop(key, None)

    def login(self, *, username: str, password: str, client_key: str) -> Optional[str]:
        """Return a fresh opaque token, ``None`` for invalid credentials.

        ``RuntimeError`` indicates a bounded rate limit and should map to 429.
        """

        now = self._clock()
        client_key = str(client_key or "unknown")[:200]
        with self._lock:
            self._prune_attempts(now)
            attempts = self._attempts[client_key]
            if len(attempts) >= 20 or len(self._global_attempts) >= 200:
                raise RuntimeError("Too many login attempts. Try again shortly.")
        if not self.credentials.verifies(username, password):
            with self._lock:
                self._attempts[client_key].append(now)
                self._global_attempts.append(now)
            return None
        with self._lock:
            self._attempts.pop(client_key, None)
        token = secrets.token_urlsafe(48)
        expires_at = now + self.session_ttl_seconds
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO sessions(token_digest, username, created_at, last_seen_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._digest(token), self.credentials.username, now, now, expires_at),
            )
        return token

    def verify_token(self, token: str) -> bool:
        if not token or len(token) > 256:
            return False
        now = self._clock()
        digest = self._digest(token)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT last_seen_at, expires_at FROM sessions WHERE token_digest = ?",
                (digest,),
            ).fetchone()
            if not row or float(row[1]) <= now:
                connection.execute("DELETE FROM sessions WHERE token_digest = ?", (digest,))
                return False
            if now - float(row[0]) >= 60:
                connection.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE token_digest = ?",
                    (now, digest),
                )
        return True

    def token_from_cookie(self, cookie_header: str) -> str:
        try:
            cookie = SimpleCookie()
            cookie.load(str(cookie_header or ""))
            morsel = cookie.get(COOKIE_NAME)
            return str(morsel.value) if morsel is not None else ""
        except Exception:
            return ""

    def verify_cookie(self, cookie_header: str) -> bool:
        return self.verify_token(self.token_from_cookie(cookie_header))

    def principal_from_cookie(self, cookie_header: str) -> Optional[AuthPrincipal]:
        if not self.verify_cookie(cookie_header):
            return None
        return AuthPrincipal(
            account_id="shared",
            username=self.credentials.username,
            display_name=self.credentials.username,
            role="admin",
        )

    @property
    def registration_enabled(self) -> bool:
        return False

    def logout_cookie(self, cookie_header: str) -> None:
        token = self.token_from_cookie(cookie_header)
        if not token:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_digest = ?", (self._digest(token),)
            )

    def session_cookie(self, token: str) -> str:
        return (
            f"{COOKIE_NAME}={token}; Path=/; Max-Age={self.session_ttl_seconds}; "
            "Secure; HttpOnly; SameSite=Lax"
        )

    @staticmethod
    def expired_cookie() -> str:
        return (
            f"{COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax"
        )


_CLASSROOM_AUTH_SCHEMA = "optpilot.classroom-auth.v1"
_ADMIN_USERNAME = "admin"
_RESERVED_USERNAMES = frozenset(
    {"admin", "administrator", "operator", "root", "system", "optpilot"}
)


def _normalized_username(value: str) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
    key = display.casefold()
    if len(display) < 2 or len(display) > 64:
        raise ValueError("Username must contain 2-64 characters.")
    if any(ord(character) < 32 for character in display):
        raise ValueError("Username contains unsupported characters.")
    if key in _RESERVED_USERNAMES:
        raise ValueError("This username is reserved.")
    return display, key


def _normalized_display_name(value: str, fallback: str) -> str:
    display = " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
    if not display:
        return fallback
    if len(display) > 80 or any(ord(character) < 32 for character in display):
        raise ValueError("Display name must contain at most 80 characters.")
    return display


def _password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        str(password).encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )


class ClassroomAuth:
    """Invite-only student accounts plus one startup-configured admin."""

    def __init__(
        self,
        *,
        database_path: Path,
        admin_password: str,
        invitation_code: str = "",
        registration_enabled: bool = False,
        session_ttl_seconds: int = 7 * 24 * 60 * 60,
        max_accounts: int = 100,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(admin_password) < 12:
            raise SharedAuthConfigurationError(
                "OPTPILOT_ADMIN_PASSWORD must contain at least 12 characters."
            )
        if registration_enabled and len(invitation_code) < 12:
            raise SharedAuthConfigurationError(
                "Registration requires an invitation code of at least 12 characters."
            )
        if session_ttl_seconds < 300:
            raise SharedAuthConfigurationError(
                "Classroom login session lifetime must be at least five minutes."
            )
        if max_accounts < 1 or max_accounts > 10000:
            raise SharedAuthConfigurationError(
                "Classroom account limit must be between 1 and 10000."
            )
        database_candidate = database_path.expanduser()
        if database_candidate.is_symlink():
            raise SharedAuthConfigurationError("Account database must not be a symlink.")
        database_candidate.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.database_path = database_candidate.resolve()
        if not self.database_path.exists():
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.database_path, flags, 0o600)
            os.close(descriptor)
        metadata = self.database_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise SharedAuthConfigurationError(
                "Account database must be a private regular file."
            )
        self.session_ttl_seconds = int(session_ttl_seconds)
        self.max_accounts = int(max_accounts)
        self._registration_enabled = bool(registration_enabled)
        self._clock = clock
        self._lock = threading.RLock()
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._global_attempts: deque[float] = deque()
        self._admin_salt = secrets.token_bytes(24)
        self._admin_password_hash = _password_digest(admin_password, self._admin_salt)
        self._invitation_hash = hashlib.sha256(
            str(invitation_code).encode("utf-8")
        ).digest()
        self._dummy_salt = secrets.token_bytes(24)
        self._dummy_password_hash = _password_digest(
            secrets.token_urlsafe(24), self._dummy_salt
        )
        self._initialize_database()
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO metadata(key, value)
                VALUES ('schema', 'optpilot.classroom-auth.v1');
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_login_at REAL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_digest TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS classroom_sessions_expiry
                ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS classroom_sessions_account
                ON sessions(account_id);
                CREATE TABLE IF NOT EXISTS asset_ownership (
                    asset_type TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    owner_account_id TEXT NOT NULL,
                    visibility TEXT NOT NULL DEFAULT 'private',
                    created_at REAL NOT NULL,
                    PRIMARY KEY (asset_type, asset_id)
                );
                CREATE INDEX IF NOT EXISTS classroom_assets_owner
                ON asset_ownership(owner_account_id, asset_type);
                CREATE TABLE IF NOT EXISTS asset_visibility_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_type TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    actor_account_id TEXT NOT NULL,
                    previous_visibility TEXT NOT NULL,
                    visibility TEXT NOT NULL,
                    changed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS classroom_asset_visibility_events_asset
                ON asset_visibility_events(asset_type, asset_id, event_id);
                """
            )
            schema = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema'"
            ).fetchone()
            if not schema or schema[0] != _CLASSROOM_AUTH_SCHEMA:
                raise SharedAuthConfigurationError(
                    "Account database uses an unsupported schema."
                )
            # An admin password is supplied anew at process startup. Do not let
            # a session issued under an older environment value survive it.
            connection.execute("DELETE FROM sessions WHERE role = 'admin'")

    @staticmethod
    def _asset_coordinate(asset_type: str, asset_id: str) -> tuple[str, str]:
        kind = str(asset_type or "").strip()
        identifier = str(asset_id or "").strip()
        if not kind or len(kind) > 64 or not identifier or len(identifier) > 512:
            raise ValueError("Asset ownership coordinate is invalid.")
        return kind, identifier

    def claim_asset(
        self,
        *,
        asset_type: str,
        asset_id: str,
        principal: AuthPrincipal,
        visibility: str = "private",
    ) -> None:
        """Bind a new durable Studio coordinate to one authenticated account."""

        kind, identifier = self._asset_coordinate(asset_type, asset_id)
        normalized_visibility = str(visibility or "private").casefold()
        if normalized_visibility not in {"private", "classroom"}:
            raise ValueError("Asset visibility must be private or classroom.")
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT owner_account_id FROM asset_ownership "
                "WHERE asset_type = ? AND asset_id = ?",
                (kind, identifier),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != principal.account_id:
                    raise PermissionError("This asset belongs to another account.")
                return
            connection.execute(
                "INSERT INTO asset_ownership(asset_type, asset_id, owner_account_id, visibility, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    kind,
                    identifier,
                    principal.account_id,
                    normalized_visibility,
                    self._clock(),
                ),
            )

    def can_access_asset(
        self,
        *,
        asset_type: str,
        asset_id: str,
        principal: AuthPrincipal,
        write: bool = False,
    ) -> bool:
        """Authorize an exact asset; unclaimed legacy data is admin-only."""

        if principal.role == "admin":
            return True
        kind, identifier = self._asset_coordinate(asset_type, asset_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT owner_account_id, visibility FROM asset_ownership "
                "WHERE asset_type = ? AND asset_id = ?",
                (kind, identifier),
            ).fetchone()
        if row is None:
            return False
        if str(row[0]) == principal.account_id:
            return True
        return not write and str(row[1]) == "classroom"

    def set_asset_visibility(
        self,
        *,
        asset_type: str,
        asset_id: str,
        visibility: str,
        principal: AuthPrincipal,
    ) -> None:
        """Let only the admin publish or privatize an existing asset."""

        if principal.role != "admin":
            raise PermissionError("Only the admin may change asset visibility.")
        kind, identifier = self._asset_coordinate(asset_type, asset_id)
        normalized_visibility = str(visibility or "").casefold()
        if normalized_visibility not in {"private", "classroom"}:
            raise ValueError("Asset visibility must be private or classroom.")
        with self._lock, self._connect() as connection:
            updated = connection.execute(
                "UPDATE asset_ownership SET visibility = ? "
                "WHERE asset_type = ? AND asset_id = ?",
                (normalized_visibility, kind, identifier),
            ).rowcount
        if not updated:
            raise KeyError(identifier)

    def asset_ownership(
        self, *, asset_type: str, asset_id: str
    ) -> Optional[dict[str, str]]:
        """Return one ownership policy without treating absence as access denial."""

        kind, identifier = self._asset_coordinate(asset_type, asset_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT owner_account_id, visibility FROM asset_ownership "
                "WHERE asset_type = ? AND asset_id = ?",
                (kind, identifier),
            ).fetchone()
        if row is None:
            return None
        return {
            "owner_account_id": str(row[0]),
            "visibility": str(row[1]),
        }

    def set_catalog_entry_visibility(
        self,
        *,
        asset_id: str,
        visibility: str,
        principal: AuthPrincipal,
    ) -> None:
        """Let a Catalog item's owner or the admin change its audience."""

        kind, identifier = self._asset_coordinate("catalog-entry", asset_id)
        normalized_visibility = str(visibility or "").casefold()
        if normalized_visibility not in {"private", "classroom"}:
            raise ValueError("Catalog visibility must be private or classroom.")
        now = self._clock()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT owner_account_id, visibility FROM asset_ownership "
                "WHERE asset_type = ? AND asset_id = ?",
                (kind, identifier),
            ).fetchone()
            if row is None:
                # Catalog entries that predate per-account ownership stay
                # classroom-visible.  Only the admin may adopt one in order to
                # make that legacy policy explicit or later privatize it.
                if principal.role != "admin":
                    raise PermissionError(
                        "Only the Catalog item owner or admin may change visibility."
                    )
                owner_account_id = principal.account_id
                previous_visibility = "classroom"
                connection.execute(
                    "INSERT INTO asset_ownership(asset_type, asset_id, owner_account_id, visibility, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        kind,
                        identifier,
                        owner_account_id,
                        normalized_visibility,
                        now,
                    ),
                )
            else:
                owner_account_id = str(row[0])
                previous_visibility = str(row[1])
                if (
                    principal.role != "admin"
                    and owner_account_id != principal.account_id
                ):
                    raise PermissionError(
                        "Only the Catalog item owner or admin may change visibility."
                    )
                connection.execute(
                    "UPDATE asset_ownership SET visibility = ? "
                    "WHERE asset_type = ? AND asset_id = ?",
                    (normalized_visibility, kind, identifier),
                )
            if previous_visibility != normalized_visibility:
                connection.execute(
                    "INSERT INTO asset_visibility_events("
                    "asset_type, asset_id, actor_account_id, previous_visibility, visibility, changed_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        kind,
                        identifier,
                        principal.account_id,
                        previous_visibility,
                        normalized_visibility,
                        now,
                    ),
                )

    def asset_visibility_events(
        self, *, asset_type: str, asset_id: str, limit: int = 100
    ) -> list[dict[str, object]]:
        """Return bounded newest-first visibility history for local auditing."""

        kind, identifier = self._asset_coordinate(asset_type, asset_id)
        bounded_limit = min(max(int(limit), 1), 1000)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT actor_account_id, previous_visibility, visibility, changed_at "
                "FROM asset_visibility_events "
                "WHERE asset_type = ? AND asset_id = ? "
                "ORDER BY event_id DESC LIMIT ?",
                (kind, identifier, bounded_limit),
            ).fetchall()
        return [
            {
                "actor_account_id": str(row[0]),
                "previous_visibility": str(row[1]),
                "visibility": str(row[2]),
                "changed_at": float(row[3]),
            }
            for row in rows
        ]

    def asset_owner_account_id(self, *, asset_type: str, asset_id: str) -> str:
        """Return a claimed asset's stable owner id, or an empty string."""

        kind, identifier = self._asset_coordinate(asset_type, asset_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT owner_account_id FROM asset_ownership "
                "WHERE asset_type = ? AND asset_id = ?",
                (kind, identifier),
            ).fetchone()
        return "" if row is None else str(row[0])

    @property
    def registration_enabled(self) -> bool:
        return self._registration_enabled

    def registration_status(self) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            account_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM accounts WHERE status = 'active'"
                ).fetchone()[0]
            )
        return {
            "enabled": self.registration_enabled,
            "account_count": account_count,
            "max_accounts": self.max_accounts,
            "capacity_available": account_count < self.max_accounts,
        }

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _prune_attempts(self, now: float) -> None:
        cutoff = now - 60
        while self._global_attempts and self._global_attempts[0] < cutoff:
            self._global_attempts.popleft()
        for key, attempts in list(self._attempts.items()):
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if not attempts:
                self._attempts.pop(key, None)

    def _check_attempt_limit(self, client_key: str, now: float) -> str:
        key = str(client_key or "unknown")[:200]
        self._prune_attempts(now)
        if len(self._attempts[key]) >= 20 or len(self._global_attempts) >= 200:
            raise RuntimeError("Too many authentication attempts. Try again shortly.")
        return key

    def _failed_attempt(self, client_key: str, now: float) -> None:
        with self._lock:
            self._attempts[client_key].append(now)
            self._global_attempts.append(now)

    def _create_session(
        self,
        connection: sqlite3.Connection,
        *,
        principal: AuthPrincipal,
        now: float,
    ) -> str:
        token = secrets.token_urlsafe(48)
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        connection.execute(
            "INSERT INTO sessions(token_digest, account_id, username, display_name, role, created_at, last_seen_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._digest(token),
                principal.account_id,
                principal.username,
                principal.display_name,
                principal.role,
                now,
                now,
                now + self.session_ttl_seconds,
            ),
        )
        return token

    def login(self, *, username: str, password: str, client_key: str) -> Optional[str]:
        now = self._clock()
        raw_username = " ".join(
            unicodedata.normalize("NFKC", str(username or "")).split()
        )
        username_key = raw_username.casefold()
        with self._lock:
            key = self._check_attempt_limit(client_key, now)
        if username_key == _ADMIN_USERNAME:
            candidate = _password_digest(password, self._admin_salt)
            if not hmac.compare_digest(candidate, self._admin_password_hash):
                self._failed_attempt(key, now)
                return None
            principal = AuthPrincipal("admin", "admin", "Admin", "admin")
            with self._lock, self._connect() as connection:
                token = self._create_session(
                    connection, principal=principal, now=now
                )
            with self._lock:
                self._attempts.pop(key, None)
            return token

        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT account_id, username, display_name, password_salt, password_hash "
                "FROM accounts WHERE username_key = ? AND status = 'active'",
                (username_key,),
            ).fetchone()
            salt = bytes(row[3]) if row else self._dummy_salt
            expected = bytes(row[4]) if row else self._dummy_password_hash
            candidate = _password_digest(password, salt)
            if row is None or not hmac.compare_digest(candidate, expected):
                self._failed_attempt(key, now)
                return None
            principal = AuthPrincipal(
                account_id=str(row[0]),
                username=str(row[1]),
                display_name=str(row[2]),
                role="student",
            )
            token = self._create_session(connection, principal=principal, now=now)
            connection.execute(
                "UPDATE accounts SET last_login_at = ? WHERE account_id = ?",
                (now, principal.account_id),
            )
        with self._lock:
            self._attempts.pop(key, None)
        return token

    def register(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        invitation_code: str,
        client_key: str,
    ) -> str:
        if not self.registration_enabled:
            raise PermissionError("Registration is currently closed.")
        canonical_username, username_key = _normalized_username(username)
        canonical_display_name = _normalized_display_name(
            display_name, canonical_username
        )
        if len(password) < 12:
            raise ValueError("Password must contain at least 12 characters.")
        if len(password) > 1024:
            raise ValueError("Password is too long.")
        now = self._clock()
        with self._lock:
            key = self._check_attempt_limit(client_key, now)
        invitation_hash = hashlib.sha256(
            str(invitation_code).encode("utf-8")
        ).digest()
        if not hmac.compare_digest(invitation_hash, self._invitation_hash):
            self._failed_attempt(key, now)
            raise PermissionError("The invitation code is invalid.")
        salt = secrets.token_bytes(24)
        password_hash = _password_digest(password, salt)
        account_id = f"account_{secrets.token_hex(16)}"
        principal = AuthPrincipal(
            account_id=account_id,
            username=canonical_username,
            display_name=canonical_display_name,
            role="student",
        )
        with self._lock, self._connect() as connection:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM accounts WHERE status = 'active'"
                ).fetchone()[0]
            )
            if count >= self.max_accounts:
                raise RuntimeError("Classroom registration has reached its account limit.")
            try:
                connection.execute(
                    "INSERT INTO accounts(account_id, username, username_key, display_name, password_salt, password_hash, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                    (
                        account_id,
                        canonical_username,
                        username_key,
                        canonical_display_name,
                        salt,
                        password_hash,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("This username is already registered.") from error
            token = self._create_session(connection, principal=principal, now=now)
        with self._lock:
            self._attempts.pop(key, None)
        return token

    def token_from_cookie(self, cookie_header: str) -> str:
        try:
            cookie = SimpleCookie()
            cookie.load(str(cookie_header or ""))
            morsel = cookie.get(COOKIE_NAME)
            return str(morsel.value) if morsel is not None else ""
        except Exception:
            return ""

    def principal_from_token(self, token: str) -> Optional[AuthPrincipal]:
        if not token or len(token) > 256:
            return None
        now = self._clock()
        digest = self._digest(token)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT account_id, username, display_name, role, last_seen_at, expires_at "
                "FROM sessions WHERE token_digest = ?",
                (digest,),
            ).fetchone()
            if not row or float(row[5]) <= now:
                connection.execute(
                    "DELETE FROM sessions WHERE token_digest = ?", (digest,)
                )
                return None
            if now - float(row[4]) >= 60:
                connection.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE token_digest = ?",
                    (now, digest),
                )
            if str(row[3]) == "student":
                active = connection.execute(
                    "SELECT 1 FROM accounts WHERE account_id = ? AND status = 'active'",
                    (str(row[0]),),
                ).fetchone()
                if not active:
                    connection.execute(
                        "DELETE FROM sessions WHERE token_digest = ?", (digest,)
                    )
                    return None
        return AuthPrincipal(
            account_id=str(row[0]),
            username=str(row[1]),
            display_name=str(row[2]),
            role=str(row[3]),
        )

    def principal_from_cookie(self, cookie_header: str) -> Optional[AuthPrincipal]:
        return self.principal_from_token(self.token_from_cookie(cookie_header))

    def verify_token(self, token: str) -> bool:
        return self.principal_from_token(token) is not None

    def verify_cookie(self, cookie_header: str) -> bool:
        return self.principal_from_cookie(cookie_header) is not None

    def logout_cookie(self, cookie_header: str) -> None:
        token = self.token_from_cookie(cookie_header)
        if not token:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_digest = ?", (self._digest(token),)
            )

    def session_cookie(self, token: str) -> str:
        return (
            f"{COOKIE_NAME}={token}; Path=/; Max-Age={self.session_ttl_seconds}; "
            "Secure; HttpOnly; SameSite=Lax"
        )

    @staticmethod
    def expired_cookie() -> str:
        return SharedAuth.expired_cookie()


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m optpilot_studio.ui.shared_auth")
    parser.add_argument("credentials_path", type=Path)
    parser.add_argument("--username", default="students")
    args = parser.parse_args(argv)
    import getpass

    first = getpass.getpass("Shared login password (at least 12 characters): ")
    if len(first) < 12:
        parser.error("Password must contain at least 12 characters.")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        parser.error("Passwords do not match.")
    try:
        write_credentials(args.credentials_path, username=args.username, password=first)
    except SharedAuthConfigurationError as error:
        parser.error(str(error))
    print(f"Wrote private credentials to {args.credentials_path.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
