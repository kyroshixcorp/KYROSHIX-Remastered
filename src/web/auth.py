from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import Request, WebSocket

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
AUTH_DIR = PROJECT_ROOT / "data"
AUTH_FILE = AUTH_DIR / "web_auth.env"

SESSION_COOKIE = "kyroshix_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7
PBKDF2_ITERATIONS = 600_000
MAX_SESSIONS = 128

_sessions: dict[str, float] = {}
_lock = threading.RLock()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _read_auth_file() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        if not AUTH_FILE.is_file():
            return result
        for raw_line in AUTH_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    except (OSError, UnicodeError):
        return {}
    return result


def auth_configured() -> bool:
    cfg = _read_auth_file()
    return bool(cfg.get("USERNAME") and cfg.get("PASSWORD_SALT") and cfg.get("PASSWORD_HASH"))


def create_credentials(username: str, password: str) -> None:
    username = username.strip()
    if not username or len(username) > 128:
        raise ValueError("Username must contain 1-128 characters")
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must contain 12-1024 characters")

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(32)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()

    content = "\n".join([
        "# KYROSHIX BOT Web Authentication",
        f"USERNAME={username}",
        f"PASSWORD_SALT={salt}",
        f"PASSWORD_HASH={password_hash}",
        "",
    ])
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, AUTH_FILE)
    destroy_all_sessions()


def verify_credentials(username: str, password: str) -> bool:
    if len(username) > 128 or len(password) > 1024:
        return False
    cfg = _read_auth_file()
    expected_username = cfg.get("USERNAME", "")
    salt = cfg.get("PASSWORD_SALT", "")
    expected_hash = cfg.get("PASSWORD_HASH", "")
    if not expected_username or not salt or not expected_hash:
        return False
    try:
        salt_bytes = bytes.fromhex(salt)
        if len(salt_bytes) < 16:
            return False
        calculated_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt_bytes, PBKDF2_ITERATIONS
        ).hex()
    except (ValueError, TypeError, UnicodeError):
        return False
    return (
        hmac.compare_digest(username, expected_username)
        and hmac.compare_digest(calculated_hash, expected_hash)
    )


def cleanup_sessions() -> None:
    now = time.time()
    with _lock:
        for digest, expires in list(_sessions.items()):
            if expires < now:
                _sessions.pop(digest, None)


def create_session() -> str:
    cleanup_sessions()
    token = secrets.token_urlsafe(48)
    digest = _token_digest(token)
    with _lock:
        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions, key=_sessions.get)
            _sessions.pop(oldest, None)
        _sessions[digest] = time.time() + SESSION_MAX_AGE
    return token


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _sessions.pop(_token_digest(token), None)


def destroy_all_sessions() -> None:
    with _lock:
        _sessions.clear()


def _session_valid(token: str | None) -> bool:
    if not token or len(token) > 256:
        return False
    digest = _token_digest(token)
    now = time.time()
    with _lock:
        expires = _sessions.get(digest)
        if expires is None:
            return False
        if expires < now:
            _sessions.pop(digest, None)
            return False
        return True


def request_authenticated(request: Request) -> bool:
    return _session_valid(request.cookies.get(SESSION_COOKIE))


def websocket_authenticated(ws: WebSocket) -> bool:
    return _session_valid(ws.cookies.get(SESSION_COOKIE))
