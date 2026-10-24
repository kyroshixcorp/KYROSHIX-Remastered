"""Security: rate limiting, brute-force protection, DDoS mitigation, OSC flood guard.

Provides:
  - Token-bucket rate limiter per IP for HTTP endpoints
  - Brute-force lockout for /auth/login (N failures -> temporary ban)
  - OSC message flood guard (max messages per second per source)
  - Suspicious IP tracking and optional auto-block
  - Request size limits
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class _TokenBucket:
    """Simple token bucket: refills at ``rate`` tokens per second, capacity ``burst``."""
    rate: float
    burst: int
    _tokens: float = field(init=False)
    _last: float = field(init=False, default_factory=time.monotonic)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self):
        self._tokens = float(self.burst)

    def allow(self, cost: float = 1.0) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False


@dataclass
class _IpRecord:
    """Per-IP state for brute-force / DDoS tracking."""
    failures: int = 0
    first_failure: float = 0.0
    blocked_until: float = 0.0
    request_times: deque = field(default_factory=deque)
    osc_times: deque = field(default_factory=deque)


class SecurityManager:
    """Central security controls for HTTP + OSC.

    Thread-safe. Instantiate once and share across the app.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._ips: dict[str, _IpRecord] = {}

        # Tunables (can be overridden from config.yml)
        self.http_rate = 10.0          # tokens/sec per IP for general HTTP
        self.http_burst = 20           # burst capacity
        self.login_rate = 1.0          # tokens/sec for /auth/login
        self.login_burst = 3           # burst for login
        self.max_login_failures = 5    # failures before lockout
        self.lockout_seconds = 300     # 5 minutes lockout
        self.failure_window_seconds = 300
        self.http_requests_per_sec = 15
        self.osc_messages_per_sec = 20
        self.osc_burst = 30
        self.max_request_bytes = 1_048_576  # 1 MB max body
        self.blocked_ips: set[str] = set()
        self.trusted_ips: set[str] = {"127.0.0.1", "::1"}
        self._configured = False

    def load_config(self, config_obj) -> None:
        """Read security settings from the application Config object."""
        sec = getattr(config_obj, "security", None) or {}
        self.http_requests_per_sec = int(sec.get("http_requests_per_sec", self.http_requests_per_sec))
        self.login_rate = float(sec.get("login_rate", self.login_rate))
        self.login_burst = int(sec.get("login_burst", self.login_burst))
        self.max_login_failures = int(sec.get("max_login_failures", self.max_login_failures))
        self.lockout_seconds = int(sec.get("lockout_seconds", self.lockout_seconds))
        self.failure_window_seconds = int(sec.get("failure_window_seconds", self.failure_window_seconds))
        self.osc_messages_per_sec = int(sec.get("osc_messages_per_sec", self.osc_messages_per_sec))
        self.osc_burst = int(sec.get("osc_burst", self.osc_burst))
        self.max_request_bytes = int(sec.get("max_request_bytes", self.max_request_bytes))

        trusted = sec.get("trusted_ips") or []
        if trusted:
            self.trusted_ips = set(str(ip) for ip in trusted)

        self._configured = True
        logger.info(
            "Security: loaded config (http=%d/s, login=%.1f/s burst=%d, "
            "max_failures=%d, lockout=%ds, osc=%d/s)",
            self.http_requests_per_sec, self.login_rate, self.login_burst,
            self.max_login_failures, self.lockout_seconds, self.osc_messages_per_sec,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _get_record(self, ip: str) -> _IpRecord:
        with self._lock:
            rec = self._ips.get(ip)
            if rec is None:
                rec = _IpRecord()
                self._ips[ip] = rec
            return rec

    @staticmethod
    def _is_private(ip: str) -> bool:
        try:
            return ipaddress.ip_address(ip).is_private
        except ValueError:
            return False

    def _prune(self, rec: _IpRecord, now: float, window: float):
        while rec.request_times and rec.request_times[0] < now - window:
            rec.request_times.popleft()
        while rec.osc_times and rec.osc_times[0] < now - window:
            rec.osc_times.popleft()

    # ------------------------------------------------------------------
    # HTTP rate limiting
    # ------------------------------------------------------------------
    def check_http(self, ip: str, endpoint: str = "") -> tuple[bool, str]:
        """Return (allowed, reason). Denies if rate-limited or blocked."""
        now = time.monotonic()
        rec = self._get_record(ip)

        with self._lock:
            if ip in self.blocked_ips and now < rec.blocked_until:
                return False, "ip_blocked"
            if ip in self.blocked_ips and now >= rec.blocked_until:
                # lockout expired
                self.blocked_ips.discard(ip)
                rec.blocked_until = 0.0
                rec.failures = 0

            self._prune(rec, now, self.failure_window_seconds)

            # General request rate
            rec.request_times.append(now)
            recent = len(rec.request_times)
            if recent > self.http_requests_per_sec:
                return False, "http_rate_limited"

            # Login-specific stricter limit
            if endpoint == "login":
                bucket = _TokenBucket(rate=self.login_rate, burst=self.login_burst)
                # reuse a stored bucket for persistence
                if not hasattr(rec, "_login_bucket"):
                    rec._login_bucket = bucket
                else:
                    bucket = rec._login_bucket
                if not bucket.allow():
                    return False, "login_rate_limited"

        return True, "ok"

    def record_login_failure(self, ip: str):
        """Record a failed login attempt. May trigger lockout."""
        now = time.monotonic()
        with self._lock:
            rec = self._get_record(ip)
            rec.failures += 1
            if rec.first_failure == 0.0:
                rec.first_failure = now
            # prune old failures outside window
            if now - rec.first_failure > self.failure_window_seconds:
                rec.failures = 1
                rec.first_failure = now
            if rec.failures >= self.max_login_failures:
                rec.blocked_until = now + self.lockout_seconds
                self.blocked_ips.add(ip)
                logger.warning(
                    "Security: IP %s locked out for %ds after %d login failures",
                    ip, self.lockout_seconds, rec.failures,
                )

    def record_login_success(self, ip: str):
        """Reset failure counter on successful login."""
        with self._lock:
            rec = self._get_record(ip)
            rec.failures = 0
            rec.first_failure = 0.0
            rec.blocked_until = 0.0
            self.blocked_ips.discard(ip)

    def check_request_size(self, content_length: Optional[int]) -> tuple[bool, str]:
        if content_length is None:
            return True, "ok"
        if content_length > self.max_request_bytes:
            return False, "request_too_large"
        return True, "ok"

    # ------------------------------------------------------------------
    # OSC flood guard
    # ------------------------------------------------------------------
    def check_osc(self, ip: str, address: str) -> tuple[bool, str]:
        """Return (allowed, reason) for incoming OSC messages."""
        now = time.monotonic()
        rec = self._get_record(ip)

        with self._lock:
            if ip in self.blocked_ips and now < rec.blocked_until:
                return False, "ip_blocked"
            if ip in self.blocked_ips and now >= rec.blocked_until:
                self.blocked_ips.discard(ip)
                rec.blocked_until = 0.0
                rec.failures = 0

            self._prune(rec, now, self.failure_window_seconds)
            rec.osc_times.append(now)
            recent = len(rec.osc_times)
            if recent > self.osc_messages_per_sec:
                # escalate: block the IP for flooding OSC
                rec.failures += 1
                if rec.failures >= self.max_login_failures:
                    rec.blocked_until = now + self.lockout_seconds
                    self.blocked_ips.add(ip)
                    logger.warning(
                        "Security: IP %s blocked for OSC flood (%d msgs/sec)",
                        ip, recent,
                    )
                return False, "osc_flood"

        return True, "ok"

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            blocked = {
                ip: rec.blocked_until for ip, rec in self._ips.items()
                if rec.blocked_until > time.monotonic()
            }
            return {
                "tracked_ips": len(self._ips),
                "blocked_ips": blocked,
                "trusted_ips": sorted(self.trusted_ips),
                "settings": {
                    "http_requests_per_sec": self.http_requests_per_sec,
                    "login_rate": self.login_rate,
                    "login_burst": self.login_burst,
                    "max_login_failures": self.max_login_failures,
                    "lockout_seconds": self.lockout_seconds,
                    "osc_messages_per_sec": self.osc_messages_per_sec,
                },
            }


# Global singleton
security = SecurityManager()
