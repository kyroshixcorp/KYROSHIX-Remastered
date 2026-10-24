"""MagicChatbox OSC integration (https://github.com/BoiHanny/vrcosc-magicchatbox).

Sends custom text to display above the bot's avatar head in VRChat via OSC.
The MagicChatbox avatar prop listens for OSC string parameters and renders
them as a floating chatbox above the avatar.

Default OSC addresses (configurable):
  - /avatar/parameters/MagicChatbox/Text   (string)  - the text to display
  - /avatar/parameters/MagicChatbox/Enable (float)   - 1 = show, 0 = hide
  - /avatar/parameters/MagicChatbox/Color  (string)  - hex color, e.g. "#FFFFFF"
  - /avatar/parameters/MagicChatbox/Size   (float)   - text size multiplier
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class MagicChatbox:
    """Controls the vrcosc-magicchatbox floating text above the avatar head."""

    def __init__(self, osc, config=None):
        self.osc = osc
        self.enabled = False
        self.text = ""
        self.color = "#FFFFFF"
        self.size = 1.0
        self._last_text = ""
        self._last_color = ""
        self._last_size = 1.0
        self._last_enable = -1
        self._last_send = 0.0
        self._min_interval = 0.5  # seconds between OSC sends to avoid spam

        if config is not None:
            self._load_config(config)

    def _load_config(self, config):
        mc = getattr(config, "magic_chatbox", None) or {}
        self.enabled = bool(mc.get("enabled", False))
        self.text = str(mc.get("default_text", ""))
        self.color = str(mc.get("color", "#FFFFFF"))
        self.size = float(mc.get("size", 1.0))
        self._min_interval = float(mc.get("min_send_interval", 0.5))

        # OSC address overrides
        self.addr_text = str(mc.get("osc_address_text", "/avatar/parameters/MagicChatbox/Text"))
        self.addr_enable = str(mc.get("osc_address_enable", "/avatar/parameters/MagicChatbox/Enable"))
        self.addr_color = str(mc.get("osc_address_color", "/avatar/parameters/MagicChatbox/Color"))
        self.addr_size = str(mc.get("osc_address_size", "/avatar/parameters/MagicChatbox/Size"))

    def set_text(self, text: str):
        """Set the floating text (sent on next tick if changed)."""
        text = text or ""
        if text != self.text:
            self.text = text
            self._schedule_send()

    def set_color(self, color: str):
        self.color = color or "#FFFFFF"
        self._schedule_send()

    def set_size(self, size: float):
        self.size = float(size or 1.0)
        self._schedule_send()

    def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)
        self._schedule_send(force=True)

    def clear(self):
        self.set_text("")
        self.set_enabled(False)

    def _schedule_send(self, force: bool = False):
        """Send OSC update if enough time has passed or forced."""
        now = time.monotonic()
        if force or (now - self._last_send) >= self._min_interval:
            self._send()
            self._last_send = now
        else:
            # Changes will be flushed on next tick via tick()
            pass

    def tick(self):
        """Call periodically (e.g. each frame/tick) to flush pending changes."""
        now = time.monotonic()
        if (now - self._last_send) >= self._min_interval:
            self._send()

    def _send(self):
        if not self.enabled or not self.osc:
            # Still send disable if we were previously enabled
            if self._last_enable != 0:
                try:
                    self.osc._safe_send(self.addr_enable, 0.0)
                    self._last_enable = 0
                except Exception:
                    pass
            return

        try:
            # Only send fields that changed to reduce OSC traffic
            if self.text != self._last_text:
                self.osc._safe_send(self.addr_text, self.text)
                self._last_text = self.text
            if self.color != self._last_color:
                self.osc._safe_send(self.addr_color, self.color)
                self._last_color = self.color
            if self.size != self._last_size:
                self.osc._safe_send(self.addr_size, self.size)
                self._last_size = self.size
            if self._last_enable != 1:
                self.osc._safe_send(self.addr_enable, 1.0)
                self._last_enable = 1
        except Exception as e:
            logger.debug(f"MagicChatbox OSC send failed: {e}")

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "text": self.text,
            "color": self.color,
            "size": self.size,
        }
