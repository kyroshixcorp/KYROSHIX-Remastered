"""Builds the LiveConnectConfig the session hands to genai client.aio.live.connect."""

import logging

from google.genai import types

from src.tools import get_tool_declarations

logger = logging.getLogger(__name__)


class ConfigBuilderMixin:
    def _needs_alpha_api(self):
        """Check if any v1alpha-only features are enabled (affective dialog, proactivity).
        Models that dont support them still connect over v1alpha, we just omit the params."""
        if not self.config.model_capabilities.alpha_features:
            return False
        return (self.config.enable_affective_dialog is not None
                or self.config.proactivity is not None)

    def _log_compat_notes(self, notes):
        # each distinct correction logs once per session so reconnects dont spam it
        seen = getattr(self, "_compat_notes_logged", None)
        if seen is None:
            seen = set()
            self._compat_notes_logged = seen
        for note in notes:
            key = str(note)
            if key in seen:
                continue
            seen.add(key)
            logger.warning(f"model compat: {note}")

    def _build_config(self, skip_alpha_features=False):
        caps = self.config.model_capabilities

        # Build VAD config based on mode
        if self.config.vad_mode == "silero":
            # Client-side Silero VAD: disable server VAD, we handle speech detection ourselves
            vad_config = types.AutomaticActivityDetection(disabled=True)
        else:
            # Server-side auto VAD with configurable sensitivity
            start_sens_map = {
                "START_SENSITIVITY_LOW": types.StartSensitivity.START_SENSITIVITY_LOW,
                "START_SENSITIVITY_HIGH": types.StartSensitivity.START_SENSITIVITY_HIGH,
            }
            end_sens_map = {
                "END_SENSITIVITY_LOW": types.EndSensitivity.END_SENSITIVITY_LOW,
                "END_SENSITIVITY_HIGH": types.EndSensitivity.END_SENSITIVITY_HIGH,
            }
            vad_config = types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=start_sens_map.get(
                    self.config.vad_start_sensitivity,
                    types.StartSensitivity.START_SENSITIVITY_HIGH,
                ),
                end_of_speech_sensitivity=end_sens_map.get(
                    self.config.vad_end_sensitivity,
                    types.EndSensitivity.END_SENSITIVITY_HIGH,
                ),
                prefix_padding_ms=self.config.vad_prefix_padding_ms,
                silence_duration_ms=self.config.vad_silence_duration_ms,
            )

        transcription_config = types.AudioTranscriptionConfig()

        if self.config.gemini_dynamic_tools:
            from src.tools.meta_router import build_meta_declarations
            tool_list = build_meta_declarations(self.config)
            declared = sum(len(t.function_declarations or []) for t in tool_list)
            logger.info(f"dynamic tools ON: {declared} declarations sent (searchTools/executeTool + core), rest reached via searchTools")
        else:
            tool_list = get_tool_declarations(self.config)

        config_kwargs = dict(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part.from_text(
                    text=self.config.build_system_instruction(self.personality)
                )]
            ),
            tools=tool_list,
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
                automatic_activity_detection=vad_config
            ),
            session_resumption=types.SessionResumptionConfig(
                handle=self._session_handle
            ) if self._session_handle else types.SessionResumptionConfig(),
        )

        # Skip session resumption if it keeps failing
        if self._resumption_fail_streak >= 3:
            config_kwargs["session_resumption"] = types.SessionResumptionConfig()
            logger.warning(f"Session resumption disabled (failed {self._resumption_fail_streak} times in a row)")
        elif self._session_handle:
            logger.debug(f"Config includes session handle: {self._session_handle[:24]}...")
        else:
            logger.debug("Config requesting new session handle (no existing handle)")

        if self.config.temperature is not None:
            config_kwargs["temperature"] = self.config.temperature
        if self.config.top_p is not None:
            config_kwargs["top_p"] = self.config.top_p
        if self.config.top_k is not None:
            config_kwargs["top_k"] = self.config.top_k
        if self.config.max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = self.config.max_output_tokens
        if not skip_alpha_features and caps.alpha_features:
            if self.config.enable_affective_dialog is not None:
                config_kwargs["enable_affective_dialog"] = self.config.enable_affective_dialog
            if self.config.proactivity is not None:
                config_kwargs["proactivity"] = self.config.proactivity
        self._log_compat_notes(self.config.alpha_feature_notes())

        # Context window compression
        # When custom compression is enabled, skip Gemini's built-in sliding window
        # to avoid 1007 errors at the threshold -- we handle it ourselves via summarization
        if self.config.compression_enabled and not self.config.custom_compression_enabled:
            sw_kwargs = {}
            if self.config.compression_target_tokens is not None:
                sw_kwargs["target_tokens"] = self.config.compression_target_tokens
            cw_kwargs = {"sliding_window": types.SlidingWindow(**sw_kwargs)}
            if self.config.compression_trigger_tokens is not None:
                cw_kwargs["trigger_tokens"] = self.config.compression_trigger_tokens
            config_kwargs["context_window_compression"] = types.ContextWindowCompressionConfig(**cw_kwargs)
            trigger = self.config.compression_trigger_tokens or "default"
            target = self.config.compression_target_tokens or "default"
            logger.info(f"Context compression enabled (trigger={trigger}, target={target})")
        elif self.config.custom_compression_enabled:
            # No built-in compression at all -- we handle it via summarization + reconnect
            trigger = self.config.custom_compression_trigger_tokens or "auto"
            logger.info(f"Custom context compression enabled (trigger={trigger} tokens)")

        # Media resolution (reduces image token cost, critical for 3.1 free tier)
        media_res = self.config.vision_media_resolution
        if media_res and self.config.vision_enabled:
            res_map = {
                "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
                "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            }
            resolved = res_map.get(media_res.lower())
            if resolved:
                config_kwargs["media_resolution"] = resolved
                token_map = {"low": 280, "medium": 560, "high": 1120}
                tokens = token_map.get(media_res.lower(), "?")
                logger.info(f"Media resolution: {media_res} (~{tokens} tokens/frame)")

        # Thinking configuration. the resolver maps whatever the user set onto
        # what this model actually accepts, so a wrong pick gets nudged or
        # dropped with a logged reason instead of failing the connect.
        thinking_kwargs, thinking_notes = self.config.resolve_thinking_config()
        self._log_compat_notes(thinking_notes)
        if thinking_kwargs:
            config_kwargs["thinking_config"] = types.ThinkingConfig(**thinking_kwargs)
            shown = ", ".join(f"{k}={v}" for k, v in thinking_kwargs.items())
            logger.info(f"Thinking config: {shown}")
        elif caps.thinking == "none":
            logger.info(f"{caps.name} reasons on its own, no thinking config sent")

        # New gen models need initial_history_in_client_content=true so the first
        # send_client_content (replay/summary seeding on a fresh connect) is
        # accepted as history rather than rejected as a mid-session update.
        if caps.history_config:
            config_kwargs["history_config"] = types.HistoryConfig(
                initial_history_in_client_content=True
            )

        return types.LiveConnectConfig(**config_kwargs)
