from __future__ import annotations

import asyncio
import os
import psutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:
    from prometheus_client import Gauge, Counter, Histogram, start_http_server
except Exception:  # pragma: no cover - prometheus optional
    start_http_server = None  # type: ignore
    Gauge = Counter = Histogram = None  # type: ignore


@dataclass
class HealthSnapshot:
    ts: float
    cpu_percent: float
    mem_percent: float
    mem_used_mb: float
    gpu_percent: Optional[float] = None
    gpu_mem_mb: Optional[float] = None
    uptime_s: float = 0.0
    session_errors: int = 0
    active_connections: int = 0


class Monitor:
    """Advanced monitoring: bot instance + server actions + health metrics."""

    def __init__(self, config: dict | None = None, port: int = 9090) -> None:
        self.config = config or {}
        self.port = int(self.config.get("monitor_port", port))
        self._started = False
        self._start_ts = time.time()
        self._history: List[HealthSnapshot] = []
        self._max_history = int(self.config.get("monitor_max_history", 1440))
        self._server_actions: List[dict] = []
        self._max_actions = int(self.config.get("monitor_max_actions", 500))
        self._lock = asyncio.Lock()
        self._session_errors = 0
        self._active_connections = 0
        # Prometheus gauges (optional)
        self.g_cpu: Any = None
        self.g_mem: Any = None
        self.g_gpu: Any = None
        self.c_errors: Any = None
        self.c_actions: Any = None

    def start(self) -> None:
        if self._started or start_http_server is None:
            return
        try:
            start_http_server(self.port)
            self.g_cpu = Gauge("kyroshix_cpu_percent", "CPU usage %")
            self.g_mem = Gauge("kyroshix_mem_percent", "Memory usage %")
            self.g_gpu = Gauge("kyroshix_gpu_percent", "GPU usage %")
            self.c_errors = Counter("kyroshix_session_errors_total", "Session errors")
            self.c_actions = Counter("kyroshix_server_actions_total", "Server actions")
            self._started = True
        except Exception:
            self._started = False

    def record_server_action(self, action: str, detail: dict | None = None, source: str = "system") -> None:
        entry = {"ts": time.time(), "action": action, "source": source, "detail": detail or {}}
        self._server_actions.append(entry)
        if len(self._server_actions) > self._max_actions:
            self._server_actions = self._server_actions[-self._max_actions:]
        if self.c_actions:
            self.c_actions.inc()

    def record_error(self) -> None:
        self._session_errors += 1
        if self.c_errors:
            self.c_errors.inc()

    def set_active_connections(self, n: int) -> None:
        self._active_connections = n

    def snapshot(self) -> HealthSnapshot:
        proc = psutil.Process(os.getpid())
        try:
            cpu = proc.cpu_percent(interval=0.0)
        except Exception:
            cpu = 0.0
        mem = proc.memory_info()
        mem_p = proc.memory_percent()
        snap = HealthSnapshot(
            ts=time.time(),
            cpu_percent=cpu,
            mem_percent=mem_p,
            mem_used_mb=mem.rss / (1024 * 1024),
            uptime_s=time.time() - self._start_ts,
            session_errors=self._session_errors,
            active_connections=self._active_connections,
        )
        self._gpu_fill(snap)
        self._history.append(snap)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        if self.g_cpu:
            self.g_cpu.set(snap.cpu_percent)
            self.g_mem.set(snap.mem_percent)
            if snap.gpu_percent is not None:
                self.g_gpu.set(snap.gpu_percent)
        return snap

    def _gpu_fill(self, snap: HealthSnapshot) -> None:
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                snap.gpu_percent = None  # torch doesn't expose % directly
                props = torch.cuda.get_device_properties(0)
                snap.gpu_mem_mb = props.total_memory / (1024 * 1024)
        except Exception:
            pass

    def recent_actions(self, limit: int = 50) -> List[dict]:
        return list(self._server_actions[-limit:])

    def recent_health(self, limit: int = 60) -> List[HealthSnapshot]:
        return list(self._history[-limit:])

    def status(self) -> dict:
        snap = self.snapshot()
        return {
            "uptime_s": snap.uptime_s,
            "cpu_percent": snap.cpu_percent,
            "mem_percent": snap.mem_percent,
            "mem_used_mb": round(snap.mem_used_mb, 1),
            "gpu_mem_mb": round(snap.gpu_mem_mb, 1) if snap.gpu_mem_mb else None,
            "session_errors": snap.session_errors,
            "active_connections": snap.active_connections,
            "recent_actions": self.recent_actions(10),
        }

monitor: Optional[Monitor] = None


def get_monitor() -> Optional[Monitor]:
    return monitor


def init_monitor(config: dict | None = None, port: int = 9090) -> Monitor:
    global monitor
    monitor = Monitor(config, port)
    monitor.start()
    return monitor
