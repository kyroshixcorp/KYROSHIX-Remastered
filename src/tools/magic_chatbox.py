"""MagicChatbox OSC tools - custom floating text above avatar head."""

from __future__ import annotations

import logging
from typing import Any, Dict

from google.genai import types
from src.tools._base import BaseTool, register_tool

logger = logging.getLogger(__name__)


@register_tool
class MagicChatboxTools(BaseTool):
    """Controls the vrcosc-magicchatbox floating text via OSC."""

    tool_key = "magic_chatbox"

    def declarations(self, config=None):
        mc_enabled = False
        if config is not None:
            mc = getattr(config, "magic_chatbox", None) or {}
            mc_enabled = bool(mc.get("enabled", False))
        if not mc_enabled:
            return []
        return [
            types.FunctionDeclaration(
                name="setMagicChatboxText",
                description="Set the custom floating text displayed above the bot's avatar head (requires vrcosc-magicchatbox avatar prop).\n**Invocation Condition:** Call when asked to change the head text, show a status, label, or message above the head.",
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "text": {"type": "STRING", "description": "Text to display above the head (empty to clear)."},
                        "color": {"type": "STRING", "description": "Hex color, e.g. '#00FF00' (green), '#FF0000' (red), '#FFFFFF' (white)."},
                        "size": {"type": "NUMBER", "description": "Text size multiplier, e.g. 1.0 normal, 1.5 larger, 0.7 smaller."},
                        "show": {"type": "BOOLEAN", "description": "true = show the text, false = hide it."},
                    },
                    "required": ["text"],
                },
            ),
            types.FunctionDeclaration(
                name="clearMagicChatbox",
                description="Hide the floating text above the bot's head.\n**Invocation Condition:** Call when asked to remove or clear the head text.",
                parameters={"type": "OBJECT", "properties": {}},
            ),
        ]

    async def handle(self, name, args) -> Dict[str, Any]:
        mc = getattr(self.handler, "magic_chatbox", None)
        if mc is None:
            return {"result": "error", "message": "MagicChatbox is not available"}

        if name == "setMagicChatboxText":
            text = args.get("text", "")
            color = args.get("color")
            size = args.get("size")
            show = args.get("show", True)

            if color is not None:
                mc.set_color(str(color))
            if size is not None:
                try:
                    mc.set_size(float(size))
                except (TypeError, ValueError):
                    pass
            mc.set_text(str(text))
            mc.set_enabled(bool(show))
            mc.tick()
            return {"result": "ok", "text": mc.text, "color": mc.color, "size": mc.size, "enabled": mc.enabled}

        elif name == "clearMagicChatbox":
            mc.clear()
            mc.tick()
            return {"result": "ok", "message": "MagicChatbox cleared"}

        return None
