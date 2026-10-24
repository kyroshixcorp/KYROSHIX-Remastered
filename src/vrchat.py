import asyncio
import logging
import math
import queue
import threading
import time
from collections import deque

from pythonosc import udp_client
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

from src.raycast import RaycastState
from src.security import security

logger = logging.getLogger(__name__)

CHATBOX_CHAR_LIMIT = 144
CHATBOX_RATE_LIMIT = 1.27  # Legacy flat chatbox rate limit in seconds
CHATBOX_BUCKET_CAPACITY = 5
CHATBOX_BUCKET_WINDOW_SECONDS = 5.0
CHATBOX_BUCKET_SAFETY_MARGIN_SECONDS = 0.1
AVATAR_EYE_HEIGHT_MIN_M = 0.1
AVATAR_EYE_HEIGHT_MAX_M = 100.0

try:
    from pynput.keyboard import Controller as KeyboardController

    _keyboard = KeyboardController()
except Exception as exc:
    _keyboard = None
    logger.warning("Keyboard control unavailable; crouch and crawl require a desktop keyboard backend: %s", exc)


class VRChatOSC:
    def __init__(self, config):
        self.config = config
        self.client = udp_client.SimpleUDPClient(config.osc_ip, config.osc_port)
        self._osc_failures = 0
        self.last_osc_error = None
        self._typing = False
        self._load_chatbox_rate_limit_config()
        self._last_chatbox_time = 0.0
        self._chatbox_sent_times = deque()
        self._chatbox_queue = queue.Queue()
        self._chatbox_thread = threading.Thread(target=self._chatbox_worker, daemon=True)
        self._chatbox_thread.start()

        # Avatar velocity from VRChat OSC output (read by wanderer for stuck detection)
        self.velocity_x = 0.0
        self.velocity_y = 0.0
        self.velocity_z = 0.0
        self.grounded = True
        self.seated = False
        self.velocity_received = False  # True once any velocity update arrives

        # Avatar scaling state from VRChat OSC output
        self.avatar_eye_height_meters = None
        self.avatar_eye_height_min_meters = None
        self.avatar_eye_height_max_meters = None
        self.avatar_eye_height_scaling_allowed = None
        self.last_requested_avatar_eye_height_meters = None

        # Raycast state, fed by the catch-all avatar param handler. Strips
        # VRCFury's VF<id>_ prefix by default so consumers see clean names.
        self.raycast_state = RaycastState()

        # Start OSC listener for avatar parameters
        self._start_osc_listener(config)

    def _load_chatbox_rate_limit_config(self):
        self._chatbox_rate_limiter_enabled = self._coerce_bool(
            getattr(self.config, "chatbox_rate_limiter_enabled", True), True
        )
        self._chatbox_bucket_capacity = self._coerce_int(
            getattr(self.config, "chatbox_rate_limit_capacity", CHATBOX_BUCKET_CAPACITY),
            CHATBOX_BUCKET_CAPACITY,
            minimum=1,
        )
        self._chatbox_bucket_window_seconds = self._coerce_float(
            getattr(self.config, "chatbox_rate_limit_window_seconds", CHATBOX_BUCKET_WINDOW_SECONDS),
            CHATBOX_BUCKET_WINDOW_SECONDS,
            minimum=0.1,
        )
        self._chatbox_bucket_safety_margin_seconds = self._coerce_float(
            getattr(self.config, "chatbox_rate_limit_safety_margin_seconds", CHATBOX_BUCKET_SAFETY_MARGIN_SECONDS),
            CHATBOX_BUCKET_SAFETY_MARGIN_SECONDS,
            minimum=0.0,
        )
        self._chatbox_legacy_rate_limit_seconds = self._coerce_float(
            getattr(self.config, "chatbox_legacy_rate_limit_seconds", CHATBOX_RATE_LIMIT),
            CHATBOX_RATE_LIMIT,
            minimum=0.0,
        )
        if self._chatbox_rate_limiter_enabled:
            logger.info(
                "Chatbox leaky bucket limiter enabled: %s messages per %.2fs",
                self._chatbox_bucket_capacity,
                self._chatbox_bucket_window_seconds,
            )
        else:
            logger.info(
                "Chatbox leaky bucket limiter disabled, legacy %.2fs spacing is active",
                self._chatbox_legacy_rate_limit_seconds,
            )

    @staticmethod
    def _coerce_bool(value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _coerce_int(value, default: int, minimum: int | None = None) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        if minimum is not None:
            parsed = max(minimum, parsed)
        return parsed

    @staticmethod
    def _coerce_float(value, default: float, minimum: float | None = None) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        if not math.isfinite(parsed):
            parsed = default
        if minimum is not None:
            parsed = max(minimum, parsed)
        return parsed

    def _safe_send(self, address: str, *args) -> bool:
        """Envia uma mensagem OSC de forma segura: nunca levanta exceção para
        cima, então o bot continua rodando mesmo sem o VRChat aberto (UDP é
        fire-and-forget; falhas são apenas registradas e contabilizadas)."""
        try:
            self._safe_send(address, *args)
            self._osc_failures = 0
            self.last_osc_error = None
            return True
        except Exception as exc:  # noqa: BLE001
            self._osc_failures += 1
            self.last_osc_error = exc
            if self._osc_failures <= 3 or self._osc_failures % 50 == 0:
                logger.debug("OSC send failed (%s): %s", address, exc)
            return False

    @property
    def vrchat_available(self) -> bool:
        """Indica se o último envio OSC teve sucesso (heurística para UI/logs)."""
        return self._osc_failures == 0 or self.last_osc_error is None

    def _start_osc_listener(self, config):
        """Start background OSC server to receive avatar parameters from VRChat."""
        receive_port = getattr(config, "osc_receive_port", 9001)
        dispatcher = Dispatcher()
        dispatcher.map("/avatar/parameters/VelocityZ", self._on_velocity_z)
        dispatcher.map("/avatar/parameters/VelocityX", self._on_velocity_x)
        dispatcher.map("/avatar/parameters/VelocityY", self._on_velocity_y)
        dispatcher.map("/avatar/parameters/Grounded", self._on_grounded)
        dispatcher.map("/avatar/parameters/Seated", self._on_seated)
        dispatcher.map("/avatar/eyeheight", self._on_avatar_eye_height)
        dispatcher.map("/avatar/eyeheightmin", self._on_avatar_eye_height_min)
        dispatcher.map("/avatar/eyeheightmax", self._on_avatar_eye_height_max)
        dispatcher.map("/avatar/eyeheightscalingallowed", self._on_avatar_eye_height_scaling_allowed)
        # catch-all for anything else under /avatar/parameters/, used to feed
        # the raycast state (and any future param sniffers)
        dispatcher.set_default_handler(self._on_unmapped_osc, needs_reply_address=False)

        try:
            server = ThreadingOSCUDPServer(("127.0.0.1", receive_port), dispatcher)
            thread = threading.Thread(target=server.serve_forever, daemon=True, name="osc-listener")
            thread.start()
            logger.info(f"OSC listener started on port {receive_port}")
        except OSError as e:
            logger.warning(f"OSC listener failed to start on port {receive_port}: {e}")

    def _on_velocity_z(self, address, value):
        self.velocity_z = float(value)
        self.velocity_received = True

    def _on_velocity_x(self, address, value):
        self.velocity_x = float(value)
        self.velocity_received = True

    def _on_velocity_y(self, address, value):
        self.velocity_y = float(value)

    def _on_grounded(self, address, value):
        self.grounded = bool(value)

    def _on_seated(self, address, value):
        self.seated = bool(value)

    def _on_avatar_eye_height(self, address, value):
        self.avatar_eye_height_meters = float(value)

    def _on_avatar_eye_height_min(self, address, value):
        self.avatar_eye_height_min_meters = float(value)

    def _on_avatar_eye_height_max(self, address, value):
        self.avatar_eye_height_max_meters = float(value)

    def _on_avatar_eye_height_scaling_allowed(self, address, value):
        self.avatar_eye_height_scaling_allowed = bool(value)

    def _on_unmapped_osc(self, address, *args):
        """Catch-all for OSC addresses without an explicit handler. Forwards
        avatar params to the raycast state so VRCRaycast outputs are picked
        up automatically (the prefix scrubber inside RaycastState handles
        VRCFury's VF<id>_ namespacing).

        Includes OSC flood guard: rejects messages if the source IP exceeds
        the configured OSC messages-per-second limit.
        """
        # OSC flood guard (messages come from 127.0.0.1 in local mode)
        osc_ip = "127.0.0.1"
        allowed, reason = security.check_osc(osc_ip, address)
        if not allowed:
            logger.debug("OSC flood guard blocked %s from %s: %s", address, osc_ip, reason)
            return

        if not args or not address.startswith("/avatar/parameters/"):
            return
        param = address[len("/avatar/parameters/"):]
        self.raycast_state.update(param, args[0])

    def _chatbox_worker(self):
        """Background thread that sends chatbox messages respecting VRChat's rate limit."""
        while True:
            try:
                text = self._chatbox_queue.get()
                self._wait_for_chatbox_slot()
                self._safe_send("/chatbox/input", [text, True, False])
                self._record_chatbox_send()

            except Exception as e:
                logger.error(f"Chatbox worker error: {e}")

    def _wait_for_chatbox_slot(self):
        if not self._chatbox_rate_limiter_enabled:
            self._wait_for_legacy_chatbox_slot()
            return

        while True:
            now = time.monotonic()
            self._prune_chatbox_bucket(now)
            if len(self._chatbox_sent_times) < self._chatbox_bucket_capacity:
                return

            oldest_send = self._chatbox_sent_times[0]
            wait_seconds = (
                oldest_send
                + self._chatbox_bucket_window_seconds
                + self._chatbox_bucket_safety_margin_seconds
                - now
            )
            if wait_seconds <= 0:
                continue
            time.sleep(wait_seconds)

    def _wait_for_legacy_chatbox_slot(self):
        if self._chatbox_legacy_rate_limit_seconds <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_chatbox_time
        if elapsed < self._chatbox_legacy_rate_limit_seconds:
            time.sleep(self._chatbox_legacy_rate_limit_seconds - elapsed)

    def _prune_chatbox_bucket(self, now: float):
        cutoff = now - self._chatbox_bucket_window_seconds
        while self._chatbox_sent_times and self._chatbox_sent_times[0] <= cutoff:
            self._chatbox_sent_times.popleft()

    def _record_chatbox_send(self):
        now = time.monotonic()
        self._last_chatbox_time = now
        if self._chatbox_rate_limiter_enabled:
            self._chatbox_sent_times.append(now)

    def set_typing(self, typing: bool):
        if self._typing != typing:
            self._typing = typing
            self._safe_send("/chatbox/typing", typing)

    def send_chatbox(self, text: str):
        """Queue a chatbox message (non-blocking, sent by background thread)."""
        # Clear any pending messages and use the latest text
        try:
            while True:
                self._chatbox_queue.get_nowait()
        except queue.Empty:
            pass
        self._chatbox_queue.put(text)

    def send_chatbox_paginated(self, text: str) -> list[str]:
        if len(text) <= CHATBOX_CHAR_LIMIT:
            self.send_chatbox(text)
            return [text]
        pages = self._paginate(text)
        if pages:
            self.send_chatbox(pages[0])
        return pages

    def _paginate(self, text: str) -> list[str]:
        words = text.split()
        if not words:
            return [text[:CHATBOX_CHAR_LIMIT]]

        indicator_reserve = len(" (99/99)")
        usable = CHATBOX_CHAR_LIMIT - indicator_reserve

        chunks = []
        current = ""
        for word in words:
            test = f"{current} {word}".strip() if current else word
            if len(test) > usable:
                if current:
                    chunks.append(current)
                    current = word
                else:
                    chunks.append(word[:usable])
                    current = ""
            else:
                current = test
        if current:
            chunks.append(current)

        total = len(chunks)
        return [f"{c} ({i + 1}/{total})" for i, c in enumerate(chunks)]

    def send_chatbox_direct(self, text: str):
        """Send chatbox message directly via the queue (for paginated display)."""
        self._chatbox_queue.put(text)

    async def display_pages(self, pages: list[str], delay: float = 3.0):
        try:
            actual_delay = max(0.0, float(delay))
        except (TypeError, ValueError):
            actual_delay = 3.0
        for page in pages:
            self.send_chatbox_direct(page)
            self.set_typing(True)
            await asyncio.sleep(actual_delay)

    def toggle_voice(self):
        self._safe_send("/input/Voice", 1)
        time.sleep(0.05)
        self._safe_send("/input/Voice", 0)

    @staticmethod
    def clamp_avatar_eye_height(height_meters: float) -> float:
        requested = float(height_meters)
        if not math.isfinite(requested):
            raise ValueError("Avatar eye height must be a finite number")
        return max(AVATAR_EYE_HEIGHT_MIN_M, min(AVATAR_EYE_HEIGHT_MAX_M, requested))

    def set_avatar_eye_height(self, height_meters: float) -> dict:
        """Set avatar eye height in meters using VRChat OSC avatar scaling."""
        requested = float(height_meters)
        clamped = self.clamp_avatar_eye_height(requested)
        self.last_requested_avatar_eye_height_meters = clamped
        self._safe_send("/avatar/eyeheight", clamped)
        logger.info(f"Set avatar eye height to {clamped:.3f}m")
        return {
            "requested_height_meters": requested,
            "sent_height_meters": clamped,
            "clamped": clamped != requested,
            "supported_min_meters": AVATAR_EYE_HEIGHT_MIN_M,
            "supported_max_meters": AVATAR_EYE_HEIGHT_MAX_M,
        }

    def get_avatar_scaling_status(self) -> dict:
        """Return the latest avatar scaling values observed from VRChat OSC."""
        return {
            "eye_height_meters": self.avatar_eye_height_meters,
            "eye_height_min_meters": self.avatar_eye_height_min_meters,
            "eye_height_max_meters": self.avatar_eye_height_max_meters,
            "scaling_allowed": self.avatar_eye_height_scaling_allowed,
            "last_requested_height_meters": self.last_requested_avatar_eye_height_meters,
            "supported_min_meters": AVATAR_EYE_HEIGHT_MIN_M,
            "supported_max_meters": AVATAR_EYE_HEIGHT_MAX_M,
        }

    def set_movement(self, forward: float = 0.0, horizontal: float = 0.0):
        """Set movement axes (float -1 to 1).

        forward: /input/Vertical  (-1 back, 1 forward)
        horizontal: /input/Horizontal (-1 strafe left, 1 strafe right)

        Reset to 0 when done. Use set_look() for camera/head rotation.
        """
        self._safe_send("/input/Vertical", max(-1.0, min(1.0, forward)))
        self._safe_send("/input/Horizontal", max(-1.0, min(1.0, horizontal)))

    def set_look(self, look_h: float = 0.0, look_v: float = 0.0):
        """Set camera/head rotation axes (float -1 to 1).

        look_h: /input/LookHorizontal (-1 left, 1 right) -- rotates the view
        look_v: /input/LookVertical (-1 down, 1 up)

        Reset to 0 when done. Use set_movement() for walking/strafing.
        """
        self._safe_send("/input/LookHorizontal", max(-1.0, min(1.0, look_h)))
        self._safe_send("/input/LookVertical", max(-1.0, min(1.0, look_v)))

    def stop_movement(self):
        self._safe_send("/input/Vertical", 0.0)
        self._safe_send("/input/Horizontal", 0.0)
        self._safe_send("/input/LookHorizontal", 0.0)
        self._safe_send("/input/LookVertical", 0.0)

    def toggle_crouch(self):
        """Toggle crouch in VRChat by pressing C key."""
        if _keyboard is None:
            return
        _keyboard.press('c')
        time.sleep(0.05)
        _keyboard.release('c')
        logger.info("Toggled crouch (C key)")

    def toggle_crawl(self):
        """Toggle crawl/prone in VRChat by pressing Z key."""
        if _keyboard is None:
            return
        _keyboard.press('z')
        time.sleep(0.05)
        _keyboard.release('z')
        logger.info("Toggled crawl (Z key)")

    # Manual movement methods for AI control
    def start_move(self, direction: str, speed: str = "normal"):
        """Start moving in a direction with speed control.

        direction: forward, backward, left (strafe), right (strafe)
        speed: 'slow' (0.5), 'normal' (0.8), 'fast' (1.0), 'sprint' (1.0 + Run)
        """
        speed_map = {"slow": 0.5, "normal": 0.8, "fast": 1.0, "sprint": 1.0}
        axis_val = speed_map.get(speed, 0.8)
        sprinting = speed == "sprint"

        if direction == "forward":
            self._safe_send("/input/Vertical", axis_val)
        elif direction == "backward":
            self._safe_send("/input/Vertical", -axis_val)
        elif direction == "left":
            self._safe_send("/input/Horizontal", -axis_val)
        elif direction == "right":
            self._safe_send("/input/Horizontal", axis_val)

        self._safe_send("/input/Run", 1 if sprinting else 0)
        logger.info(f"Started moving {direction} (speed={speed}, axis={axis_val}, sprint={sprinting})")

    def stop_all_movement(self):
        """Reset all movement axes and sprint to zero."""
        self._safe_send("/input/Vertical", 0.0)
        self._safe_send("/input/Horizontal", 0.0)
        self._safe_send("/input/LookHorizontal", 0.0)
        self._safe_send("/input/Run", 0)
        logger.info("Stopped all movement")

    def jump(self):
        """Make the avatar jump."""
        self._safe_send("/input/Jump", 1)
        time.sleep(0.05)
        self._safe_send("/input/Jump", 0)
        logger.info("Jumped")

    def look(self, direction: str, duration: float, speed: str = "normal"):
        """Smooth turn left or right, ramping up/down like the tracker system.

        speed: 'slow' (0.3), 'normal' (0.6), 'fast' (1.0) - max axis value
        """
        speed_map = {"slow": 0.6, "normal": 0.8, "fast": 1.0}
        target = speed_map.get(speed, 0.6)
        if direction == "left":
            target = -target

        step_interval = 1.0 / 30  # ~30 updates per second like the tracker
        alpha = 0.4  # EMA smoothing factor (same as tracker default)
        max_rate = 0.12  # Max change per step (same as tracker)
        current = 0.0

        elapsed = 0.0
        # Ramp up and hold
        while elapsed < duration:
            new_val = current * (1 - alpha) + target * alpha
            delta = new_val - current
            if abs(delta) > max_rate:
                new_val = current + max_rate * (1 if delta > 0 else -1)
            current = max(-1.0, min(1.0, new_val))
            self._safe_send("/input/LookHorizontal", float(current))
            time.sleep(step_interval)
            elapsed += step_interval

        # Ramp down smoothly
        while abs(current) > 0.01:
            new_val = current * (1 - alpha)
            delta = new_val - current
            if abs(delta) > max_rate:
                new_val = current + max_rate * (1 if delta > 0 else -1)
            current = max(-1.0, min(1.0, new_val))
            self._safe_send("/input/LookHorizontal", float(current))
            time.sleep(step_interval)

        self._safe_send("/input/LookHorizontal", 0.0)
        logger.info(f"Looked {direction} for {duration}s (speed={speed})")

    def look_vertical(self, direction: str, duration: float, speed: str = "normal"):
        """Smooth look up or down, same EMA ramp as horizontal look."""
        speed_map = {"slow": 0.6, "normal": 0.8, "fast": 1.0}
        target = speed_map.get(speed, 0.6)
        if direction == "down":
            target = -target

        step_interval = 1.0 / 30
        alpha = 0.4
        max_rate = 0.12
        current = 0.0

        elapsed = 0.0
        while elapsed < duration:
            new_val = current * (1 - alpha) + target * alpha
            delta = new_val - current
            if abs(delta) > max_rate:
                new_val = current + max_rate * (1 if delta > 0 else -1)
            current = max(-1.0, min(1.0, new_val))
            self._safe_send("/input/LookVertical", float(current))
            time.sleep(step_interval)
            elapsed += step_interval

        while abs(current) > 0.01:
            new_val = current * (1 - alpha)
            delta = new_val - current
            if abs(delta) > max_rate:
                new_val = current + max_rate * (1 if delta > 0 else -1)
            current = max(-1.0, min(1.0, new_val))
            self._safe_send("/input/LookVertical", float(current))
            time.sleep(step_interval)

        self._safe_send("/input/LookVertical", 0.0)
        logger.info(f"Looked {direction} for {duration}s (speed={speed})")

    def grab(self):
        """Grab and hold the item highlighted in front of you. Stays held until drop() is called."""
        self._safe_send("/input/GrabRight", 1)
        logger.info("Grabbing item (GrabRight held)")

    def drop(self):
        """Release the item currently held in right hand."""
        self._safe_send("/input/GrabRight", 0)
        logger.info("Dropped item (GrabRight released)")

    def use(self):
        """Use/interact with the item highlighted in front of you using right hand."""
        self._safe_send("/input/UseRight", 1)
        time.sleep(0.15)
        self._safe_send("/input/UseRight", 0)
        logger.info("Used item (UseRight)")
