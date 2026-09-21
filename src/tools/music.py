import logging

from google.genai import types
from src.tools._base import BaseTool, register_tool

logger = logging.getLogger(__name__)


@register_tool
class MusicTools(BaseTool):
    tool_key = "music"

    def declarations(self, config=None):
        return [
            types.FunctionDeclaration(
                name="listMusic",
                description=(
                    "List locally available music files. "
                    "Use this when the user specifically asks what songs "
                    "are already stored locally."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {},
                },
            ),

            types.FunctionDeclaration(
                name="playMusic",
                description=(
                    "Play music requested by the user. This is the MAIN music tool. "
                    "The request may be a song name, artist plus song name, "
                    "local filename, or an http/https media URL. "
                    "If the request is a song name, the music system first searches "
                    "the local library. If it is not available locally, it searches "
                    "online and plays the first matching result. "
                    "If the request is a URL, it resolves the URL directly. "
                    "Do NOT require the user to provide an exact local filename. "
                    "Do NOT call listMusic first unless the user explicitly wants "
                    "to browse the local library. "
                    "This tool can be used for requests received through VRChat "
                    "voice chat, text chat, the web panel, or another connected interface."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "request": {
                            "type": "STRING",
                            "description": (
                                "Song name, artist and song name, local filename, "
                                "or complete http/https media URL requested by the user."
                            ),
                        },
                        "volume": {
                            "type": "INTEGER",
                            "description": (
                                "Playback volume. Normally 0-100. "
                                "Values up to 300 are supported."
                            ),
                        },
                    },
                    "required": ["request"],
                },
            ),

            types.FunctionDeclaration(
                name="playMusicUrl",
                description=(
                    "Play music directly from a supported http/https media URL. "
                    "Normally playMusic can handle URLs automatically, but this "
                    "tool remains available for explicit URL playback. "
                    "Public media supported by yt-dlp can be resolved and cached. "
                    "DRM-protected or inaccessible subscription content is not bypassed."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "url": {
                            "type": "STRING",
                            "description": "Complete http/https media URL.",
                        },
                        "volume": {
                            "type": "INTEGER",
                            "description": (
                                "Playback volume. Normally 0-100; "
                                "values up to 300 are supported."
                            ),
                        },
                    },
                    "required": ["url"],
                },
            ),

            types.FunctionDeclaration(
                name="stopMusic",
                description=(
                    "Stop the currently playing music. "
                    "Call when the user asks to stop the song or turn the music off."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {},
                },
            ),

            types.FunctionDeclaration(
                name="pauseMusic",
                description=(
                    "Pause the currently playing music so it can be resumed later."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {},
                },
            ),

            types.FunctionDeclaration(
                name="resumeMusic",
                description=(
                    "Resume previously paused music playback."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {},
                },
            ),

            types.FunctionDeclaration(
                name="setMusicVolume",
                description=(
                    "Change the current music playback volume. "
                    "Use when the user asks to make music louder, quieter, "
                    "or requests a specific volume."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "volume": {
                            "type": "INTEGER",
                            "description": "Music volume from 0 to 300.",
                        },
                    },
                    "required": ["volume"],
                },
            ),
        ]

    async def handle(self, name, args):
        args = args or {}

        if name == "listMusic":
            files = self.audio.list_music()
            return {
                "result": "ok",
                "files": files,
                "count": len(files),
            }

        elif name == "playMusic":
            # Backward compatibility: old callers may still send "filename".
            request = str(
                args.get("request")
                or args.get("filename")
                or ""
            ).strip()

            volume = args.get("volume", 50)

            if not request:
                return {
                    "result": "error",
                    "message": "No song, filename, or URL was provided",
                }

            logger.info(
                "playMusic requested: %s (volume=%s)",
                request,
                volume,
            )

            return self.audio.play_music_request(
                request,
                volume,
            )

        elif name == "playMusicUrl":
            url = str(args.get("url") or "").strip()
            volume = args.get("volume", 50)

            if not url:
                return {
                    "result": "error",
                    "message": "No URL was provided",
                }

            if not url.lower().startswith(("http://", "https://")):
                return {
                    "result": "error",
                    "message": "Invalid URL",
                }

            logger.info(
                "playMusicUrl requested: %s (volume=%s)",
                url,
                volume,
            )

            return self.audio.play_music_url(
                url,
                volume,
            )

        elif name == "stopMusic":
            self.audio.stop_music()
            logger.info("Music stopped by tool")

            return {
                "result": "ok",
                "message": "stopped",
            }

        elif name == "pauseMusic":
            ok = self.audio.pause_music()

            return {
                "result": "ok" if ok else "error",
                "message": "paused" if ok else "nothing is currently playing",
            }

        elif name == "resumeMusic":
            ok = self.audio.resume_music()

            return {
                "result": "ok" if ok else "error",
                "message": "resumed" if ok else "nothing is currently paused",
            }

        elif name == "setMusicVolume":
            try:
                volume = int(args.get("volume", 50))
            except (TypeError, ValueError):
                return {
                    "result": "error",
                    "message": "Invalid volume",
                }

            volume = max(0, min(300, volume))
            ok = self.audio.set_music_volume(volume)

            return {
                "result": "ok" if ok else "error",
                "message": (
                    f"volume set to {volume}%"
                    if ok
                    else "nothing is currently playing"
                ),
                "volume": volume,
            }

        return {
            "result": "error",
            "message": f"Unknown music tool: {name}",
        }
