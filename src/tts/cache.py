"""Audio cache for TTS providers.

Avoids re-synthesizing identical sentences by caching PCM output keyed by
a hash of (provider, voice, model, text, sample_rate). Dramatically reduces
latency and bandwidth when the AI repeats common phrases.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)


class TTSCache:
    """LRU cache of synthesized PCM audio chunks.

    Thread-safe. Stores raw int16 PCM bytes keyed by SHA256 of the
    synthesis parameters. Evicts the oldest entries when the max size is
    reached so long-running 24/7 sessions don't grow RAM forever.
    """

    def __init__(self, max_entries: int = 500, max_age_seconds: int = 3600):
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.RLock()
        self._max_entries = max_entries
        self._max_age_seconds = max_age_seconds
        self._timestamps: dict[str, float] = {}
        self._hits = 0
        self._misses = 0

    def _key(self, provider: str, voice: str, model: str, text: str, sample_rate: int) -> str:
        raw = f"{provider}|{voice}|{model}|{sample_rate}|{text.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, provider: str, voice: str, model: str, text: str, sample_rate: int) -> Optional[bytes]:
        key = self._key(provider, voice, model, text, sample_rate)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._timestamps[key] = time.monotonic()
                self._hits += 1
                return self._cache[key]
            self._misses += 1
            return None

    def put(self, provider: str, voice: str, model: str, text: str, sample_rate: int, pcm: bytes):
        key = self._key(provider, voice, model, text, sample_rate)
        with self._lock:
            # Evict if at capacity
            while len(self._cache) >= self._max_entries:
                self._evict_oldest()
            self._cache[key] = pcm
            self._timestamps[key] = time.monotonic()

    def _evict_oldest(self):
        if not self._cache:
            return
        key, _ = self._cache.popitem(last=False)
        self._timestamps.pop(key, None)

    def evict_expired(self):
        """Remove entries older than max_age_seconds."""
        now = time.monotonic()
        with self._lock:
            expired = [k for k, t in self._timestamps.items() if now - t > self._max_age_seconds]
            for k in expired:
                self._cache.pop(k, None)
                self._timestamps.pop(k, None)

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = round((self._hits / total) * 100, 1) if total else 0.0
            return {
                "entries": len(self._cache),
                "max_entries": self._max_entries,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
            }


# Global cache shared by all TTS providers
tts_cache = TTSCache()
