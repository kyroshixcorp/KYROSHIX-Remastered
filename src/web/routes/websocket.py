"""Secure WebSocket endpoint and background broadcast loops for KYROSHIX BOT."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import websocket_authenticated
from ..shared import (
    broadcast_log,
    broadcast_state,
    get_full_state,
    shared_state,
    websocket_clients,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# WebSocket configuration
# ---------------------------------------------------------------------------

MAX_MESSAGE_SIZE = 16 * 1024  # 16 KiB
MAX_WEBSOCKET_CLIENTS = 8
WEBSOCKET_IDLE_TIMEOUT = 120.0

MUSIC_BROADCAST_INTERVAL = 1.0
STATE_BROADCAST_INTERVAL = 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _same_origin(ws: WebSocket) -> bool:
    """
    Basic same-origin validation for browser WebSocket connections.

    Browsers automatically send the Origin header when creating a WebSocket.
    We accept:
      - no Origin (allows non-browser/local clients)
      - Origin whose host matches the current HTTP Host header

    This is an additional protection. Authentication is still mandatory.
    """

    origin = ws.headers.get("origin")
    host = ws.headers.get("host")

    if not origin:
        return True

    if not host:
        return False

    try:
        origin = origin.strip().lower()
        host = host.strip().lower()

        allowed_http = f"http://{host}"
        allowed_https = f"https://{host}"

        return origin in {
            allowed_http,
            allowed_https,
        }

    except Exception:
        return False


async def _safe_close(
    ws: WebSocket,
    *,
    code: int = 1000,
    reason: str = "",
) -> None:
    """Close a WebSocket without allowing close errors to escape."""

    with suppress(Exception):
        await ws.close(
            code=code,
            reason=reason[:120],
        )


def _remove_client(ws: WebSocket) -> None:
    """Remove all references to a WebSocket from the client registry."""

    while ws in websocket_clients:
        with suppress(ValueError):
            websocket_clients.remove(ws)


async def _send_json(
    ws: WebSocket,
    payload: dict[str, Any],
) -> None:
    """Serialize and send a compact JSON WebSocket message."""

    await ws.send_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Authenticated KYROSHIX BOT WebSocket.

    Authentication uses the same HttpOnly session cookie as the web panel.
    The connection is rejected before accept() when authentication fails.
    """

    # Bound concurrent sockets to limit resource exhaustion.
    if len(websocket_clients) >= MAX_WEBSOCKET_CLIENTS:
        await _safe_close(ws, code=1013, reason="Server busy")
        return

    # Authentication MUST happen before accepting the connection.
    if not websocket_authenticated(ws):
        logger.warning(
            "websocket: rejected unauthenticated connection from %s",
            ws.client.host if ws.client else "unknown",
        )

        await _safe_close(
            ws,
            code=1008,
            reason="Authentication required",
        )
        return

    # Additional browser cross-origin protection.
    if not _same_origin(ws):
        logger.warning(
            "websocket: rejected invalid origin from %s (origin=%r)",
            ws.client.host if ws.client else "unknown",
            ws.headers.get("origin"),
        )

        await _safe_close(
            ws,
            code=1008,
            reason="Invalid origin",
        )
        return

    try:
        await ws.accept()
    except Exception:
        logger.exception("websocket: failed to accept connection")
        return

    if ws not in websocket_clients:
        websocket_clients.append(ws)

    client_address = (
        f"{ws.client.host}:{ws.client.port}"
        if ws.client
        else "unknown"
    )

    logger.info(
        "websocket: authenticated client connected (%s)",
        client_address,
    )

    try:
        # Send current state immediately after authentication.
        state = await asyncio.to_thread(get_full_state)

        await _send_json(
            ws,
            {
                "type": "state",
                "data": state,
            },
        )

        while True:
            try:
                message = await asyncio.wait_for(
                    ws.receive_text(),
                    timeout=WEBSOCKET_IDLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await _safe_close(ws, code=1001, reason="Idle timeout")
                break

            # Protect the server from unnecessarily large client messages.
            if len(message.encode("utf-8")) > MAX_MESSAGE_SIZE:
                logger.warning(
                    "websocket: oversized message rejected from %s",
                    client_address,
                )

                await _safe_close(
                    ws,
                    code=1009,
                    reason="Message too large",
                )
                break

            # The dashboard currently does not need to send commands over
            # WebSocket. Still support a tiny control protocol for connection
            # health and future compatibility.
            if not message:
                continue

            try:
                data = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                # Ignore unknown text messages instead of crashing the socket.
                continue

            if not isinstance(data, dict):
                continue

            message_type = data.get("type")

            if message_type == "ping":
                await _send_json(
                    ws,
                    {
                        "type": "pong",
                    },
                )

    except WebSocketDisconnect:
        pass

    except asyncio.CancelledError:
        raise

    except Exception:
        logger.exception(
            "websocket: connection error (%s)",
            client_address,
        )

    finally:
        _remove_client(ws)

        logger.info(
            "websocket: client disconnected (%s)",
            client_address,
        )


# ---------------------------------------------------------------------------
# Background broadcast state
# ---------------------------------------------------------------------------

_music_broadcast_task: asyncio.Task | None = None
_state_broadcast_task: asyncio.Task | None = None

_last_music_state: Any = None
_last_state_payload: str | None = None


# ---------------------------------------------------------------------------
# Music broadcast
# ---------------------------------------------------------------------------

async def _music_broadcast_loop():
    global _last_music_state

    logger.info("websocket: music broadcast loop started")

    try:
        while True:
            await asyncio.sleep(MUSIC_BROADCAST_INTERVAL)

            try:
                audio_mgr = shared_state.get("audio_mgr")

                if (
                    not audio_mgr
                    or not hasattr(audio_mgr, "get_music_progress")
                ):
                    if _last_music_state is not None:
                        _last_music_state = None

                        await broadcast_log(
                            {
                                "type": "music_update",
                                "content": "",
                                "extra": {
                                    "playing": False,
                                },
                            }
                        )

                    continue

                # Some audio implementations may perform blocking work.
                prog = await asyncio.to_thread(
                    audio_mgr.get_music_progress
                )

                if prog:
                    lyric = None

                    if hasattr(audio_mgr, "get_current_lyric"):
                        try:
                            lyric = await asyncio.to_thread(
                                audio_mgr.get_current_lyric
                            )
                        except Exception:
                            logger.debug(
                                "websocket: unable to read current lyric",
                                exc_info=True,
                            )

                    is_playing = True

                    if hasattr(audio_mgr, "is_music_playing"):
                        try:
                            is_playing = await asyncio.to_thread(
                                audio_mgr.is_music_playing
                            )
                        except Exception:
                            logger.debug(
                                "websocket: unable to read music state",
                                exc_info=True,
                            )

                    song_name = str(
                        prog.get("song_name", "Unknown")
                        or "Unknown"
                    )

                    try:
                        position = round(
                            float(prog.get("position", 0) or 0),
                            1,
                        )
                    except (TypeError, ValueError):
                        position = 0.0

                    try:
                        duration = round(
                            float(prog.get("duration", 0) or 0),
                            1,
                        )
                    except (TypeError, ValueError):
                        duration = 0.0

                    try:
                        progress = round(
                            float(prog.get("progress", 0) or 0),
                            3,
                        )
                    except (TypeError, ValueError):
                        progress = 0.0

                    # Keep progress inside its expected range.
                    progress = max(
                        0.0,
                        min(1.0, progress),
                    )

                    await broadcast_log(
                        {
                            "type": "music_update",
                            "content": song_name,
                            "extra": {
                                "playing": bool(is_playing),
                                "song_name": song_name,
                                "position": position,
                                "duration": duration,
                                "progress": progress,
                                "lyric": lyric,
                            },
                        }
                    )

                    _last_music_state = True

                elif _last_music_state is not None:
                    _last_music_state = None

                    await broadcast_log(
                        {
                            "type": "music_update",
                            "content": "",
                            "extra": {
                                "playing": False,
                            },
                        }
                    )

            except asyncio.CancelledError:
                raise

            except Exception:
                # A temporary audio failure must not kill the broadcaster.
                logger.exception(
                    "websocket: music broadcast iteration failed"
                )

    except asyncio.CancelledError:
        logger.info(
            "websocket: music broadcast loop stopped"
        )
        raise


# ---------------------------------------------------------------------------
# State broadcast
# ---------------------------------------------------------------------------

async def _state_broadcast_loop():
    """Push full UI state only when the serialized state changes."""

    global _last_state_payload

    logger.info("websocket: state broadcast loop started")

    try:
        while True:
            await asyncio.sleep(STATE_BROADCAST_INTERVAL)

            try:
                state = await asyncio.to_thread(
                    get_full_state
                )

                payload = json.dumps(
                    state,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )

                if payload == _last_state_payload:
                    continue

                # Update only after a successful broadcast.
                await broadcast_state(state)

                _last_state_payload = payload

            except asyncio.CancelledError:
                raise

            except Exception:
                # Never silently kill this loop.
                logger.exception(
                    "websocket: state broadcast iteration failed"
                )

    except asyncio.CancelledError:
        logger.info(
            "websocket: state broadcast loop stopped"
        )
        raise


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------

def start_music_broadcast():
    """Start the music broadcaster once."""

    global _music_broadcast_task

    if (
        _music_broadcast_task is None
        or _music_broadcast_task.done()
    ):
        _music_broadcast_task = asyncio.create_task(
            _music_broadcast_loop(),
            name="kyroshix-music-broadcast",
        )


def start_state_broadcast():
    """Start the state broadcaster once."""

    global _state_broadcast_task

    if (
        _state_broadcast_task is None
        or _state_broadcast_task.done()
    ):
        _state_broadcast_task = asyncio.create_task(
            _state_broadcast_loop(),
            name="kyroshix-state-broadcast",
        )


async def stop_broadcast_tasks():
    """Cleanly stop all KYROSHIX BOT WebSocket broadcast tasks."""

    global _music_broadcast_task
    global _state_broadcast_task
    global _last_music_state
    global _last_state_payload

    tasks = [
        task
        for task in (
            _music_broadcast_task,
            _state_broadcast_task,
        )
        if task is not None and not task.done()
    ]

    for task in tasks:
        task.cancel()

    if tasks:
        await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

    _music_broadcast_task = None
    _state_broadcast_task = None

    _last_music_state = None
    _last_state_payload = None

    logger.info(
        "websocket: broadcast tasks stopped"
    )