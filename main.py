# ruff: noqa: E402
import argparse
import asyncio
import logging
import socket
import time

_BOOT_T0 = time.perf_counter()

from src.cli import print_startup_info, setup_logging

setup_logging()
logger = logging.getLogger("gabriel")
_t = time.perf_counter()

# Import tracker FIRST, its module-level code pre-initialises bettercam
# via DXGI Desktop Duplication BEFORE any CUDA library loads.
# If CUDA loads first, DXGI fails on hybrid-GPU systems.
# Importing is safe even when tracker is disabled in config.
from src.tracker import PlayerTracker

# Now pull torch in fully, on the main thread, before anything spins up worker
# threads. A few things load torch lazily from background threads (silero VAD in
# the parakeet STT, scipy via stream2sentence in some TTS providers). Two threads
# importing torch at once can trip "partially initialized module 'torch' has no
# attribute 'Tensor'" circular-import crashes. Doing it here (after the tracker so
# DXGI still wins the CUDA race) means those later imports just grab the finished
# module out of sys.modules instead of racing the init.
try:
    import torch  # noqa: F401
    # Force a única GPU disponível (AMD ROCm expõe via torch.cuda) como
    # dispositivo padrão de processamento antes de qualquer worker subir.
    if torch.cuda.is_available():
        torch.cuda.set_device(0)
        _gpu = torch.cuda.get_device_name(0)
        logger.info(f"GPU padrão selecionada: {_gpu} (cuda:0)")
except Exception:
    pass

from src.audio import AudioManager
from src.config import Config
from src.emotions import get_emotion_system
from src.gemini_live import GeminiLiveSession
from src.memory import memory_system
from src.magic_chatbox import MagicChatbox
from src.monitor import init_monitor
from src.personalities import PersonalityManager
from src.vrchat import VRChatOSC

_BOOT_IMPORT_S = time.perf_counter() - _t

# Suppress the known CPython 3.12 Windows ProactorEventLoop assertion error
# This fires during pipe transport cleanup and is harmless
class _ProactorAssertFilter(logging.Filter):
    def filter(self, record):
        return not (record.name == "asyncio" and "_loop_writing" in str(record.msg))

logging.getLogger("asyncio").addFilter(_ProactorAssertFilter())


def setup_control_server(session, audio, personality, memory, get_emotion_fn, config):
    """Setup the control panel shared state and return a uvicorn Server."""
    try:
        import uvicorn

        from control_server import app, shared_state

        shared_state["session"] = session
        shared_state["audio_mgr"] = audio
        shared_state["personality_mgr"] = personality
        shared_state["memory_mgr"] = memory
        shared_state["get_emotion_fn"] = get_emotion_fn
        shared_state["config"] = config
        shared_state["monitor"] = init_monitor(getattr(config,"_data",{}), port=int(getattr(config,"monitor_port",9090) if hasattr(config,"monitor_port") else 9090))
        logger.info("Starting control panel on http://localhost:8766")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("0.0.0.0", 8766))
        except OSError as exc:
            logger.warning(
                "Control panel port 8766 is already in use; "
                "continuing without starting a second WebUI: %s",
                exc,
            )
            return None
        config = uvicorn.Config(app, host="0.0.0.0", port=8766, log_level="warning")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # Don't override main app's signals
        return server
    except ImportError:
        logger.warning("Control server not available (missing dependencies)")
        return None
    except Exception as e:
        logger.error(f"Control server setup error: {e}")
        return None


def _run_component_watchdog(session, audio, osc, tracker, face_tracker,
                            wanderer, personality, tts_provider, config, logger):
    """Check that critical components are still alive and restart dead ones.

    Runs inside the main event loop, so it never blocks. Each component
    exposes a `healthy()` method or a sentinel attribute we can inspect.
    """
    # Session / Gemini connection
    if session:
        try:
            if not getattr(session, "_session", None):
                logger.warning("Watchdog: Gemini session dead - will reconnect on next run()")
        except Exception:
            pass

    # Audio manager
    if audio:
        try:
            if hasattr(audio, "healthy") and not audio.healthy():
                logger.warning("Watchdog: AudioManager unhealthy - resetting")
                audio.cleanup()
        except Exception:
            pass

    # VRChat OSC (always resilient, but log if completely dead)
    if osc is None and config is not None:
        try:
            from src.vrchat import VRChatOSC
            osc = VRChatOSC(config)
            logger.info("Watchdog: VRChatOSC restarted")
        except Exception as e:
            logger.debug(f"Watchdog: could not restart VRChatOSC: {e}")

    # Face tracker
    if face_tracker:
        try:
            if hasattr(face_tracker, "healthy") and not face_tracker.healthy():
                logger.warning("Watchdog: FaceTracker unhealthy - restarting")
                face_tracker.stop()
                from src.face_tracker import FaceTracker
                face_tracker = FaceTracker(config, osc)
                face_tracker.preload()
                face_tracker.start()
        except Exception as e:
            logger.debug(f"Watchdog: could not restart FaceTracker: {e}")

    # Wanderer
    if wanderer:
        try:
            if hasattr(wanderer, "healthy") and not wanderer.healthy():
                logger.warning("Watchdog: Wanderer unhealthy - restarting")
                wanderer.active = False
                from src.wanderer import Wanderer
                wanderer = Wanderer(config, osc)
                wanderer.preload()
        except Exception as e:
            logger.debug(f"Watchdog: could not restart Wanderer: {e}")

    # TTS provider
    if tts_provider:
        try:
            if hasattr(tts_provider, "healthy") and not tts_provider.healthy():
                logger.warning("Watchdog: TTS provider unhealthy - stopping")
                tts_provider.stop()
        except Exception:
            pass

    # Personality manager
    if personality:
        try:
            if hasattr(personality, "healthy") and not personality.healthy():
                logger.warning("Watchdog: PersonalityManager unhealthy - resetting")
                personality = PersonalityManager()
        except Exception:
            pass

    # Memory system: vacuum SQLite periodically to keep file small
    memory_mgr = None
    try:
        from src.web.shared import shared_state
        memory_mgr = shared_state.get("memory_mgr")
    except Exception:
        pass
    if memory_mgr and hasattr(memory_mgr, "vacuum_sqlite"):
        try:
            memory_mgr.vacuum_sqlite()
        except Exception:
            pass


async def main(save_audio=False):
    _t_main = time.perf_counter()
    loop = asyncio.get_running_loop()
    _orig_handler = loop.get_exception_handler()
    def _suppress_proactor_write_assert(loop, context):
        exc = context.get("exception")
        if isinstance(exc, AssertionError) and "_loop_writing" in str(context.get("handle", "")):
            return
        if _orig_handler:
            _orig_handler(loop, context)
        else:
            loop.default_exception_handler(context)
    loop.set_exception_handler(_suppress_proactor_write_assert)

    config = Config()

    # ── Security / DDoS / brute-force protection ──
    from src.security import security
    security.load_config(config)

    # ── Performance / low-latency tuning ──
    perf = getattr(config, "performance", None) or {}
    torch_threads = int(perf.get("torch_threads", 4) or 4)
    if torch_threads > 0:
        try:
            import torch
            torch.set_num_threads(torch_threads)
            torch.set_num_interop_threads(torch_threads)
        except Exception:
            pass
    # Limit BLAS / OpenMP threads to prevent CPU oversubscription
    for env_var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        import os as _os
        if env_var not in _os.environ:
            _os.environ[env_var] = str(torch_threads)

    # GPU memory fraction for ROCm (reduce VRAM pressure)
    gpu_mem_frac = float(perf.get("gpu_memory_fraction", 0.75) or 0.75)
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.set_per_process_memory_fraction(gpu_mem_frac, device=0)
            # Enable mixed precision by default on ROCm
            if bool(perf.get("mixed_precision", True)):
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
    except Exception:
        pass

    # Apply log level from config now that it's loaded. Default INFO from
    # boot stays unless config.yml has logging.level set.
    from src.cli import apply_log_level
    apply_log_level(config.log_level)

    # Plugin system: discover and load BEFORE the session so any tools the
    # plugins register show up in the very first connect. plugins also get
    # a chance to register external TTS/STT providers we'll pick up below.
    # Done before the banner so the unified banner can show fresh plugin status.
    from src.plugins import PluginManager, get_tts_factory
    plugin_manager = PluginManager(config)
    _t = time.perf_counter()
    from src.cli import Spinner
    # silence plugin chatter while they load so the plugins block in the
    # startup banner lands right under the cyan banner, not after a wall
    # of INFO lines. warnings/errors still come through.
    _root = logging.getLogger()
    _prev_level = _root.level
    _root.setLevel(logging.WARNING)
    try:
        with Spinner("Loading plugins"):
            plugin_manager.discover_and_load()
    finally:
        _root.setLevel(_prev_level)
    _boot_plugins_s = time.perf_counter() - _t

    # Sync the live tool registry into config/tools.yml so any newly added
    # built-in tools or plugin tools show up as togglable. Then reload the
    # in-memory tools cfg so the freshly written entries are visible.
    try:
        from src.tools_sync import sync_tools_yml
        sync_tools_yml(config)
        config.reload_tools_cfg()
    except Exception as e:
        logger.warning(f"tools.yml sync failed (non-fatal): {e}")

    # Banner now that plugins + tools.yml are fresh
    print_startup_info(config)
    logger.info(
        f"[boot] imports {_BOOT_IMPORT_S:.2f}s | plugins {_boot_plugins_s:.2f}s"
        f" | banner ready {time.perf_counter() - _BOOT_T0:.2f}s"
    )

    # Audio manager (may fail without microphone/speaker; degrades to silent mode)
    try:
        audio = AudioManager(config)
    except Exception as e:
        logger.error(f"AudioManager failed to start (silent mode): {e}")
        audio = None

    # VRChat OSC (never crashes the bot if VRChat is closed - _safe_send swallows OSC errors)
    try:
        osc = VRChatOSC(config)
    except Exception as e:
        logger.error(f"VRChatOSC failed to start: {e}")
        osc = None

    # MagicChatbox - floating text above avatar head via OSC (vrcosc-magicchatbox)
    try:
        magic_chatbox = MagicChatbox(osc, config)
        if magic_chatbox.enabled:
            magic_chatbox.set_text(magic_chatbox.text)
            magic_chatbox.set_enabled(True)
            logger.info("MagicChatbox enabled - floating text above avatar")
    except Exception as e:
        logger.error(f"MagicChatbox failed to start: {e}")
        magic_chatbox = None

    tracker = None
    if config.tracker_enabled and osc is not None:
        try:
            tracker = PlayerTracker(config, osc)
            tracker.preload()  # async background model load + warmup
        except Exception as e:
            logger.error(f"PlayerTracker failed to start: {e}")
            tracker = None

    # Face tracker for looking at people (lazy import to skip heavy deps when disabled)
    face_tracker = None
    if config.face_tracker_enabled and osc is not None:
        try:
            from src.face_tracker import FaceTracker
            face_tracker = FaceTracker(config, osc)
            face_tracker.preload()
        except Exception as e:
            logger.error(f"FaceTracker failed to start: {e}")
            face_tracker = None

    # Wanderer for autonomous exploration (lazy import to skip heavy deps when disabled)
    wanderer = None
    if config.wanderer_enabled and osc is not None:
        try:
            from src.wanderer import Wanderer
            wanderer = Wanderer(config, osc)
            wanderer.preload()
        except Exception as e:
            logger.error(f"Wanderer failed to start: {e}")
            wanderer = None

    try:
        personality = PersonalityManager()
    except Exception as e:
        logger.error(f"PersonalityManager failed to start: {e}")
        personality = None

    # External TTS provider (optional - when tts.provider != "gemini")
    tts_provider = None
    if config.tts_hoppou_enabled:
        try:
            from src.tts import HoppouTTSProvider
            tts_provider = HoppouTTSProvider(config)
            tts_provider.start()
            logger.info("Using Hoppou TTS provider (Gemini audio will be discarded)")
        except Exception as e:
            logger.error(f"Hoppou TTS provider failed to start: {e}")
    elif config.tts_chirp3_hd_enabled:
        try:
            from src.tts import Chirp3HDTTSProvider
            tts_provider = Chirp3HDTTSProvider(config)
            tts_provider.start()
            logger.info("Using Chirp 3 HD TTS provider (Gemini audio will be discarded)")
        except Exception as e:
            logger.error(f"Chirp 3 HD TTS provider failed to start: {e}")
    elif config.tts_tiktok_enabled:
        try:
            from src.tts import TikTokTTSProvider
            tts_provider = TikTokTTSProvider(config)
            tts_provider.start()
            logger.info("Using TikTok TTS provider (Gemini audio will be discarded)")
        except Exception as e:
            logger.error(f"TikTok TTS provider failed to start: {e}")
    else:
        # plugin provider. set tts.provider to the name directly, old external_provider still works as fallback
        ext_name = config.tts_provider
        if ext_name in ("gemini", "", None):
            ext_name = config.get("tts", "external_provider", default=None)
        if ext_name:
            factory = get_tts_factory(ext_name)
            if factory is None:
                logger.warning(
                    f"tts provider '{ext_name}' is not registered by any plugin"
                )
            else:
                try:
                    tts_provider = factory(config)
                    if hasattr(tts_provider, "start"):
                        tts_provider.start()
                    logger.info(f"Using plugin TTS provider '{ext_name}' (Gemini audio will be discarded)")
                except Exception as e:
                    logger.error(f"plugin TTS '{ext_name}' failed to start: {e}")
                    tts_provider = None

    if config.backend == "local":
        if tts_provider is None:
            logger.error(
                "local backend selected but no TTS provider is enabled. "
                "enable one of tts.hoppou / tts.chirp3_hd / tts.tiktok, "
                "or set tts.provider to a plugin-supplied name."
            )
            raise SystemExit(2)
        from src.local_live import LocalLiveSession
        logger.info("backend = local (LM Studio + parakeet.cpp)")
        session = LocalLiveSession(config, audio, osc, tracker, personality, tts_provider)
    else:
        logger.info("backend = gemini_live")
        session = GeminiLiveSession(config, audio, osc, tracker, personality, tts_provider)
    session._save_audio = save_audio

    # Instance monitor for player list (VRChat log parsing) - never crashes the bot
    try:
        from src.instance_monitor import InstanceMonitor
        instance_monitor = InstanceMonitor()
        instance_monitor.start()
        session.tool_handler.instance_monitor = instance_monitor
    except Exception as e:
        logger.error(f"InstanceMonitor failed to start: {e}")
        instance_monitor = None

    # VRChat API for avatar switching (background login)
    vrchat_api = None
    if config.vrchat_api_username:
        try:
            from src.vrchatapi import VRChatAPI
            vrchat_api = VRChatAPI(config)
            session.tool_handler.vrchat_api = vrchat_api

            async def _bg_login():
                try:
                    ok = await vrchat_api.ensure_logged_in()
                    if ok:
                        logger.info("VRChat API authenticated successfully")
                        if not VRChatAPI.friends_cache_fresh():
                            logger.info("Friends cache is stale (>4h) or missing, fetching...")
                            await vrchat_api.fetch_and_cache_friends()
                        else:
                            logger.info("Friends cache is fresh, skipping fetch")
                    else:
                        logger.warning("VRChat API login failed -- avatar switching may not work")
                except Exception as e:
                    logger.error(f"VRChat API background login error: {e}")

            asyncio.create_task(_bg_login())
        except Exception as e:
            logger.error(f"VRChatAPI failed to start: {e}")

    # Wire wanderer into tool handler and session
    if wanderer:
        session.tool_handler.wanderer = wanderer
        session._wanderer = wanderer
        if face_tracker:
            wanderer._face_tracker_ref = face_tracker
        wanderer._emotion_system_ref = get_emotion_system()

    # Wire magic_chatbox into session and tool handler
    if magic_chatbox:
        session.tool_handler.magic_chatbox = magic_chatbox
        session._magic_chatbox = magic_chatbox

    # Wire face tracker speaking callback and start
    if face_tracker:
        face_tracker.set_speaking_callback(lambda: session._speaking)
        face_tracker.set_idle_callback(lambda: session._is_idle)
        if tracker:
            face_tracker.set_player_tracker(tracker)
        if wanderer:
            face_tracker.set_wanderer(wanderer)
        face_tracker.start()

    # Start control panel as async task in same event loop
    control_server = setup_control_server(session, audio, personality, memory_system, get_emotion_system, config)
    if control_server:
        try:
            from control_server import shared_state
            shared_state["instance_monitor"] = instance_monitor
            shared_state["tracker"] = tracker
            # tracker pushes annotated frames into vision_server module state whenever
            # _vision_debug is on. webui vision tab pulls from that same buffer, so just
            # flip it on whenever the WebUI is running.
            if tracker:
                tracker._vision_debug = True
            # mapping + waypoints service (lazy, nothing runs til UI starts it)
            try:
                from src.mapping_service import MappingService
                shared_state["mapping_service"] = MappingService(
                    osc, instance_monitor=instance_monitor,
                )
                session.tool_handler.mapping_service = shared_state["mapping_service"]
                # face tracker yields LookHorizontal while voxel nav drives,
                # otherwise it fights the path steering + final-yaw aligner
                if face_tracker:
                    face_tracker.set_mapping_service(shared_state["mapping_service"])
                # let the wanderer use the voxel map for curiosity exploration
                if wanderer:
                    wanderer._mapping_service_ref = shared_state["mapping_service"]
                # idle chatbox shows live mapping stats when its running
                try:
                    if getattr(session, "_idle_chatbox", None):
                        session._idle_chatbox.set_mapping_service(shared_state["mapping_service"])
                except Exception:
                    pass
            except Exception as _e:
                logger.warning(f"mapping service unavailable: {_e}")
        except ImportError:
            pass
        asyncio.create_task(control_server.serve())

        # Expose magic_chatbox to the WebUI shared state
        try:
            from control_server import shared_state
            shared_state["magic_chatbox"] = magic_chatbox
        except Exception:
            pass

    # Discord selfbot (optional)
    discord_bot = None
    if config.discord_bot_enabled:
        from discord_bot.bot import DiscordBot
        from discord_bot.config import BotConfig

        async def _relay_to_main(text):
            """Relay callback: send Discord activity to main Gemini session."""
            await session.send_text(text)

        try:
            bot_config = BotConfig()
            discord_bot = DiscordBot(config=bot_config, relay_callback=_relay_to_main,
                                     instance_monitor=instance_monitor)
            session.tool_handler.discord_bot = discord_bot
            asyncio.create_task(discord_bot.start())
            logger.info("Discord selfbot starting...")
        except Exception as e:
            logger.error(f"Discord bot startup failed: {e}")

    # Lyria RealTime music generation (optional)
    if config.music_gen_enabled:
        try:
            from src.music_gen import MusicGenerator
            music_gen = MusicGenerator(config, audio)
            session.tool_handler.music_gen = music_gen
            logger.info("Lyria RealTime music generation enabled")
        except Exception as e:
            logger.error(f"MusicGenerator failed to start: {e}")

    # Social server (AI-to-AI messaging, optional)
    social_client = None
    if config.social_enabled:
        try:
            from src.social import SocialClient
            social_client = SocialClient(config)
            social_client.set_session(session)
            session.tool_handler.social_client = social_client
            asyncio.create_task(social_client.start())
            logger.info("Social client starting...")
        except Exception as e:
            logger.error(f"Social client failed to start: {e}")

    # Plugins: bind app refs and fire the startup event now that everything is wired
    plugin_manager.bind_app(audio=audio, osc=osc, session=session,
                            tool_handler=session.tool_handler, config=config)
    plugin_manager.emit("startup")

    logger.info(f"[boot] main() ready in {time.perf_counter() - _t_main:.2f}s, total boot {time.perf_counter() - _BOOT_T0:.2f}s")


    # 24/7 run loop with exponential backoff restart on session crash,
    # plus a component watchdog that restarts dead workers without
    # restarting the whole bot.
    _restart_delay = 1.0
    _max_restart_delay = 60.0
    _watchdog_ticks = 0
    _watchdog_interval = 30  # check every 30s (~30 session.run iterations)
    _last_healthy = time.monotonic()

    while True:
        try:
            await session.run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
            break
        except SystemExit:
            raise
        except Exception as e:
            logger.error(f"Session crashed: {e}")
            _restart_delay = min(_restart_delay * 2, _max_restart_delay)
            logger.info(f"Restarting session in {_restart_delay:.0f} seconds...")
            await asyncio.sleep(_restart_delay)
            continue

        # Watchdog: run component health checks periodically.
        _watchdog_ticks += 1
        if _watchdog_ticks >= _watchdog_interval:
            _watchdog_ticks = 0
            try:
                _run_component_watchdog(session, audio, osc, tracker,
                                        face_tracker, wanderer, personality,
                                        tts_provider, config, logger)
                _last_healthy = time.monotonic()
            except Exception as e:
                logger.warning(f"Component watchdog check failed: {e}")

        # If no successful run for too long, force a full restart.
        if time.monotonic() - _last_healthy > 300:
            logger.warning("No healthy session run for 300s - forcing restart")
            _restart_delay = min(_restart_delay * 2, _max_restart_delay)
            await asyncio.sleep(_restart_delay)

    # Cleanup
    plugin_manager.emit("shutdown")
    await plugin_manager.teardown_all()
    if save_audio and session:
        session.save_audio_to_wav()
    if config.music_gen_enabled and session and session.tool_handler.music_gen:
        await session.tool_handler.music_gen.stop()
    if social_client:
        await social_client.stop()
    if discord_bot:
        await discord_bot.stop()
    if tts_provider:
        tts_provider.stop()
    if face_tracker:
        face_tracker.stop()
    if tracker:
        tracker.active = False
    emotion = get_emotion_system()
    if emotion:
        emotion.stop()
    if audio:
        audio.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ProjectGabriel - VRChat AI")
    parser.add_argument("--save-audio", action="store_true",
                        help="Save Gemini voice output to a .wav file on exit")
    args = parser.parse_args()
    try:
        asyncio.run(main(save_audio=args.save_audio))
    except KeyboardInterrupt:
        pass
