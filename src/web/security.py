from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Deque, Dict, Optional


@dataclass
class _ClientState:
    hits: Deque[float] = field(default_factory=deque)
    failures: int = 0
    blocked_until: float = 0.0


class Security:
    """Rate limiting and login failure tracking for HTTP endpoints."""

    def __init__(self, max_hits: int = 30, window_seconds: float = 60.0,
                 max_failures: int = 5, block_seconds: float = 300.0) -> None:
        self.max_hits = max_hits
        self.window_seconds = window_seconds
        self.max_failures = max_failures
        self.block_seconds = block_seconds
        self._states: Dict[str, _ClientState] = defaultdict(_ClientState)
        self._lock = Lock()

    def _key_state(self, key: str) -> _ClientState:
        with self._lock:
            return self._states[key]

    def check_http(self, key: str, endpoint: str = "http") -> tuple[bool, str]:
        """Return (allowed, reason). Rate-limits per client key."""
        now = time.time()
        with self._lock:
            st = self._states[key]
            if st.blocked_until > now:
                return False, f"blocked until {st.blocked_until:.0f}"
            # Trim old hits
            cutoff = now - self.window_seconds
            while st.hits and st.hits[0] < cutoff:
                st.hits.popleft()
            if len(st.hits) >= self.max_hits:
                st.blocked_until = now + self.block_seconds
                return False, "rate limited"
            st.hits.append(now)
            return True, "ok"

    def record_login_failure(self, key: str) -> None:
        with self._lock:
            st = self._states[key]
            st.failures += 1
            if st.failures >= self.max_failures:
                st.blocked_until = time.time() + self.block_seconds

    def record_login_success(self, key: str) -> None:
        with self._lock:
            st = self._states[key]
            st.failures = 0
            st.blocked_until = 0.0


security = Security()
