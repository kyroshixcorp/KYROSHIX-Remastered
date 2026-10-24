"""System health endpoints: RAM, GPU (ROCm/AMD), uptime, session health, audio devices."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

from ..shared import shared_state

router = APIRouter()

_BOOT_TS = time.time()


def _read_meminfo() -> dict:
    """Read /proc/meminfo for RAM stats."""
    mem = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                if key in ("MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached", "SwapTotal", "SwapFree"):
                    try:
                        num = int(val.split()[0])
                        mem[key] = num * 1024  # convert kB to bytes
                    except (ValueError, IndexError):
                        pass
    except Exception:
        pass
    return mem


def _gpu_info() -> dict:
    """Detect AMD ROCm GPU info. Falls back gracefully when tooling is absent."""
    info = {"available": False, "vendor": None, "name": None, "vram_total_bytes": None, "driver": None}

    # Try rocm-smi first (AMD ROCm)
    smi = shutil.which("rocm-smi")
    if smi:
        try:
            out = subprocess.run(
                [smi, "--showproductname", "--showmeminfo vram", "--showdriverver"],
                capture_output=True, text=True, timeout=5,
            )
            text = out.stdout or ""
            if text:
                info["available"] = True
                info["driver"] = "rocm"
                for line in text.splitlines():
                    low = line.lower()
                    if "gfx" in low or "radeon" in low or "rx " in low or "amd" in low:
                        info["name"] = line.strip()
                        info["vendor"] = "AMD"
                    if "vram" in low and ("total" in low or "byte" in low):
                        parts = line.split()
                        for p in parts:
                            try:
                                info["vram_total_bytes"] = int(float(p.replace(",", "")) * 1024 * 1024)
                                break
                            except ValueError:
                                continue
        except Exception:
            pass

    # Fallback: torch.cuda (ROCm exposes AMD GPUs through torch.cuda)
    if not info["available"]:
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                info["available"] = True
                info["vendor"] = "AMD (ROCm via torch.cuda)"
                info["name"] = torch.cuda.get_device_name(0)
                try:
                    info["vram_total_bytes"] = torch.cuda.get_device_properties(0).total_memory
                except Exception:
                    pass
                info["driver"] = "torch.cuda"
        except Exception:
            pass

    return info


def _uptime_seconds() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8", errors="replace") as f:
            return float(f.read().split()[0])
    except Exception:
        return time.time() - _BOOT_TS


@router.get("/api/health")
async def health():
    """Return system + session health for the control panel."""
    mem = _read_meminfo()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    free = mem.get("MemFree", 0)
    used = total - available if total and available else 0
    ram_percent = round((used / total) * 100, 1) if total else None

    gpu = _gpu_info()

    session = shared_state.get("session")
    session_connected = bool(session and getattr(session, "_session", None) is not None)
    session_handle = None
    session_age_seconds = None
    handle_file = Path("data/session_handle.txt")
    if handle_file.exists():
        try:
            import json
            from datetime import datetime as _dt
            data = json.loads(handle_file.read_text())
            saved_at = _dt.fromisoformat(data.get("saved_at", ""))
            session_age_seconds = (_dt.now() - saved_at).total_seconds()
            session_handle = {
                "exists": True,
                "age_minutes": round(session_age_seconds / 60, 1),
            }
        except Exception:
            session_handle = {"exists": True, "error": "unparseable"}

    watchdog = {}
    if session:
        watchdog = {
            "max_session_seconds": getattr(session, "_max_session_seconds", None),
            "health_check_seconds": getattr(session, "_health_check_seconds", None),
            "dead_socket_seconds": getattr(session, "_dead_socket_seconds", None),
            "force_reconnect": getattr(session, "_force_reconnect", False),
            "connection_start_time": getattr(session, "_connection_start_time", None),
            "healthy": session.healthy() if hasattr(session, "healthy") else None,
        }

    # Gemini API key status
    keys_status = []
    config = shared_state.get("config")
    if config:
        keys = getattr(config, "_keys", [])
        key_index = getattr(config, "_key_index", 0)
        for i, key in enumerate(keys):
            masked = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"
            keys_status.append({
                "index": i,
                "masked": masked,
                "active": i == key_index,
            })

    return {
        "ok": True,
        "timestamp": datetime.now().isoformat(),
        "uptime_seconds": round(_uptime_seconds(), 1),
        "ram": {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "free_bytes": free,
            "percent": ram_percent,
        },
        "gpu": gpu,
        "session": {
            "connected": session_connected,
            "handle": session_handle,
            "watchdog": watchdog,
        },
        "gemini_keys": keys_status,
    }


@router.post("/api/session/reconnect")
async def session_reconnect():
    """Force the Gemini session to reconnect (fresh handle, context replay)."""
    session = shared_state.get("session")
    if not session:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Session not available")
    session.request_reconnect()
    return {"ok": True, "message": "Reconnect requested"}


@router.get("/api/audio-devices")
async def audio_devices():
    """List available audio input and output devices."""
    audio_mgr = shared_state.get("audio_mgr")
    if audio_mgr and hasattr(audio_mgr, "list_input_devices"):
        try:
            inputs = await __import__("asyncio").to_thread(audio_mgr.list_input_devices)
            outputs = await __import__("asyncio").to_thread(audio_mgr.list_output_devices)
        except Exception as e:
            inputs = []
            outputs = []
            _err = str(e)
    else:
        inputs = []
        outputs = []
        _err = "audio manager not available"
    return {
        "ok": True,
        "inputs": inputs,
        "outputs": outputs,
        "current_input": getattr(audio_mgr, "input_device", None) if audio_mgr else None,
        "current_output": getattr(audio_mgr, "output_device", None) if audio_mgr else None,
    }


@router.post("/api/audio-device")
async def set_audio_device(body: dict):
    """Set the active input or output audio device by index."""
    audio_mgr = shared_state.get("audio_mgr")
    if not audio_mgr:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Audio manager not available")
    device_type = body.get("type", "input")
    index = body.get("index")
    if index is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="index is required")
    try:
        index = int(index)
    except (TypeError, ValueError):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="index must be an integer")
    if device_type == "input":
        audio_mgr.input_device = index
    elif device_type == "output":
        audio_mgr.output_device = index
    else:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="type must be 'input' or 'output'")
    return {"ok": True, "type": device_type, "index": index}


@router.get("/api/tts-cache")
async def tts_cache_stats():
    """Return TTS audio cache statistics."""
    try:
        from src.tts.cache import tts_cache
        return {"ok": True, "cache": tts_cache.stats()}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/api/tts-cache/clear")
async def tts_cache_clear():
    """Clear the TTS audio cache."""
    try:
        from src.tts.cache import tts_cache
        tts_cache.clear()
        return {"ok": True, "message": "TTS cache cleared"}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/api/memory/maintenance")
async def memory_maintenance():
    """Run memory maintenance (decay, cleanup, summarization, vacuum)."""
    try:
        from src.memory.cognitive import cognitive_system
        res = cognitive_system.run_maintenance()
        return {"ok": True, "maintenance": res}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/api/memory/intelligence")
async def memory_intelligence():
    """Return memory intelligence stats (importance distribution, links, entities)."""
    try:
        from src.memory.system import memory_system
        if not memory_system.is_available():
            return {"ok": False, "message": "Memory unavailable"}
        stats = memory_system.stats()
        return {"ok": True, "stats": stats.get("stats", {})}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=str(e))


# ------------------------------------------------------------------
# Security status endpoints
# ------------------------------------------------------------------
@router.get("/api/security/status")
async def security_status():
    """Return security status: blocked IPs, rate limits, circuit breakers."""
    try:
        from src.security import security
        return {"ok": True, "security": security.status()}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/api/security/blocked-ips/{ip}/reset")
async def security_reset_ip(ip: str):
    """Manually unblock an IP address."""
    try:
        from src.security import security
        with security._lock:
            security.blocked_ips.discard(ip)
            rec = security._ips.get(ip)
            if rec:
                rec.blocked_until = 0.0
                rec.failures = 0
        return {"ok": True, "ip": ip, "unblocked": True}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=str(e))


# ------------------------------------------------------------------
# MagicChatbox endpoints
# ------------------------------------------------------------------
@router.get("/api/magic-chatbox")
async def magic_chatbox_status():
    """Return current MagicChatbox state."""
    from ..shared import shared_state
    mc = shared_state.get("magic_chatbox")
    if mc is None:
        return {"ok": False, "message": "MagicChatbox not available"}
    return {"ok": True, "magic_chatbox": mc.status()}


@router.post("/api/magic-chatbox")
async def magic_chatbox_set(body: dict):
    """Set MagicChatbox text/color/size/enabled."""
    from ..shared import shared_state
    mc = shared_state.get("magic_chatbox")
    if mc is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="MagicChatbox not available")

    text = body.get("text")
    color = body.get("color")
    size = body.get("size")
    enabled = body.get("enabled")

    if text is not None:
        mc.set_text(str(text))
    if color is not None:
        mc.set_color(str(color))
    if size is not None:
        try:
            mc.set_size(float(size))
        except (TypeError, ValueError):
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="size must be a number")
    if enabled is not None:
        mc.set_enabled(bool(enabled))

    mc.tick()
    return {"ok": True, "magic_chatbox": mc.status()}


@router.post("/api/magic-chatbox/clear")
async def magic_chatbox_clear():
    """Clear the MagicChatbox text."""
    from ..shared import shared_state
    mc = shared_state.get("magic_chatbox")
    if mc is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="MagicChatbox not available")
    mc.clear()
    mc.tick()
    return {"ok": True, "message": "MagicChatbox cleared"}



# ------------------------------------------------------------------
# Prometheus metrics endpoint
# ------------------------------------------------------------------
_METRICS: dict[str, float] = {}
_METRICS_LOCK = __import__("threading").Lock()


def _inc(metric: str, value: float = 1.0):
    with _METRICS_LOCK:
        _METRICS[metric] = _METRICS.get(metric, 0.0) + value


def _set(metric: str, value: float):
    with _METRICS_LOCK:
        _METRICS[metric] = value


def get_metrics() -> dict:
    with _METRICS_LOCK:
        return dict(_METRICS)


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint."""
    lines: list[str] = []
    with _METRICS_LOCK:
        for name, value in sorted(_METRICS.items()):
            safe_name = name.replace("-", "_").replace(".", "_")
            lines.append(f"# TYPE {safe_name} gauge")
            lines.append(f"{safe_name} {value}")
    return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}


# ------------------------------------------------------------------
# Circuit breaker registry (external API calls: Gemini, TTS, etc.)
# ------------------------------------------------------------------
_CIRCUITS: dict[str, dict] = {}
_CIRCUITS_LOCK = __import__("threading").Lock()


def circuit_record_success(service: str):
    with _CIRCUITS_LOCK:
        c = _CIRCUITS.setdefault(
            service,
            {"failures": 0, "state": "closed", "last_failure": 0.0, "opened_at": 0.0},
        )
        c["failures"] = 0
        c["state"] = "closed"


def circuit_record_failure(service: str) -> bool:
    """Record a failure. Returns True if the circuit is now OPEN (blocked)."""
    import time as _t
    with _CIRCUITS_LOCK:
        c = _CIRCUITS.setdefault(
            service,
            {"failures": 0, "state": "closed", "last_failure": 0.0, "opened_at": 0.0},
        )
        c["failures"] += 1
        c["last_failure"] = _t.time()
        if c["failures"] >= 5 and c["state"] != "open":
            c["state"] = "open"
            c["opened_at"] = _t.time()
            return True
        return c["state"] == "open"


def circuit_is_open(service: str) -> bool:
    with _CIRCUITS_LOCK:
        c = _CIRCUITS.get(service)
        if not c:
            return False
        if c["state"] == "open":
            # Half-open after 60s
            import time as _t
            if _t.time() - c["opened_at"] > 60:
                c["state"] = "half_open"
                return False
            return True
        return False


def circuit_get_state(service: str) -> str:
    with _CIRCUITS_LOCK:
        return _CIRCUITS.get(service, {}).get("state", "unknown")


@router.get("/api/circuits")
async def circuits_status():
    """Return circuit breaker states for external services."""
    with _CIRCUITS_LOCK:
        return {"ok": True, "circuits": dict(_CIRCUITS)}


@router.post("/api/circuits/{service}/reset")
async def circuit_reset(service: str):
    """Manually reset a circuit breaker (force closed)."""
    with _CIRCUITS_LOCK:
        if service in _CIRCUITS:
            _CIRCUITS[service]["failures"] = 0
            _CIRCUITS[service]["state"] = "closed"
            _CIRCUITS[service]["opened_at"] = 0.0
        else:
            _CIRCUITS[service] = {"failures": 0, "state": "closed", "last_failure": 0.0, "opened_at": 0.0}
    return {"ok": True, "service": service, "state": "closed"}


# Expose helpers for other modules to use
__all__ = ["router", "_inc", "_set", "get_metrics", "circuit_record_success", "circuit_record_failure", "circuit_is_open", "circuit_get_state"]

