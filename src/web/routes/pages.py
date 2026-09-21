"""Pages and authentication routes for KYROSHIX BOT."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import (
    SESSION_COOKIE, SESSION_MAX_AGE, auth_configured, create_session,
    destroy_session, request_authenticated, verify_credentials,
)
from ..shared import shared_state

STATIC_DIR = Path(__file__).resolve().parent.parent.parent.parent / "webui"
router = APIRouter()

LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_ATTEMPTS = 8
LOGIN_BLOCK_SECONDS = 120
MAX_TRACKED_CLIENTS = 2048
_attempts: dict[str, deque[float]] = defaultdict(deque)
_blocked_until: dict[str, float] = {}
_attempt_lock = threading.RLock()


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


def _client_key(request: Request) -> str:
    # Funnel is the only public ingress. Do not trust spoofable forwarding headers.
    return request.client.host if request.client else "unknown"


def _rate_state(key: str) -> tuple[bool, int]:
    now = time.monotonic()
    with _attempt_lock:
        until = _blocked_until.get(key, 0.0)
        if until > now:
            return False, max(1, int(until - now))
        _blocked_until.pop(key, None)
        q = _attempts[key]
        while q and now - q[0] > LOGIN_WINDOW_SECONDS:
            q.popleft()
        if len(q) >= LOGIN_MAX_ATTEMPTS:
            _blocked_until[key] = now + LOGIN_BLOCK_SECONDS
            q.clear()
            return False, LOGIN_BLOCK_SECONDS
        if len(_attempts) > MAX_TRACKED_CLIENTS:
            for stale in list(_attempts)[: len(_attempts) // 4]:
                if not _attempts[stale]:
                    _attempts.pop(stale, None)
                    _blocked_until.pop(stale, None)
        return True, 0


def _record_failure(key: str) -> None:
    with _attempt_lock:
        _attempts[key].append(time.monotonic())


def _clear_failures(key: str) -> None:
    with _attempt_lock:
        _attempts.pop(key, None)
        _blocked_until.pop(key, None)


def _require_obs():
    cfg = shared_state.get("config")
    if not cfg or not cfg.obs_enabled:
        raise HTTPException(status_code=404, detail="OBS overlay is disabled (obs.enabled: false)")


def _html(path: Path) -> HTMLResponse:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Page not found")
    return HTMLResponse(
        path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, private"},
    )


@router.get("/login")
async def login_page(request: Request):
    if request_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return _html(STATIC_DIR / "login.html")


@router.post("/auth/login")
async def login(body: LoginInput, request: Request):
    key = _client_key(request)
    allowed, retry = _rate_state(key)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many login attempts. Try again later."},
            headers={"Retry-After": str(retry), "Cache-Control": "no-store"},
        )
    if not auth_configured():
        return JSONResponse(status_code=503, content={"detail": "Web authentication is not configured"})

    valid = await asyncio.to_thread(
        verify_credentials, body.username.strip(), body.password
    )
    if not valid:
        _record_failure(key)
        await asyncio.sleep(0.35)
        return JSONResponse(status_code=401, content={"detail": "Invalid username or password"})

    _clear_failures(key)
    old = request.cookies.get(SESSION_COOKIE)
    destroy_session(old)
    token = create_session()
    response = JSONResponse({"success": True, "message": "Authenticated"})
    response.set_cookie(
        key=SESSION_COOKIE, value=token, max_age=SESSION_MAX_AGE,
        httponly=True, secure=True, samesite="strict", path="/",
    )
    response.headers["Cache-Control"] = "no-store, private"
    return response


@router.post("/auth/logout")
async def logout(request: Request):
    destroy_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"success": True, "message": "Logged out"})
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
    )
    response.headers["Cache-Control"] = "no-store, private"
    return response


@router.get("/")
async def index():
    return _html(STATIC_DIR / "index.html")


@router.get("/overlay")
async def overlay():
    _require_obs()
    return _html(STATIC_DIR / "overlay.html")


@router.get("/overlay/config")
async def overlay_config():
    _require_obs()
    return _html(STATIC_DIR / "overlay_config.html")


@router.get("/overlay/music")
async def overlay_music():
    _require_obs()
    return _html(STATIC_DIR / "overlay_music.html")
