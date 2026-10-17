import pyaudio
import numpy as np
import logging
import os
import re
import time
import warnings
import shutil
from pathlib import Path
from urllib.parse import urlparse

warnings.filterwarnings("ignore", message="pkg_resources is deprecated", category=UserWarning)
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame

logger = logging.getLogger(__name__)

MUSIC_FADEOUT_START = 2.0
MUSIC_FADEOUT_END = 10.0

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma"}


class _PitchShifter:
    """Pedalboard-based streaming pitch shifter."""
    _BLOCK = 4096
    _MAX_BUF = 20480

    def __init__(self, sample_rate=24000):
        from pedalboard import Pedalboard, PitchShift

        self.sample_rate = sample_rate
        self._semitones = 0.0
        self._shift = PitchShift(semitones=0.0)
        self._board = Pedalboard([self._shift])
        self._in_pending: list[np.ndarray] = []
        self._in_len = 0
        self._out_parts: list[np.ndarray] = []
        self._out_ready = 0
        self._warmup()

    def _warmup(self):
        for _ in range(4):
            silence = np.zeros((1, self._BLOCK), dtype=np.float32)
            result = self._board(silence, self.sample_rate)
            out = result[0] * 32767.0
            if len(out) > 0:
                self._out_parts.append(out)
                self._out_ready += len(out)

    def reset(self):
        from pedalboard import Pedalboard, PitchShift

        self._shift = PitchShift(semitones=self._semitones)
        self._board = Pedalboard([self._shift])
        self._in_pending.clear()
        self._in_len = 0
        self._out_parts.clear()
        self._out_ready = 0
        self._warmup()

    def process(self, chunk: np.ndarray, semitones: float) -> np.ndarray:
        if semitones != self._semitones:
            self._semitones = semitones
            self._shift.semitones = semitones

        n = len(chunk)
        audio = chunk.astype(np.float32) / 32767.0
        self._in_pending.append(audio)
        self._in_len += len(audio)

        if self._in_len >= self._BLOCK:
            block = np.concatenate(self._in_pending)
            self._in_pending.clear()
            self._in_len = 0
            result = self._board(block.reshape(1, -1), self.sample_rate)
            processed = result[0] * 32767.0
            self._out_parts.append(processed)
            self._out_ready += len(processed)

        if self._out_ready >= n:
            combined = np.concatenate(self._out_parts)
            drain = n

            if len(combined) > self._MAX_BUF:
                extra = len(combined) - self._MAX_BUF
                drain = min(n + extra, len(combined))

            out = combined[:drain]

            if drain != n:
                x_old = np.linspace(0, 1, drain)
                x_new = np.linspace(0, 1, n)
                out = np.interp(x_new, x_old, out)

            leftover = combined[drain:]

            if len(leftover) > 0:
                self._out_parts = [leftover]
                self._out_ready = len(leftover)
            else:
                self._out_parts.clear()
                self._out_ready = 0

            return out

        return np.zeros(n, dtype=np.float32)


class _ResamplingInputStream:
    def __init__(self, stream, native_rate, target_rate):
        self._stream = stream
        self._native_rate = native_rate
        self._target_rate = target_rate

    def read(self, num_frames, exception_on_overflow=False):
        native_frames = int(round(num_frames * self._native_rate / self._target_rate))
        data = self._stream.read(native_frames, exception_on_overflow=exception_on_overflow)
        samples = np.frombuffer(data, dtype=np.int16)

        if self._native_rate == self._target_rate or len(samples) == 0:
            return data

        new_len = max(1, int(round(len(samples) * self._target_rate / self._native_rate)))
        x_old = np.arange(len(samples))
        x_new = np.linspace(0, len(samples) - 1, new_len)
        out = np.interp(x_new, x_old, samples).astype(np.int16)
        return out.tobytes()

    def stop_stream(self):
        return self._stream.stop_stream()

    def close(self):
        return self._stream.close()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _ResamplingOutputStream:
    def __init__(self, stream, native_rate, target_rate):
        self._stream = stream
        self._native_rate = native_rate
        self._target_rate = target_rate

    def write(self, data, *args, **kwargs):
        if self._native_rate == self._target_rate or not data:
            return self._stream.write(data, *args, **kwargs)

        samples = np.frombuffer(data, dtype=np.int16)

        if len(samples) == 0:
            return self._stream.write(data, *args, **kwargs)

        new_len = max(1, int(round(len(samples) * self._native_rate / self._target_rate)))
        x_old = np.arange(len(samples))
        x_new = np.linspace(0, len(samples) - 1, new_len)
        resampled = np.interp(x_new, x_old, samples).astype(np.int16).tobytes()
        return self._stream.write(resampled, *args, **kwargs)

    def stop_stream(self):
        return self._stream.stop_stream()

    def close(self):
        return self._stream.close()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _SilentInputStream:
    def __init__(self, chunk_size: int, sample_rate: int = 16000):
        self.chunk_size = chunk_size
        self.sample_rate = sample_rate

    def read(self, num_frames, exception_on_overflow=False):
        return b"\x00" * max(1, num_frames * 2)

    def stop_stream(self):
        return None

    def close(self):
        return None


class _SilentOutputStream:
    def __init__(self):
        self.closed = False

    def write(self, data, *args, **kwargs):
        if self.closed:
            return 0
        return len(data) if data is not None else 0

    def stop_stream(self):
        return None

    def close(self):
        self.closed = True
        return None


def _is_invalid_sample_rate_error(exc: BaseException) -> bool:
    if getattr(exc, "errno", None) == -9997:
        return True
    return "Invalid sample rate" in str(exc)


class AudioManager:
    def __init__(self, config):
        self.config = config
        self.pya = pyaudio.PyAudio()
        self.boost_level = 0
        self._music_start_time = None
        self._music_paused_at = None
        self._current_song_name = None
        self._current_song_duration = None
        self._current_volume = 50
        self._using_boosted_sound = False
        self._boosted_sound_channel = None
        self._lyrics = []
        self._pygame_ready = False
        self._thinking_sound = None
        self._thinking_channel = None
        self._pitch_semitones = 0.0
        self._pitch_shifter = None
        self._low_quality = False
        self._lq_downsample = 4
        self._lq_bitcrush = 256.0
        self._lq_noise = 800.0
        self._lq_glitch = 0.03
        self._external_music_active = False
        self._external_music_started_at: float | None = None
        self._setup_devices()

    def _find_first_device(self, device_type: str):
        try:
            count = self.pya.get_device_count()
        except Exception:
            return None

        for index in range(count):
            try:
                info = self.pya.get_device_info_by_index(index)
            except Exception:
                continue

            channels = info.get("maxInputChannels" if device_type == "input" else "maxOutputChannels", 0)
            if int(channels) > 0:
                return index

        return None

    def _setup_devices(self):
        if self.config.input_device is not None:
            self.input_device = self.config.input_device
        else:
            try:
                self.input_device = self.pya.get_default_input_device_info()["index"]
            except Exception:
                fallback = self._find_first_device("input")
                if fallback is None:
                    logger.warning("No input audio device available. Voice input is disabled until a microphone is connected or selected.")
                    self.input_device = None
                else:
                    logger.warning(
                        "No default input device available. Falling back to '%s' (index %s).",
                        self.pya.get_device_info_by_index(fallback).get("name", f"#{fallback}"),
                        fallback,
                    )
                    self.input_device = fallback

        if self.config.output_device is not None:
            self.output_device = self.config.output_device
        else:
            try:
                self.output_device = self.pya.get_default_output_device_info()["index"]
            except Exception:
                fallback = self._find_first_device("output")
                if fallback is None:
                    logger.warning("No output audio device available. Audio playback is disabled until a speaker/headset is connected or selected.")
                    self.output_device = None
                else:
                    logger.warning(
                        "No default output device available. Falling back to '%s' (index %s).",
                        self.pya.get_device_info_by_index(fallback).get("name", f"#{fallback}"),
                        fallback,
                    )
                    self.output_device = fallback

    def _setup_pygame(self):
        if self._pygame_ready:
            return
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        self._pygame_ready = True

    def open_input_stream(self):
        if self.input_device is None:
            logger.warning(
                "No input audio device available. Running in silent input mode; connect a microphone or set audio.input_device in config.yml."
            )
            return _SilentInputStream(self.config.chunk_size, self.config.send_sample_rate)

        target_rate = self.config.send_sample_rate

        try:
            return self.pya.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=target_rate,
                input=True,
                input_device_index=self.input_device,
                frames_per_buffer=self.config.chunk_size,
            )
        except OSError as e:
            if not _is_invalid_sample_rate_error(e):
                logger.error("Unable to open microphone device %s: %s. Using silent input.", self.input_device, e)
                self.input_device = None
                return _SilentInputStream(self.config.chunk_size, target_rate)

            try:
                info = self.pya.get_device_info_by_index(self.input_device)
                native_rate = int(info.get("defaultSampleRate", 48000))
                dev_name = info.get("name", f"#{self.input_device}")
            except Exception:
                native_rate = 48000
                dev_name = f"#{self.input_device}"

            if native_rate == target_rate:
                native_rate = 48000

            native_chunk = int(round(self.config.chunk_size * native_rate / target_rate))

            logger.warning(
                f"Mic '{dev_name}' wont open at {target_rate} Hz, "
                f"falling back to {native_rate} Hz with software resampling."
            )

            try:
                stream = self.pya.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=native_rate,
                    input=True,
                    input_device_index=self.input_device,
                    frames_per_buffer=native_chunk,
                )
            except OSError as fallback_error:
                logger.error(
                    "Unable to open microphone device %s at %s Hz: %s. Using silent input.",
                    self.input_device,
                    native_rate,
                    fallback_error,
                )
                self.input_device = None
                return _SilentInputStream(self.config.chunk_size, target_rate)
            return _ResamplingInputStream(stream, native_rate, target_rate)

    def open_output_stream(self):
        if self.output_device is None:
            logger.warning(
                "No output audio device available. Running in silent output mode; connect a speaker/headset or set audio.output_device in config.yml."
            )
            return _SilentOutputStream()

        target_rate = self.config.receive_sample_rate

        try:
            return self.pya.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=target_rate,
                output=True,
                output_device_index=self.output_device,
            )
        except OSError as e:
            if not _is_invalid_sample_rate_error(e):
                raise

            try:
                info = self.pya.get_device_info_by_index(self.output_device)
                native_rate = int(info.get("defaultSampleRate", 48000))
                dev_name = info.get("name", f"#{self.output_device}")
            except Exception:
                native_rate = 48000
                dev_name = f"#{self.output_device}"

            if native_rate == target_rate:
                native_rate = 48000

            logger.warning(
                f"Speaker '{dev_name}' wont open at {target_rate} Hz, "
                f"falling back to {native_rate} Hz with software resampling."
            )

            stream = self.pya.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=native_rate,
                output=True,
                output_device_index=self.output_device,
            )
            return _ResamplingOutputStream(stream, native_rate, target_rate)

    def is_music_playing(self) -> bool:
        if self._external_music_active:
            return True
        if not self._pygame_ready:
            return False
        return pygame.mixer.music.get_busy() or pygame.mixer.get_busy()

    def set_external_music_active(self, active: bool):
        if active and not self._external_music_active:
            self._external_music_started_at = time.time()
            self._music_start_time = self._external_music_started_at
            self._music_paused_at = None
        elif not active and self._external_music_active:
            if not (
                self._pygame_ready
                and (pygame.mixer.music.get_busy() or pygame.mixer.get_busy())
            ):
                self._music_start_time = None
            self._external_music_started_at = None

        self._external_music_active = active

    def get_voice_volume_multiplier(self) -> float:
        if not self.is_music_playing():
            if self._music_paused_at is None:
                self._music_start_time = None
            return 1.0

        if self._music_start_time is None:
            return 1.0

        elapsed = time.time() - self._music_start_time

        if elapsed < MUSIC_FADEOUT_START:
            return 1.0
        if elapsed >= MUSIC_FADEOUT_END:
            return 0.0

        fade_progress = (
            (elapsed - MUSIC_FADEOUT_START)
            / (MUSIC_FADEOUT_END - MUSIC_FADEOUT_START)
        )
        return 1.0 - fade_progress

    def process_output_audio(self, data: bytes) -> bytes:
        voice_mult = self.get_voice_volume_multiplier()

        if voice_mult == 0.0:
            return b"\x00" * len(data)

        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)

        if self.boost_level > 0:
            gain = 1.0 + (self.boost_level * 0.8)
            samples *= gain

            if self.boost_level >= 3 and len(samples):
                alpha = 0.15 + (self.boost_level * 0.03)
                boosted = np.zeros_like(samples)
                boosted[0] = samples[0]

                for i in range(1, len(samples)):
                    boosted[i] = alpha * samples[i] + (1 - alpha) * boosted[i - 1]

                samples = samples + boosted * (self.boost_level * 0.3)

            if self.boost_level >= 2:
                max_val = 32767.0
                samples = np.tanh(
                    samples / max_val * (1 + self.boost_level * 0.2)
                ) * max_val

        if voice_mult < 1.0:
            samples *= voice_mult

        if (
            self._pitch_semitones != 0.0
            and self.config.get("audio", "pitch_shift", "enabled", default=False)
        ):
            if self._pitch_shifter is None:
                self._pitch_shifter = _PitchShifter(self.config.receive_sample_rate)
            samples = self._pitch_shifter.process(samples, self._pitch_semitones)

        if self._low_quality:
            samples = self._apply_low_quality(samples)

        samples = np.clip(samples, -32767, 32767).astype(np.int16)
        return samples.tobytes()

    def set_pitch(self, semitones: float):
        max_st = self.config.get("audio", "pitch_shift", "max_semitones", default=12)
        self._pitch_semitones = max(-max_st, min(max_st, semitones))

        if self._pitch_shifter is not None:
            self._pitch_shifter.reset()

        logger.info(f"Voice pitch set to {self._pitch_semitones:+.1f} semitones")

    def get_pitch(self) -> float:
        return self._pitch_semitones

    def set_boost(self, level: int):
        self.boost_level = max(0, min(10, level))
        logger.info(f"Voice boost set to {self.boost_level}")

    def set_low_quality(self, enabled: bool, **kwargs):
        self._low_quality = enabled

        if "downsample" in kwargs:
            self._lq_downsample = max(1, min(8, int(kwargs["downsample"])))
        if "bitcrush" in kwargs:
            self._lq_bitcrush = max(16.0, min(1024.0, float(kwargs["bitcrush"])))
        if "noise" in kwargs:
            self._lq_noise = max(0.0, min(3000.0, float(kwargs["noise"])))
        if "glitch" in kwargs:
            self._lq_glitch = max(0.0, min(0.2, float(kwargs["glitch"])))

        logger.info(
            f"Low quality mic {'enabled' if enabled else 'disabled'}"
            + (
                f" (ds={self._lq_downsample}, bc={self._lq_bitcrush}, "
                f"noise={self._lq_noise}, glitch={self._lq_glitch})"
                if enabled
                else ""
            )
        )

    def get_low_quality(self) -> bool:
        return self._low_quality

    def get_low_quality_settings(self) -> dict:
        return {
            "enabled": self._low_quality,
            "downsample": self._lq_downsample,
            "bitcrush": self._lq_bitcrush,
            "noise": self._lq_noise,
            "glitch": self._lq_glitch,
        }

    def _apply_low_quality(self, samples: np.ndarray) -> np.ndarray:
        if len(samples) == 0:
            return samples

        factor = self._lq_downsample
        if factor > 1:
            downsampled = samples[::factor]
            samples = np.repeat(downsampled, factor)[: len(samples)]

        step = self._lq_bitcrush
        samples = np.round(samples / step) * step

        if self._lq_noise > 0:
            noise = np.random.normal(
                0, self._lq_noise, len(samples)
            ).astype(np.float32)
            samples = samples + noise

        sr = self.config.receive_sample_rate

        rc_hp = 1.0 / (2.0 * np.pi * 300.0)
        alpha_hp = rc_hp / (rc_hp + 1.0 / sr)
        hp_out = np.zeros_like(samples)
        prev_in = samples[0]
        prev_out = samples[0]

        for i in range(1, len(samples)):
            prev_out = alpha_hp * (prev_out + samples[i] - prev_in)
            prev_in = samples[i]
            hp_out[i] = prev_out

        rc_lp = 1.0 / (2.0 * np.pi * 3400.0)
        alpha_lp = (1.0 / sr) / (rc_lp + 1.0 / sr)
        lp_out = np.zeros_like(hp_out)
        lp_out[0] = hp_out[0]

        for i in range(1, len(hp_out)):
            lp_out[i] = lp_out[i - 1] + alpha_lp * (hp_out[i] - lp_out[i - 1])

        if (
            self._lq_glitch > 0
            and np.random.random() < self._lq_glitch
            and len(lp_out) > 200
        ):
            pos = np.random.randint(0, len(lp_out) - 100)
            lp_out[pos : pos + 50] = lp_out[pos : pos + 50][[0] * 50]

        return lp_out

    # ------------------------------------------------------------------
    # Music library / resolving
    # ------------------------------------------------------------------

    def list_music(self) -> list[str]:
        music_dir = Path(self.config.music_dir)
        music_dir.mkdir(parents=True, exist_ok=True)

        files = []

        for filepath in music_dir.rglob("*"):
            if filepath.is_file() and filepath.suffix.lower() in AUDIO_EXTENSIONS:
                files.append(str(filepath.relative_to(music_dir)))

        return sorted(files, key=str.casefold)

    @staticmethod
    def _normalize_music_text(value: str) -> str:
        value = os.path.splitext(os.path.basename(value))[0]
        value = value.casefold()
        value = re.sub(r"\[[^\]]+\]", " ", value)
        value = re.sub(r"[_\-–—|]+", " ", value)
        value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
        return " ".join(value.split())

    def find_local_music(self, query: str) -> str | None:
        """Find the best local song match without requiring the exact filename."""
        query_norm = self._normalize_music_text(query)

        if not query_norm:
            return None

        songs = self.list_music()

        # Exact normalized match first.
        for song in songs:
            if self._normalize_music_text(song) == query_norm:
                return song

        # Then substring/token matching.
        query_tokens = set(query_norm.split())
        best_song = None
        best_score = 0.0

        for song in songs:
            song_norm = self._normalize_music_text(song)

            if query_norm in song_norm:
                return song

            song_tokens = set(song_norm.split())

            if not query_tokens:
                continue

            overlap = len(query_tokens & song_tokens) / len(query_tokens)

            if overlap > best_score:
                best_score = overlap
                best_song = song

        return best_song if best_score >= 0.6 else None

    def play_music_request(self, request: str, volume: int = 50) -> dict:
        """
        Unified music entry point.

        Accepts:
          - local filename/path
          - HTTP/HTTPS media URL
          - natural song/artist search text

        This is the method the voice/chat tool should call.
        """
        if not isinstance(request, str) or not request.strip():
            return {
                "result": "error",
                "message": "Music request is empty",
            }

        request = request.strip()
        volume = self._clamp_music_volume(volume)

        if self._is_http_url(request):
            return self.play_music_url(request, volume)

        local_match = self.find_local_music(request)

        if local_match:
            if self.play_music(local_match, volume):
                return {
                    "result": "ok",
                    "message": "playing",
                    "title": os.path.splitext(os.path.basename(local_match))[0],
                    "filename": local_match,
                    "source": "local",
                    "volume": volume,
                }

            return {
                "result": "error",
                "message": f"Local song was found but playback failed: {local_match}",
            }

        return self.search_and_play_music(request, volume)

    def search_and_play_music(self, query: str, volume: int = 50) -> dict:
        """
        Search YouTube by song/artist name, then download/cache audio and play it.

        Example:
            search_and_play_music("Bebe Rexha Heart Still Beating")
        """
        query = str(query or "").strip()

        if not query:
            return {
                "result": "error",
                "message": "Search query is empty",
            }

        # ytsearch1 is an official yt-dlp search prefix.
        return self._download_and_play_music(
            f"ytsearch1:{query}",
            volume=volume,
            original_request=query,
            source_kind="search",
        )

    def play_music(self, filename: str, volume: int = 50) -> bool:
        self._setup_pygame()

        music_dir = Path(self.config.music_dir).resolve()
        filepath = (music_dir / filename).resolve()

        # Do not allow ../ to escape the configured music directory.
        try:
            filepath.relative_to(music_dir)
        except ValueError:
            logger.error(f"Refusing music path outside music directory: {filepath}")
            return False

        if not filepath.exists() or not filepath.is_file():
            logger.warning(f"Music file not found: {filepath}")
            return False

        try:
            self.stop_music()

            self._current_volume = self._clamp_music_volume(volume)
            vol_float = self._current_volume / 100.0
            self._music_start_time = time.time()
            self._music_paused_at = None
            self._current_song_name = filepath.stem
            self._current_song_duration = self._get_audio_duration(str(filepath))
            self._lyrics = self._load_srt(str(filepath.relative_to(music_dir)))

            if vol_float <= 1.0:
                self._using_boosted_sound = False
                self._boosted_sound_channel = None
                pygame.mixer.music.load(str(filepath))
                pygame.mixer.music.set_volume(vol_float)
                pygame.mixer.music.play()
            else:
                self._using_boosted_sound = True
                sound = pygame.mixer.Sound(str(filepath))
                arr = pygame.sndarray.array(sound)
                arr = (
                    arr.astype(np.float32) * vol_float
                ).clip(-32767, 32767).astype(np.int16)
                boosted = pygame.sndarray.make_sound(arr)
                self._boosted_sound_channel = boosted.play()

            logger.info(
                f"Playing music: {filename} "
                f"(duration: {self._current_song_duration:.1f}s, "
                f"volume: {self._current_volume}%)"
            )
            return True

        except Exception as e:
            self._clear_music_state()
            logger.exception(f"Music playback failed for {filepath}: {e}")
            return False

    def play_music_url(self, url: str, volume: int = 50) -> dict:
        """Resolve a supported public media URL, cache its audio, and play it."""
        if not isinstance(url, str):
            return {
                "result": "error",
                "message": "Invalid URL",
            }

        url = url.strip()

        if not self._is_http_url(url):
            return {
                "result": "error",
                "message": "Invalid URL. Use an http:// or https:// address.",
            }

        return self._download_and_play_music(
            url,
            volume=volume,
            original_request=url,
            source_kind="url",
        )

    def _download_and_play_music(
        self,
        target: str,
        volume: int = 50,
        original_request: str | None = None,
        source_kind: str = "url",
    ) -> dict:
        """
        yt-dlp backend shared by URL playback and text search.

        Downloads audio only, converts to MP3, reuses cached media IDs, then
        hands playback to the existing pygame music path.
        """
        try:
            import yt_dlp
        except ImportError:
            logger.error("yt-dlp is not installed")
            return {
                "result": "error",
                "message": "yt-dlp is not installed",
            }

        if shutil.which("ffmpeg") is None:
            return {
                "result": "error",
                "message": "FFmpeg was not found in PATH",
            }

        volume = self._clamp_music_volume(volume)

        music_dir = Path(self.config.music_dir).resolve()
        music_dir.mkdir(parents=True, exist_ok=True)

        cache_dir = (music_dir / "url_cache").resolve()
        cache_dir.mkdir(parents=True, exist_ok=True)

        ydl_opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": str(cache_dir / "%(title).120B [%(id)s].%(ext)s"),
            "windowsfilenames": True,
            "overwrites": False,
            "continuedl": True,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "quiet": True,
            "no_warnings": False,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }

        logger.info(f"Resolving music {source_kind}: {original_request or target}")

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Resolve first so a cached ID can be reused without downloading.
                info = ydl.extract_info(target, download=False)
                info = self._first_media_entry(info)

                if not info:
                    return {
                        "result": "error",
                        "message": "No playable media was found",
                    }

                media_id = str(info.get("id") or "").strip()
                title = str(info.get("title") or "Online Music").strip()
                webpage_url = str(
                    info.get("webpage_url")
                    or info.get("original_url")
                    or original_request
                    or target
                )
                extractor = str(
                    info.get("extractor_key")
                    or info.get("extractor")
                    or "unknown"
                )
                duration = info.get("duration")

                final_path = self._find_cached_media(cache_dir, media_id)

                if final_path is not None:
                    logger.info(f"Music cache hit: {title} -> {final_path.name}")
                else:
                    logger.info(f"Music cache miss: downloading audio for {title}")

                    info = ydl.extract_info(target, download=True)
                    info = self._first_media_entry(info)

                    if not info:
                        return {
                            "result": "error",
                            "message": "yt-dlp downloaded no playable media",
                        }

                    media_id = str(info.get("id") or media_id).strip()
                    title = str(info.get("title") or title).strip()
                    webpage_url = str(
                        info.get("webpage_url")
                        or info.get("original_url")
                        or webpage_url
                    )
                    extractor = str(
                        info.get("extractor_key")
                        or info.get("extractor")
                        or extractor
                    )
                    duration = info.get("duration") or duration

                    downloaded_path = Path(ydl.prepare_filename(info))
                    expected_mp3 = downloaded_path.with_suffix(".mp3")

                    if expected_mp3.exists():
                        final_path = expected_mp3.resolve()
                    elif (
                        downloaded_path.exists()
                        and downloaded_path.suffix.lower() == ".mp3"
                    ):
                        final_path = downloaded_path.resolve()
                    else:
                        final_path = self._find_cached_media(cache_dir, media_id)

                    if final_path is None:
                        return {
                            "result": "error",
                            "message": (
                                "Audio was downloaded, but the converted MP3 "
                                "could not be found"
                            ),
                        }

                    logger.info(f"URL music downloaded: {title} -> {final_path.name}")

            try:
                relative_path = final_path.resolve().relative_to(music_dir)
            except ValueError:
                logger.error(f"Downloaded file is outside music directory: {final_path}")
                return {
                    "result": "error",
                    "message": "Downloaded file is outside music directory",
                }

            if not self.play_music(str(relative_path), volume):
                return {
                    "result": "error",
                    "message": "Audio resolved successfully but playback failed",
                }

            result = {
                "result": "ok",
                "message": "playing",
                "title": title,
                "filename": str(relative_path),
                "source_url": webpage_url,
                "source": source_kind,
                "extractor": extractor,
                "volume": volume,
            }

            if duration is not None:
                try:
                    result["duration"] = float(duration)
                except (TypeError, ValueError):
                    pass

            logger.info(
                f"Playing resolved music: {title} "
                f"(source={source_kind}, extractor={extractor})"
            )
            return result

        except yt_dlp.utils.DownloadError as e:
            logger.error(f"yt-dlp failed for {target}: {e}")
            return {
                "result": "error",
                "message": f"Could not resolve/download media: {e}",
            }
        except Exception as e:
            logger.exception(f"Music resolution failed for {target}")
            return {
                "result": "error",
                "message": str(e),
            }

    @staticmethod
    def _first_media_entry(info):
        """Return the first playable item from yt-dlp search/playlist-like results."""
        if not info:
            return None

        if "entries" not in info:
            return info

        for entry in info.get("entries") or []:
            if entry:
                return entry

        return None

    @staticmethod
    def _find_cached_media(cache_dir: Path, media_id: str) -> Path | None:
        if not media_id:
            return None

        # Escape glob metacharacters from arbitrary extractor IDs.
        safe_id = (
            media_id.replace("[", "[[]")
            .replace("]", "[]]")
            .replace("*", "[*]")
            .replace("?", "[?]")
        )

        matches = list(cache_dir.glob(f"*[{safe_id}].mp3"))

        if matches:
            return matches[0].resolve()

        # Robust fallback if glob escaping behaves differently on Windows.
        suffix = f"[{media_id}].mp3".casefold()

        for candidate in cache_dir.glob("*.mp3"):
            if candidate.name.casefold().endswith(suffix):
                return candidate.resolve()

        return None

    @staticmethod
    def _is_http_url(value: str) -> bool:
        try:
            parsed = urlparse(value.strip())
            return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
        except Exception:
            return False

    @staticmethod
    def _clamp_music_volume(volume: int) -> int:
        try:
            return max(0, min(300, int(volume)))
        except (TypeError, ValueError):
            return 50

    def _clear_music_state(self):
        self._music_start_time = None
        self._music_paused_at = None
        self._current_song_name = None
        self._current_song_duration = None
        self._using_boosted_sound = False
        self._boosted_sound_channel = None
        self._lyrics = []

    def _get_audio_duration(self, filepath: str) -> float:
        try:
            from mutagen import File

            audio = File(filepath)
            if audio is not None and audio.info is not None:
                return audio.info.length
        except Exception:
            pass

        try:
            self._setup_pygame()
            sound = pygame.mixer.Sound(filepath)
            return sound.get_length()
        except Exception:
            pass

        return 0.0

    def _parse_srt_time(self, ts: str) -> float:
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", ts.strip())

        if not m:
            return 0.0

        h, mi, s, ms = (
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4)),
        )
        return h * 3600 + mi * 60 + s + ms / 1000.0

    def _load_srt(self, music_filename: str) -> list:
        stem = Path(music_filename).stem
        srt_path = Path(self.config.music_dir) / "srt" / f"{stem}.srt"

        if not srt_path.exists():
            return []

        try:
            text = srt_path.read_text(encoding="utf-8")
            entries = []
            blocks = re.split(r"\n\s*\n", text.strip())

            for block in blocks:
                lines = block.strip().split("\n")

                if len(lines) < 3:
                    continue

                arrow = re.split(r"\s*-->\s*", lines[1])

                if len(arrow) != 2:
                    continue

                start = self._parse_srt_time(arrow[0])
                end = self._parse_srt_time(arrow[1])
                lyric = " ".join(lines[2:]).strip()

                if lyric:
                    entries.append((start, end, lyric))

            if entries:
                logger.info(f"Loaded {len(entries)} lyric entries from {srt_path.name}")

            return entries

        except Exception as e:
            logger.warning(f"Failed to load SRT {srt_path}: {e}")
            return []

    def get_current_lyric(self) -> str | None:
        if not self._lyrics or self._music_start_time is None:
            return None

        if self._music_paused_at is not None:
            pos = self._music_paused_at - self._music_start_time
        else:
            pos = time.time() - self._music_start_time

        for start, end, text in self._lyrics:
            if start <= pos < end:
                return text

        return None

    def get_music_progress(self) -> dict | None:
        if self._music_start_time is None:
            return None

        if self._external_music_active and self._current_song_name is None:
            return None

        if self._music_paused_at is not None:
            position = self._music_paused_at - self._music_start_time
        elif not self.is_music_playing():
            return None
        else:
            position = time.time() - self._music_start_time

        duration = self._current_song_duration or 0.0
        progress = (position / duration) if duration > 0 else 0.0

        return {
            "song_name": self._current_song_name or "Unknown",
            "position": position,
            "duration": duration,
            "progress": min(1.0, progress),
        }

    def stop_music(self):
        if self._pygame_ready:
            pygame.mixer.music.stop()
            pygame.mixer.stop()

        self._clear_music_state()

    def pause_music(self) -> bool:
        if self._music_paused_at is not None:
            return False

        if not self.is_music_playing():
            return False

        if self._using_boosted_sound:
            if self._boosted_sound_channel:
                self._boosted_sound_channel.pause()
        else:
            pygame.mixer.music.pause()

        if self._music_start_time:
            self._music_paused_at = time.time()

        logger.info("Music paused")
        return True

    def resume_music(self) -> bool:
        if self._music_paused_at is None:
            return False

        if self._using_boosted_sound:
            if self._boosted_sound_channel:
                self._boosted_sound_channel.unpause()
        else:
            pygame.mixer.music.unpause()

        if self._music_start_time:
            pause_duration = time.time() - self._music_paused_at
            self._music_start_time += pause_duration

        self._music_paused_at = None
        logger.info("Music resumed")
        return True

    def set_music_volume(self, volume: int) -> bool:
        if not self.is_music_playing() and self._music_paused_at is None:
            return False

        self._current_volume = self._clamp_music_volume(volume)
        vol_float = self._current_volume / 100.0

        if not self._using_boosted_sound:
            pygame.mixer.music.set_volume(min(vol_float, 1.0))
            logger.info(f"Music volume set to {self._current_volume}%")
            return True

        logger.info(
            f"Volume change requested to {self._current_volume}% "
            "(boosted mode - takes full effect on the next playback)"
        )
        return True

    def play_sfx_file(self, filepath: str, boost: int = 0) -> bool:
        self._setup_pygame()

        try:
            sound = pygame.mixer.Sound(filepath)

            if boost > 0:
                raw = pygame.sndarray.array(sound)
                samples = raw.astype(np.float32)
                gain = 1.0 + (boost * 0.8)
                samples *= gain

                if boost >= 3 and len(samples):
                    alpha = 0.15 + (boost * 0.03)
                    bass = np.zeros_like(samples)
                    bass[0] = samples[0]

                    for i in range(1, len(samples)):
                        bass[i] = alpha * samples[i] + (1 - alpha) * bass[i - 1]

                    samples = samples + bass * (boost * 0.3)

                if boost >= 2:
                    max_val = 32767.0
                    samples = np.tanh(
                        samples / max_val * (1 + boost * 0.2)
                    ) * max_val

                samples = np.clip(samples, -32767, 32767).astype(np.int16)
                sound = pygame.sndarray.make_sound(samples)
                logger.info(f"SFX boost applied: level {boost}")

            sound.play()
            return True

        except Exception as e:
            logger.error(f"SFX playback failed: {e}")
            return False

    def stop_sfx(self):
        if not self._pygame_ready:
            return

        pygame.mixer.stop()
        self._thinking_channel = None
        logger.info("All SFX playback stopped")

    def start_thinking_sound(self, source="thinking"):
        if not self.config.thinking_sound_enabled:
            return
        if source == "thinking" and not self.config.thinking_sound_on_thinking:
            return
        if source == "recall" and not self.config.thinking_sound_on_recall:
            return

        self._setup_pygame()
        filepath = self.config.thinking_sound_file

        if not os.path.exists(filepath):
            logger.warning(f"Thinking sound file not found: {filepath}")
            return

        if self._thinking_channel and self._thinking_channel.get_busy():
            return

        try:
            if self._thinking_sound is None:
                self._thinking_sound = pygame.mixer.Sound(filepath)

            vol = min(max(0, self.config.thinking_sound_volume), 100) / 100.0
            self._thinking_sound.set_volume(vol)
            fade_in = self.config.thinking_sound_fade_in_ms
            self._thinking_channel = self._thinking_sound.play(
                loops=-1,
                fade_ms=fade_in,
            )
            logger.debug("Thinking sound started")

        except Exception as e:
            logger.error(f"Failed to start thinking sound: {e}")

    def stop_thinking_sound(self):
        if self._thinking_channel and self._thinking_channel.get_busy():
            fade_out = (
                self.config.thinking_sound_fade_out_ms
                if self.config.thinking_sound_enabled
                else 800
            )
            self._thinking_channel.fadeout(fade_out)
            self._thinking_channel = None
            logger.debug("Thinking sound stopping (fade out)")

    def cleanup(self):
        self.stop_music()

        if self._pygame_ready:
            pygame.mixer.quit()
            self._pygame_ready = False

        self.pya.terminate()
