"""Secure FastAPI application for the KYROSHIX BOT control panel."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import request_authenticated
from .shared import shared_state
from ..security import security
from .routes import (
    core_router,
    health_router,
    mapping_router,
    memory_router,
    music_router,
    pages_router,
    start_music_broadcast,
    start_state_broadcast,
    vision_router,
    vrchat_router,
    ws_router,
)

# stop_broadcast_tasks was added to the hardened websocket implementation.
from .routes.websocket import stop_broadcast_tasks


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths / configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = PROJECT_ROOT / "webui"

CONTROL_PANEL_HOST = "127.0.0.1"
CONTROL_PANEL_PORT = 8766
PUBLIC_HOST = "kyroshix-bot.uaru-elver.ts.net"

# HTTP methods that are expected by the application.
ALLOWED_HTTP_METHODS = {
    "GET",
    "HEAD",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
}


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Manage KYROSHIX BOT web background services.

    Broadcasters are started once when FastAPI starts and are cancelled
    cleanly when the application shuts down.
    """

    logger.info("web: starting KYROSHIX BOT control panel services")

    start_music_broadcast()
    start_state_broadcast()

    try:
        yield

    finally:
        logger.info("web: stopping KYROSHIX BOT control panel services")

        try:
            await stop_broadcast_tasks()
        except Exception:
            logger.exception(
                "web: failed to stop broadcast tasks cleanly"
            )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="KYROSHIX BOT Control Panel",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

PUBLIC_EXACT_PATHS = frozenset(
    {
        "/login",
        "/auth/login",
        "/favicon.ico",
    }
)


def _is_public_path(path: str) -> bool:
    """
    Return True only for resources required before authentication.

    Static assets remain public because login.html and the compiled UI can
    reference them. They must never contain credentials or private state.
    """

    if path in PUBLIC_EXACT_PATHS:
        return True

    if path.startswith("/static/"):
        return True

    return False


def _is_api_path(path: str) -> bool:
    return (
        path == "/api"
        or path.startswith("/api/")
        or path.startswith("/auth/")
    )


def _is_websocket_path(path: str) -> bool:
    return path == "/ws"


def _add_security_headers(
    response: Response,
    *,
    authenticated: bool,
) -> None:
    """
    Add browser security headers that are safe for the current dashboard.

    The public control-panel endpoint is HTTPS-only through Tailscale Funnel.
    HSTS is added only for requests that FastAPI itself sees as HTTPS; the
    local Funnel upstream remains plain HTTP on 127.0.0.1.
    """

    response.headers["X-Content-Type-Options"] = "nosniff"

    response.headers["Referrer-Policy"] = "no-referrer"

    response.headers["X-Frame-Options"] = "SAMEORIGIN"

    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'self'; "
        "form-action 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self' ws: wss:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'"
    )
    # Public access is HTTPS-only through Funnel.
    response.headers["Strict-Transport-Security"] = "max-age=31536000"

    # Funnel terminates TLS before forwarding to this loopback HTTP server.
    # Do not force HSTS on the local HTTP upstream.

    response.headers[
        "Permissions-Policy"
    ] = (
        "camera=(), "
        "geolocation=(), "
        "payment=(), "
        "usb=()"
    )

    # The control panel and login page should not be cached by browsers,
    # proxies, or shared machines.
    if authenticated:
        response.headers.setdefault(
            "Cache-Control",
            "no-store, private",
        )


# ---------------------------------------------------------------------------
# Security / authentication middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def security_middleware(
    request: Request,
    call_next,
):
    path = request.url.path
    method = request.method.upper()

    # DDoS / brute-force protection via SecurityManager (per-IP token bucket)
    key = request.client.host if request.client else "unknown"
    is_login = path == "/auth/login"

    allowed, reason = security.check_http(key, endpoint="login" if is_login else "")
    if not allowed:
        response = JSONResponse(
            status_code=429,
            content={"detail": f"Rate limited: {reason}"},
            headers={"Retry-After": "10"},
        )
        _add_security_headers(response, authenticated=True)
        return response

    # Request size limit
    content_length = request.headers.get("content-length")
    ok_size, size_reason = security.check_request_size(
        int(content_length) if content_length else None
    )
    if not ok_size:
        response = JSONResponse(
            status_code=413,
            content={"detail": size_reason},
        )
        _add_security_headers(response, authenticated=True)
        return response


    # Reject unexpected HTTP verbs early.
    if method not in ALLOWED_HTTP_METHODS:
        return JSONResponse(
            status_code=405,
            content={
                "detail": "Method not allowed",
            },
            headers={
                "Allow": ", ".join(
                    sorted(ALLOWED_HTTP_METHODS)
                )
            },
        )

    # WebSockets are authenticated in websocket.py before accept().
    #
    # Normally HTTP middleware is not invoked for a WebSocket scope, but this
    # check documents and preserves the intended security boundary.
    if _is_websocket_path(path):
        return await call_next(request)

    authenticated = request_authenticated(request)

    # ---------------------------------------------------------------
    # Unauthenticated requests
    # ---------------------------------------------------------------

    if not authenticated and not _is_public_path(path):

        # APIs must receive a machine-readable 401 rather than login HTML.
        if _is_api_path(path):
            response = JSONResponse(
                status_code=401,
                content={
                    "detail": "Authentication required",
                },
            )

            _add_security_headers(
                response,
                authenticated=True,
            )

            return response

        # Browser pages are redirected to the login page.
        response = RedirectResponse(
            url="/login",
            status_code=303,
        )

        _add_security_headers(
            response,
            authenticated=True,
        )

        return response

    # ---------------------------------------------------------------
    # Authenticated/public request
    # ---------------------------------------------------------------

    try:
        response = await call_next(request)

    except Exception:
        logger.exception(
            "web: unhandled request error: %s %s",
            method,
            path,
        )
        raise

    # Prevent authentication/API responses from being cached.
    sensitive_response = (
        authenticated
        or _is_api_path(path)
        or path == "/login"
    )

    _add_security_headers(
        response,
        authenticated=sensitive_response,
    )

    return response


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

if not STATIC_DIR.exists():
    raise RuntimeError(
        f"KYROSHIX BOT web UI directory was not found: {STATIC_DIR}"
    )


app.mount(
    "/static",
    StaticFiles(
        directory=str(STATIC_DIR),
        check_dir=True,
    ),
    name="static",
)


# ---------------------------------------------------------------------------
# Application routers
# ---------------------------------------------------------------------------

app.include_router(pages_router)
app.include_router(core_router)
app.include_router(health_router)
app.include_router(music_router)
app.include_router(memory_router)
app.include_router(vrchat_router)
app.include_router(mapping_router)
app.include_router(vision_router)
app.include_router(ws_router)


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_control_server(
    host: str = CONTROL_PANEL_HOST,
    port: int = CONTROL_PANEL_PORT,
):
    """
    Start the KYROSHIX BOT control panel.

    The application listens only on loopback. Tailscale Funnel terminates
    public HTTPS and proxies requests to this local-only port.
    """

    cfg = shared_state.get("config")

    name = (
        getattr(cfg, "app_name", None)
        if cfg is not None
        else None
    ) or "KYROSHIX BOT"

    logger.info(
        "web: starting %s control panel on %s:%s",
        name,
        host,
        port,
    )

    print()
    print("=" * 54)
    print(f"  {name} - Secure Control Panel")
    print("=" * 54)
    print()
    print(f"  Local : http://127.0.0.1:{port}")
    print("  Public: https://kyroshix-bot.tailc0cc06.ts.net/")
    print()
    print("  Authentication: ENABLED")
    print("  HTTPS: ENABLED via Tailscale Funnel")
    print()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",

        # The app does not need client proxy headers for authentication.
        # Keep them disabled to avoid trusting spoofed forwarding metadata.
        proxy_headers=False,

        # Avoid advertising Uvicorn in normal HTTP responses.
        server_header=False,

        # No automatic date header is necessary for this local control API.
        date_header=False,

        # Keep a conservative WebSocket payload ceiling at the server layer.
        ws_max_size=32 * 1024,

        # Periodic WebSocket heartbeat at the protocol layer.
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
    )