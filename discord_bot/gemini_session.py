import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from google import genai
from google.genai import types
from google.genai.errors import APIError

from src.model_caps import interaction_is_idle

logger = logging.getLogger(__name__)

SESSION_HANDLE_FILE = Path("discord_bot/data/session_handle.txt")
SESSION_EXPIRY_HOURS = 2
IDLE_GRACE_SECONDS = 2.0  # how long to wait for IDLE before delivering a reply anyway


class GeminiTextSession:
    """Gemini Live session for Discord bot, AUDIO modality with output transcription."""

    def __init__(self, config, tool_handler, personality_mgr=None):
        self.config = config
        self.tool_handler = tool_handler
        self.personality = personality_mgr
        self.discord_username = None
        self._session = None
        self._session_handle = None
        self._session_handle_created = None
        self._handle_fail_count = 0
        self._rate_limit_backoff = 0
        self._connected = asyncio.Event()
        self._response_queue = asyncio.Queue()
        self._pending_responses = {}  # request_id -> asyncio.Future
        self._request_counter = 0
        self._receive_task = None
        self._reconnect_lock = asyncio.Lock()
        self._closing = False
        self._session_resumed = False
        self._bad_response_streak = 0
        self._compat_notes_logged = set()
        self._pending_transcript = ""
        self._awaiting_idle = False
        self._idle_flush_task = None
        self._load_session_handle()

    def _build_config(self):
        transcription_config = types.AudioTranscriptionConfig()

        config_kwargs = dict(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part.from_text(
                    text=self.config.build_system_instruction(self.personality, discord_username=self.discord_username)
                )]
            ),
            tools=self.tool_handler.get_declarations(),
            input_audio_transcription=transcription_config,
            output_audio_transcription=transcription_config,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self.config.voice
                    )
                )
            ),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True
                )
            ),
            session_resumption=types.SessionResumptionConfig(
                handle=self._session_handle
            ) if self._session_handle else types.SessionResumptionConfig(),
        )

        if self.config.temperature is not None:
            config_kwargs["temperature"] = self.config.temperature
        if self.config.top_p is not None:
            config_kwargs["top_p"] = self.config.top_p
        if self.config.top_k is not None:
            config_kwargs["top_k"] = self.config.top_k
        if self.config.max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = self.config.max_output_tokens

        # Context window compression
        if self.config.compression_enabled:
            sw_kwargs = {}
            if self.config.compression_target_tokens is not None:
                sw_kwargs["target_tokens"] = self.config.compression_target_tokens
            cw_kwargs = {"sliding_window": types.SlidingWindow(**sw_kwargs)}
            if self.config.compression_trigger_tokens is not None:
                cw_kwargs["trigger_tokens"] = self.config.compression_trigger_tokens
            config_kwargs["context_window_compression"] = types.ContextWindowCompressionConfig(**cw_kwargs)

        # map whatever the user set onto what this model accepts, log any corrections
        thinking_kwargs, thinking_notes = self.config.resolve_thinking_config()
        self._log_compat_notes(thinking_notes)
        if thinking_kwargs:
            config_kwargs["thinking_config"] = types.ThinkingConfig(**thinking_kwargs)

        if self.config.model_capabilities.history_config:
            config_kwargs["history_config"] = types.HistoryConfig(
                initial_history_in_client_content=True
            )

        return types.LiveConnectConfig(**config_kwargs)

    def _log_compat_notes(self, notes):
        for note in notes:
            key = str(note)
            if key in self._compat_notes_logged:
                continue
            self._compat_notes_logged.add(key)
            logger.warning(f"model compat: {note}")

    async def _deliver_transcript(self):
        # hand the accumulated reply to whoever is waiting on it
        text = self._pending_transcript.strip()
        self._pending_transcript = ""
        await self._response_queue.put(text if text else "")

    async def _flush_after_grace(self):
        # backstop for models that never report IDLE, so a caller never hangs on us
        try:
            await asyncio.sleep(IDLE_GRACE_SECONDS)
            if self._awaiting_idle:
                logger.warning(
                    f"{self.config.model} never sent interaction_status=IDLE after its turn, "
                    f"delivering the reply anyway"
                )
                self._awaiting_idle = False
                await self._deliver_transcript()
        except asyncio.CancelledError:
            pass

    def _load_session_handle(self):
        if not SESSION_HANDLE_FILE.exists():
            return
        try:
            data = json.loads(SESSION_HANDLE_FILE.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data["created"])
            if datetime.now() - created < timedelta(hours=SESSION_EXPIRY_HOURS):
                self._session_handle = data["handle"]
                self._session_handle_created = created
                logger.info(f"Loaded Discord session handle (created {created.strftime('%H:%M:%S')})")
            else:
                self._clear_session_handle()
        except Exception as e:
            logger.warning(f"Failed to load Discord session handle: {e}")
            self._clear_session_handle()

    def _save_session_handle(self, handle):
        self._session_handle = handle
        self._session_handle_created = datetime.now()
        self._handle_fail_count = 0
        SESSION_HANDLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {"handle": handle, "created": self._session_handle_created.isoformat()}
        SESSION_HANDLE_FILE.write_text(json.dumps(data), encoding="utf-8")

    def _clear_session_handle(self):
        self._session_handle = None
        self._session_handle_created = None
        self._handle_fail_count = 0
        if SESSION_HANDLE_FILE.exists():
            SESSION_HANDLE_FILE.unlink()

    def report_bad_response(self):
        """Report a bad/empty response. After 2 consecutive bad responses,
        clears the session handle and triggers a reconnect."""
        self._bad_response_streak += 1
        if self._bad_response_streak >= 2 and self._session_handle:
            logger.warning(f"Discord bot: {self._bad_response_streak} consecutive bad responses, clearing session handle and reconnecting")
            self._clear_session_handle()
            # Cancel receive task to trigger reconnect in run_forever
            if self._receive_task and not self._receive_task.done():
                self._receive_task.cancel()
            return True  # Signal that reconnect was triggered
        return False

    def report_good_response(self):
        """Report a successful response, resetting the bad response counter."""
        self._bad_response_streak = 0

    def _is_handle_expired(self):
        if not self._session_handle or not self._session_handle_created:
            return False
        return datetime.now() - self._session_handle_created >= timedelta(hours=SESSION_EXPIRY_HOURS)

    async def connect(self):
        """Connect to Gemini Live using async context manager.

        Must be called from run_forever() which manages the session lifecycle.
        Yields the connected session.
        """
        if self._is_handle_expired():
            self._clear_session_handle()

        self._client = genai.Client(
            api_key=self.config.api_key,
            http_options={"api_version": "v1alpha"},
        )
        live_config = self._build_config()

        caps = self.config.model_capabilities
        logger.info(f"Connecting Discord bot to Gemini Live ({self.config.model}) [family {caps.family}]...")
        return self._client.aio.live.connect(
            model=self.config.model,
            config=live_config,
        )

    async def disconnect(self):
        """Disconnect from Gemini Live."""
        self._closing = True
        self._connected.clear()
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._idle_flush_task and not self._idle_flush_task.done():
            self._idle_flush_task.cancel()
        self._idle_flush_task = None
        self._awaiting_idle = False
        self._pending_transcript = ""
        self._session = None

    async def send_message(self, text, images=None):
        """Send a message and wait for the complete text response.

        Args:
            text: The text message to send
            images: Optional list of (bytes, mime_type) tuples (only first used)

        Returns:
            The complete text response from the model
        """
        await self._connected.wait()
        if not self._session:
            raise RuntimeError("Not connected to Gemini Live")

        # Drain stale responses from previous timeouts/cancellations
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        if self.config.uses_realtime_text:
            # new gen models: inject images via send_client_content, send text via realtime input
            if images:
                img_data, mime_type = images[0]
                await self._session.send_client_content(
                    turns=types.Content(role="user", parts=[
                        types.Part.from_bytes(data=img_data, mime_type=mime_type),
                    ]),
                    turn_complete=False,
                )
            await self._session.send_realtime_input(text=text)
        else:
            # 2.5 models: use send_client_content for everything
            parts = []
            if images:
                img_data, mime_type = images[0]
                parts.append(types.Part.from_bytes(data=img_data, mime_type=mime_type))
            parts.append(types.Part.from_text(text=text))
            await self._session.send_client_content(
                turns=types.Content(role="user", parts=parts),
                turn_complete=True,
            )

        response_text = await self._response_queue.get()
        return response_text

    async def send_with_context(self, context_turns, text, images=None):
        """Send conversation context as structured turns, then the new message.

        Uses incremental content updates to give the model proper turn structure.

        Args:
            context_turns: List of dicts with 'role' ('user'/'model') and 'text'
            text: The new message text to send
            images: Optional list of (bytes, mime_type) tuples (only first used)

        Returns:
            The complete text response from the model
        """
        await self._connected.wait()
        if not self._session:
            raise RuntimeError("Not connected to Gemini Live")

        # Drain stale responses from previous timeouts/cancellations
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Send conversation history as structured turns
        if context_turns:
            turns = []
            for turn in context_turns:
                turns.append(types.Content(
                    role=turn["role"],
                    parts=[types.Part.from_text(text=turn["text"])],
                ))
            await self._session.send_client_content(
                turns=turns,
                turn_complete=False,
            )

        # Send the new message
        if self.config.uses_realtime_text:
            # new gen models: inject images via client content, send text via realtime input
            if images:
                img_data, mime_type = images[0]
                await self._session.send_client_content(
                    turns=types.Content(role="user", parts=[
                        types.Part.from_bytes(data=img_data, mime_type=mime_type),
                    ]),
                    turn_complete=False,
                )
            await self._session.send_realtime_input(text=text)
        else:
            # 2.5 models: use send_client_content for everything
            parts = []
            if images:
                img_data, mime_type = images[0]
                parts.append(types.Part.from_bytes(data=img_data, mime_type=mime_type))
            parts.append(types.Part.from_text(text=text))
            await self._session.send_client_content(
                turns=types.Content(role="user", parts=parts),
                turn_complete=True,
            )

        # Wait for complete response
        response_text = await self._response_queue.get()
        return response_text

    async def inject_context(self, text):
        """Inject context into the session without expecting a response.
        Used for loading conversation history on startup."""
        await self._connected.wait()
        if not self._session:
            return
        if self.config.uses_realtime_text:
            # new gen models: use send_realtime_input for mid-session text
            await self._session.send_realtime_input(text=text)
        else:
            await self._session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=text)],
                ),
                turn_complete=False,
            )

    async def _receive_loop(self):
        """Continuously receive responses from Gemini Live."""
        while not self._closing:
            try:
                async for response in self._session.receive():
                    # Audio data from model_turn is discarded (we only need transcription)

                    # Capture output transcription (text version of audio response)
                    if (
                        response.server_content
                        and hasattr(response.server_content, "output_transcription")
                        and response.server_content.output_transcription
                    ):
                        transcription = response.server_content.output_transcription
                        if hasattr(transcription, "text") and transcription.text:
                            self._pending_transcript += transcription.text

                    # Thinking/thought parts (for logging)
                    if response.server_content and response.server_content.model_turn:
                        for part in response.server_content.model_turn.parts:
                            if getattr(part, "thought", False) and part.text:
                                logger.debug(f"Discord bot thinking: {part.text[:100]}")

                    # Turn complete
                    if response.server_content and response.server_content.turn_complete:
                        if self.config.model_capabilities.turn_complete_is_idle:
                            await self._deliver_transcript()
                        else:
                            # model can keep reasoning after a turn, so give it a chance
                            # to finish before we hand a half answer to the caller
                            self._awaiting_idle = True
                            self._idle_flush_task = asyncio.create_task(self._flush_after_grace())

                    if response.server_content and interaction_is_idle(response.server_content):
                        if self._awaiting_idle:
                            self._awaiting_idle = False
                            if self._idle_flush_task and not self._idle_flush_task.done():
                                self._idle_flush_task.cancel()
                            self._idle_flush_task = None
                            await self._deliver_transcript()

                    # Tool calls
                    if response.tool_call:
                        try:
                            responses = []
                            for fc in response.tool_call.function_calls:
                                logger.info(f"Discord tool call: {fc.name}({dict(fc.args) if fc.args else {}})")
                                fr = await self.tool_handler.handle(fc)
                                responses.append(fr)
                            await self._session.send_tool_response(function_responses=responses)
                            # If a personality switch happened, inject the prompt
                            if self.tool_handler._personality_prompt:
                                prompt = self.tool_handler._personality_prompt
                                self.tool_handler._personality_prompt = None
                                if self.config.uses_realtime_text:
                                    await self._session.send_realtime_input(text=prompt)
                                else:
                                    await self._session.send_client_content(
                                        turns=types.Content(
                                            role="user",
                                            parts=[types.Part.from_text(text=prompt)],
                                        ),
                                        turn_complete=False,
                                    )
                        except Exception as e:
                            logger.error(f"Discord tool dispatch error: {e}")

                    # Session resumption
                    if (
                        hasattr(response, "session_resumption_update")
                        and response.session_resumption_update
                    ):
                        update = response.session_resumption_update
                        if update.resumable and update.new_handle:
                            self._save_session_handle(update.new_handle)

                    # Go away
                    if response.go_away:
                        logger.warning(f"Discord session: server disconnecting in {response.go_away.time_left}")

            except asyncio.CancelledError:
                return
            except Exception as e:
                if self._closing:
                    return
                logger.error(f"Discord receive loop error: {e}")
                # Signal any waiting send_message calls
                await self._response_queue.put(f"[Error: {str(e)[:100]}]")
                raise

    async def run_forever(self):
        """Main loop - connects, handles errors, reconnects automatically."""
        while not self._closing:
            if self._is_handle_expired():
                self._clear_session_handle()
            try:
                self._session_resumed = bool(self._session_handle)
                connection = await self.connect()
                async with connection as session:
                    self._session = session
                    self._bad_response_streak = 0
                    self._pending_transcript = ""
                    self._awaiting_idle = False
                    if self._idle_flush_task and not self._idle_flush_task.done():
                        self._idle_flush_task.cancel()
                    self._idle_flush_task = None
                    if self._session_resumed:
                        logger.info("Discord bot resumed Gemini Live session")
                    else:
                        logger.info("Discord bot connected to Gemini Live (fresh session)")
                    self._connected.set()
                    self._receive_task = asyncio.create_task(self._receive_loop())
                    await self._receive_task
            except APIError as e:
                err_str = str(e)
                if "429" in err_str.lower() or "quota" in err_str.lower():
                    old_key = self.config.api_key
                    new_key = self.config.rotate_key()
                    if new_key != old_key:
                        logger.warning("Discord bot: rate limited, switched API key")
                        self._rate_limit_backoff = 0
                    else:
                        self._rate_limit_backoff = min(self._rate_limit_backoff + 1, 5)
                        wait = 5 * (2 ** self._rate_limit_backoff)
                        logger.warning(f"Discord bot: rate limited, waiting {wait}s")
                        await asyncio.sleep(wait)
                    continue
                if self._session_handle:
                    self._handle_fail_count += 1
                    if self._handle_fail_count >= 2:
                        self._clear_session_handle()
                logger.error(f"Discord bot API error: {e}")
                await asyncio.sleep(2)
            except Exception as e:
                if self._closing:
                    return
                logger.error(f"Discord bot session error: {e}")
                await asyncio.sleep(3)
            finally:
                self._connected.clear()
                self._session = None
