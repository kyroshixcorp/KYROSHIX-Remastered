"""Start / stop / world-swap + the tick loop."""

from __future__ import annotations

import logging
import threading
import time

from src.pose_decoder import DEFAULT_CELL_SIZE, GRID_W, GRID_H, PoseExfilReader
from src.voxel_explorer import VoxelExplorer

from ._base import _RegionGuess

logger = logging.getLogger(__name__)


class LifecycleMixin:
    """start/stop, explore toggle, world-change handler, the tick thread."""

    def start(self, *, explore: bool = False) -> dict:
        """Find the PoseExfil strip, load the world, and start mapping.

        The screen scanner is intentionally retried because MSS captures the
        visible desktop. On a 4K desktop, the control panel, PowerShell, or
        another window may temporarily cover the PoseStrip when mapping is
        enabled. A single failed frame must not permanently abort startup.
        """
        with self._lock:
            if self._running:
                # Allow toggling exploration without a full restart.
                self._set_explore(explore)
                return self.get_state()

            world = self._resolve_world_id()
            self._world_id = world
            self._world_name = self._resolve_world_name()
            self._nav.load_world(world)
            self._ensure_waypoints(world)

            # Import lazily so normal bot startup does not pay the scanner
            # import cost unless mapping is actually requested.
            try:
                from scripts.test_pose_decoder_live import scan_and_decode
            except Exception as exc:
                self._last_error = f"scan import failed: {exc}"
                logger.exception("mapping: scan import failed")
                return self.get_state()

            # The shader currently uses 4 physical pixels per cell. Prefer the
            # decoder's canonical value so this stays synchronized if the
            # project default changes later.
            preferred_cell = max(2, int(DEFAULT_CELL_SIZE))

            # 4K-safe startup strategy:
            #   * several short attempts instead of one expensive/fatal scan
            #   * first successful full decode wins
            #   * no hard-coded screen coordinates or resolution
            #   * gives the user time to bring VRChat to the foreground
            scan_attempts = 12
            scan_retry_delay = 0.75
            result = None
            last_scan_error = None

            logger.info(
                "mapping: locating PoseStrip "
                "(cell=%d, attempts=%d, retry=%.2fs)",
                preferred_cell,
                scan_attempts,
                scan_retry_delay,
            )

            for attempt in range(1, scan_attempts + 1):
                if self._stop_evt.is_set():
                    self._last_error = "mapping startup cancelled"
                    return self.get_state()

                try:
                    candidate = scan_and_decode(preferred_cell)

                    if (
                        isinstance(candidate, tuple)
                        and len(candidate) >= 5
                        and candidate[0] == 0
                    ):
                        result = candidate
                        logger.info(
                            "mapping: PoseStrip found on attempt %d/%d",
                            attempt,
                            scan_attempts,
                        )
                        break

                except Exception as exc:
                    last_scan_error = exc
                    logger.warning(
                        "mapping: PoseStrip scan attempt %d/%d crashed: %s",
                        attempt,
                        scan_attempts,
                        exc,
                    )

                if attempt < scan_attempts:
                    logger.info(
                        "mapping: PoseStrip not visible yet (%d/%d); "
                        "keep VRChat visible",
                        attempt,
                        scan_attempts,
                    )
                    time.sleep(scan_retry_delay)

            if result is None:
                if last_scan_error is not None:
                    self._last_error = (
                        "could not locate PoseStrip after "
                        f"{scan_attempts} attempts; last scanner error: "
                        f"{last_scan_error}"
                    )
                else:
                    self._last_error = (
                        "could not locate PoseStrip after "
                        f"{scan_attempts} attempts. Keep VRChat visible "
                        "with the PoseExfil shader enabled."
                    )

                logger.warning("mapping: %s", self._last_error)
                return self.get_state()

            _, mi, ax, ay, cell = result

            try:
                mi = int(mi)
                ax = int(ax)
                ay = int(ay)
                cell = int(cell)
            except (TypeError, ValueError) as exc:
                self._last_error = f"invalid PoseStrip scan result: {result!r}"
                logger.warning("mapping: %s (%s)", self._last_error, exc)
                return self.get_state()

            if mi <= 0 or ax < 0 or ay < 0 or cell <= 0:
                self._last_error = f"invalid PoseStrip geometry: {result!r}"
                logger.warning("mapping: %s", self._last_error)
                return self.get_state()

            self._region = _RegionGuess(mi, ax, ay, cell)

            region = {
                "left": ax,
                "top": ay,
                "width": GRID_W * cell,
                "height": GRID_H * cell,
            }

            logger.info(
                "mapping: PoseStrip locked "
                "(monitor=%d left=%d top=%d cell=%d region=%dx%d)",
                mi,
                ax,
                ay,
                cell,
                region["width"],
                region["height"],
            )

            # Reader only captures the tiny 34x2 strip after discovery:
            # at 4 px/cell this is just 136x8 pixels, so polling remains cheap
            # even when the desktop itself is 3840x2160.
            try:
                self._reader = PoseExfilReader(
                    region=region,
                    cell_size=cell,
                    poll_hz=20.0,
                    monitor_index=mi,
                )
                self._reader.start()
            except Exception as exc:
                self._reader = None
                self._last_error = f"PoseStrip reader failed to start: {exc}"
                logger.exception("mapping: reader start failed")
                return self.get_state()

            try:
                if explore:
                    self._explorer = VoxelExplorer(
                        self._nav,
                        self._osc,
                        learning_mode=True,
                    )
                    self._explorer.force_run = self._force_run
                    self._explorer.speed_mode = self._speed_mode
                    self._explorer.start()
                    self._explore_enabled = True
                else:
                    self._explorer = None
                    self._explore_enabled = False

                self._explorer_follow_only = False
                self._stop_evt.clear()

                self._tick_thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name="mapping-tick",
                )
                self._tick_thread.start()

                self._running = True
                self._last_error = ""

                logger.info(
                    "mapping: started "
                    "(world=%s explore=%s tick_hz=%.1f pose_hz=20.0)",
                    world,
                    explore,
                    float(self._tick_hz),
                )

                return self.get_state()

            except Exception as exc:
                # Do not leave a live reader or movement input behind if
                # explorer/tick startup fails halfway through.
                logger.exception("mapping: startup failed")

                try:
                    if self._explorer is not None:
                        self._explorer.stop()
                except Exception:
                    logger.exception(
                        "mapping: explorer cleanup after startup failure failed"
                    )

                self._explorer = None
                self._explore_enabled = False
                self._explorer_follow_only = False

                try:
                    if self._reader is not None:
                        self._reader.stop()
                except Exception:
                    logger.exception(
                        "mapping: reader cleanup after startup failure failed"
                    )

                self._reader = None
                self._last_error = f"mapping startup failed: {exc}"
                return self.get_state()
    def stop(self) -> dict:
        with self._lock:
            if not self._running:
                return self.get_state()
            self._stop_evt.set()
            try:
                if self._explorer is not None:
                    self._explorer.stop()
            except Exception:
                logger.exception("mapping: explorer stop failed")
            self._explorer = None
            self._explore_enabled = False
            self._explorer_follow_only = False
            self._pending_align_yaw = None
            self._manual_mapping = False
            self._manual_wall_throttle.clear()
            try:
                if self._reader is not None:
                    self._reader.stop()
            except Exception:
                logger.exception("mapping: reader stop failed")
            self._reader = None
            try:
                self._nav.flush()
            except Exception:
                logger.exception("mapping: nav flush failed")
            # zero movement just in case
            try:
                self._osc.client.send_message("/input/Vertical", 0.0)
                self._osc.client.send_message("/input/Horizontal", 0.0)
                self._osc.client.send_message("/input/LookHorizontal", 0.0)
                self._osc.client.send_message("/input/Run", 0)
            except Exception:
                pass
            self._running = False

            tick_thread = self._tick_thread
            self._tick_thread = None
            if (
                tick_thread is not None
                and tick_thread is not threading.current_thread()
                and tick_thread.is_alive()
            ):
                # Do not block shutdown indefinitely if an external component
                # stalls. The daemon thread also observes _stop_evt.
                tick_thread.join(timeout=1.0)

            logger.info("mapping: stopped")
            return self.get_state()

    def set_explore(self, enabled: bool) -> dict:
        with self._lock:
            self._set_explore(enabled)
            return self.get_state()

    def _set_explore(self, enabled: bool) -> None:
        if not self._running:
            # remember desired state for next start
            self._explore_enabled = enabled
            return
        if enabled and self._explorer is None:
            self._explorer = VoxelExplorer(self._nav, self._osc,
                                            learning_mode=True)
            self._explorer.force_run = self._force_run
            self._explorer.speed_mode = self._speed_mode
            self._explorer.start()
            self._explore_enabled = True
            self._explorer_follow_only = False
            logger.info("mapping: explorer enabled")
        elif enabled and self._explorer is not None:
            # explorer already running (likely from a goto) -- keep it but
            # stop treating it as follow-only so it can run discovery.
            self._explore_enabled = True
            self._explorer_follow_only = False
        elif not enabled and self._explorer is not None:
            try:
                self._explorer.stop()
            except Exception:
                logger.exception("mapping: explorer stop failed")
            self._explorer = None
            self._explore_enabled = False
            self._explorer_follow_only = False
            logger.info("mapping: explorer disabled")

    def _handle_world_change(self, new_world: str) -> None:
        """Detected that VRChat moved us to a different world. Flush the
        old map, swap in the new one, and reset all per-world state so we
        dont observe the new pose into the old map (which creates a stray
        voxel out in the void of the new map at the old coords)."""
        with self._lock:
            old = self._world_id
            logger.info("mapping: world change %s -> %s, hot swapping",
                        old, new_world)
            # stop the explorer cold so it cant drive on a stale follow
            # queue thats indexed against the old map.
            if self._explorer is not None:
                try:
                    self._explorer.stop()
                except Exception:
                    logger.exception("mapping: explorer stop on world swap failed")
                self._explorer = None
            self._explore_enabled = False
            self._explorer_follow_only = False
            self._pending_align_yaw = None
            # flush + load. load_world also clears nav._current/_previous.
            try:
                self._nav.load_world(new_world)
            except Exception:
                logger.exception("mapping: load_world failed during swap")
            self._world_id = new_world
            self._world_name = self._resolve_world_name()
            self._ensure_waypoints(new_world)
            # forget the last pose so the next tick doesnt paint the old
            # coords into the new map.
            self._last_pose = None
            self._last_pose_t = 0.0
            self._manual_wall_throttle.clear()
            # zero movement just in case the avatar was mid-input.
            try:
                self._osc.client.send_message("/input/Vertical", 0.0)
                self._osc.client.send_message("/input/Horizontal", 0.0)
                self._osc.client.send_message("/input/LookHorizontal", 0.0)
                self._osc.client.send_message("/input/Run", 0)
            except Exception:
                pass

    def _run(self) -> None:
        last_flush = time.monotonic()
        last_world_check = 0.0
        last_gap_fill = time.monotonic()

        tick_hz = max(1.0, min(float(self._tick_hz), 120.0))
        interval = 1.0 / tick_hz
        next_tick = time.monotonic()

        while not self._stop_evt.is_set():
            reader = self._reader
            if reader is None:
                break
            try:
                # check for VRChat world change ~every 2s. cheap, just a
                # string compare against the instance monitor.
                now_pre = time.monotonic()
                if now_pre - last_world_check >= 2.0:
                    last_world_check = now_pre
                    try:
                        new_world = self._resolve_world_id()
                        if new_world and new_world != self._world_id:
                            self._handle_world_change(new_world)
                            continue  # skip this tick, dont use stale pose
                    except Exception:
                        logger.exception("mapping: world change probe failed")
                pose = reader.get()
                if pose is not None and pose.timestamp != self._last_pose_t:
                    self._last_pose_t = pose.timestamp
                    self._last_pose = pose
                    grounded = bool(getattr(self._osc, "grounded", True))
                    self._nav.observe(pose.x, pose.y, pose.z,
                                       grounded=grounded, interpolate=True)
                    if self._explorer is not None:
                        self._explorer.tick(pose.x, pose.y, pose.z, pose.yaw)
                        # if we only spun the explorer up for a goto, tear
                        # it down the moment the follow queue is empty so
                        # we dont silently slide into discovery mode.
                        if (self._explorer_follow_only
                                and not self._explore_enabled
                                and self._explorer is not None
                                and not self._explorer.follow_status.get("active")):
                            try:
                                self._explorer.stop()
                            except Exception:
                                logger.exception("mapping: explorer auto-stop failed")
                            self._explorer = None
                            self._explorer_follow_only = False
                            logger.info("mapping: explorer torn down after goto complete")
                    # yaw alignment runs after the explorer is gone so they
                    # dont fight over LookHorizontal.
                    if (self._pending_align_yaw is not None
                            and self._explorer is None):
                        self._drive_yaw_alignment(pose.yaw)
                    # manual mapping: only active when the user has explicitly
                    # toggled it on and the explorer isnt driving. uses the
                    # forward raycast to flag obvious walls.
                    if (self._manual_mapping
                            and self._explorer is None):
                        self._manual_mapping_tick(pose)
                        # grid lock runs only when manual is on, the
                        # explorer isnt driving, and theres no pending
                        # waypoint alignment fighting for LookHorizontal.
                        if (self.manual_grid_snap
                                and self._pending_align_yaw is None):
                            self._drive_grid_lock(pose)
                now = time.monotonic()
                if now - last_flush >= 5.0:
                    self._nav.flush()
                    last_flush = now
                # periodic interior hole-fill while actively exploring. the
                # passive footstep map always speckles the floor with single
                # cell gaps; close them every 20s so coverage looks solid and
                # the frontier picker stops chasing un-walkable interior holes.
                if (self._explore_enabled and self._explorer is not None
                        and now - last_gap_fill >= 20.0):
                    last_gap_fill = now
                    try:
                        filled = self._nav.fill_interior_gaps()
                        if filled:
                            logger.info("mapping: hole-fill closed %d interior "
                                        "gap cells", filled)
                    except Exception:
                        logger.exception("mapping: hole-fill failed")
            except Exception:
                logger.exception("mapping: tick failed")
            # Monotonic deadline scheduling avoids cumulative drift when
            # pose/nav work takes a variable amount of time.
            next_tick += interval
            remaining = next_tick - time.monotonic()

            if remaining > 0:
                self._stop_evt.wait(remaining)
            else:
                # If a slow tick falls behind, resynchronize instead of
                # spinning at 100% CPU trying to catch up.
                next_tick = time.monotonic()
        # final flush
        try:
            self._nav.flush()
        except Exception:
            pass


