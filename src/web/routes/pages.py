"""Pages and authentication routes for KYROSHIX BOT."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import (
    SESSION_COOKIE, SESSION_MAX_AGE, auth_configured, create_session,
    destroy_session, request_authenticated, verify_credentials,
)
from ..security import security
from ..shared import shared_state

STATIC_DIR = Path(__file__).resolve().parent.parent.parent.parent / "webui"
router = APIRouter()


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


def _client_key(request: Request) -> str:
    # Funnel is the only public ingress. Do not trust spoofable forwarding headers.
    return request.client.host if request.client else "unknown"


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
    allowed, reason = security.check_http(key, endpoint="login")
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many login attempts: {reason}. Try again later."},
            headers={"Retry-After": "10", "Cache-Control": "no-store"},
        )
    if not auth_configured():
        return JSONResponse(status_code=503, content={"detail": "Web authentication is not configured"})

    valid = await asyncio.to_thread(
        verify_credentials, body.username.strip(), body.password
    )
    if not valid:
        security.record_login_failure(key)
        await asyncio.sleep(0.35)
        return JSONResponse(status_code=401, content={"detail": "Invalid username or password"})

    security.record_login_success(key)
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
