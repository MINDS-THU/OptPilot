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


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m optpilot_studio.ui.shared_auth")
    parser.add_argument("credentials_path", type=Path)
    parser.add_argument("--username", default="students")
    args = parser.parse_args(argv)
    import getpass

    first = getpass.getpass("Shared login password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        parser.error("Passwords do not match.")
    write_credentials(args.credentials_path, username=args.username, password=first)
    print(f"Wrote private credentials to {args.credentials_path.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
