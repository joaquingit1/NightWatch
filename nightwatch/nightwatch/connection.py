"""GO2Connection that switches the lidar stream ON after connecting.

Discovery (July 23, on the real dog): camera, odometry, and control flow
over WebRTC, but `rt/utlidar/voxel_map_compressed` never publishes, so
VoxelGridMapper gets nothing, CostMapper publishes nothing, and EVERY
navigation goal dies with "No current global costmap available". Unitree
firmware gates the voxel-map stream behind a plain message on
`rt/utlidar/switch`, and nothing in dimos ever sends it (the exposed
publish_request RPC can't: it requires an api_id).

The class is deliberately ALSO named GO2Connection: autoconnect dedupes
modules by name keeping the newest, so listing this blueprint after
unitree_go2 replaces the stock connection instead of opening a second
WebRTC session.
"""

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import copy
import json
from threading import Event, Lock, RLock, Thread, Timer as ThreadTimer
import time
from typing import Any

from reactivex import operators as ops
from reactivex.disposable import CompositeDisposable, Disposable

from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.robot.unitree.connection import (
    UnitreeWebRTCConnection,
    pointcloud2_from_webrtc_lidar,
    time_is_now,
)
from dimos.robot.unitree.go2.connection import (
    GO2Connection as _StockGO2Connection,
    Go2Mode,
    _prefixed,
    make_connection,
)
from nightwatch.unitree import _request_succeeded, ensure_motion_ready
from unitree_webrtc_connect.constants import RTC_TOPIC
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

LIDAR_SWITCH_TOPIC = "rt/utlidar/switch"
# Unitree's WebRTC contract is a lifetime switch, not a sampling command:
# publish "on" once, subscribe, and leave it on.  Repeated on/off pulses can
# leave this firmware emitting only its first few voxel snapshots.  Sensor
# load is bounded after decode by the latest-only/rate gates below and by the
# Rerun bridge, never by toggling the firmware stream.
LIDAR_PUBLISH_HZ = 3.0
LIDAR_STALE_S = 3.0
LIDAR_START_GRACE_S = 8.0
LIDAR_RECONNECT_AFTER_S = 10.0
MOTION_COMMAND_GRACE_S = 2.0
VIDEO_STALE_S = 8.0
VIDEO_START_GRACE_S = 15.0
VIDEO_REARM_BACKOFF_S = 15.0
# Reasserting ON repairs a dropped video channel while the shared peer is
# healthy. If that does not produce a frame for a full 30 seconds, the track
# itself is dead and only a serialized peer rebuild can recreate it.
VIDEO_RECONNECT_AFTER_S = 30.0
WATCHDOG_POLL_S = 2.0
RECONNECT_BACKOFF_S = 5.0
# A failed rebuild already consumes the firmware slot-release wait. Do not
# create another signaling offer every watchdog tick.
RECONNECT_RETRY_BACKOFF_S = 60.0
# The Go2 firmware holds a single WebRTC slot and frees it roughly 20 s
# after the previous session dies. Rebuilding sooner produces a half-open
# peer stuck at have-local-offer that itself occupies signaling, after which
# every subsequent attempt fails too (observed July 23: 49 minutes of
# "reconnect failed" every 20 s).
WEBRTC_SLOT_FREE_S = 20.0
CONNECT_ATTEMPTS = 5
# Startup used to retry after 10 s even though the firmware needs about 20 s
# to release its single peer slot. Each premature offer could claim the slot
# again and make all three startup attempts fail deterministically. Respect the
# same proven release interval used by live-peer recovery.
CONNECT_RETRY_S = WEBRTC_SLOT_FREE_S
WEBRTC_REQUEST_TIMEOUT_S = 5.0
# A replacement-peer build against an out-of-range robot can block inside
# make_connection()/start() indefinitely. That hung thread kept
# _reconnecting=True forever, permanently disabling recovery: when the robot
# walked back into range the camera never returned (observed live 2026-07-25,
# camera stale for 8+ minutes after range was restored). Bound every build so
# a failed attempt always releases the recovery latch and retries later.
RECOVERY_BUILD_TIMEOUT_S = 45.0


class GO2Connection(_StockGO2Connection):
    """Go2 connection with bounded sensor load and tiered recovery.

    Upstream's video callback exits permanently when ``track.recv()`` raises
    ``MediaStreamError``.  Camera, lidar, odometry, and control all share one
    firmware WebRTC peer. Brief video staleness therefore reasserts only the
    idempotent video-on message. A peer explicitly reporting closed/failed, or
    a channel still stale after 30 seconds, receives one serialized full
    rebuild that restores all sensor subscriptions.
    """

    def __init__(self, **kwargs: Any) -> None:
        # Stock constructs the WebRTC peer once inside __init__, making one
        # transient ICE timeout fatal to the whole multi-process deployment.
        # Initialize the Module first, then retry only the hardware connection.
        Module.__init__(self, **kwargs)
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                self.connection = make_connection(
                    self.config.ip,
                    self.config.g,
                    aes_128_key=self.config.aes_128_key,
                    velocity_api=self.config.velocity_api,
                    mode=self.config.motion_mode,
                )
                break
            except Exception:
                if attempt >= CONNECT_ATTEMPTS:
                    raise
                logger.warning(
                    "Go2 WebRTC startup attempt failed; retrying",
                    attempt=attempt,
                    retry_in_s=CONNECT_RETRY_S,
                    exc_info=True,
                )
                time.sleep(CONNECT_RETRY_S)

        if hasattr(self.connection, "camera_info_static"):
            self.camera_info_static = self.connection.camera_info_static
        if self.config.frame_id_prefix and self.camera_info_static.frame_id:
            self.camera_info_static = copy.copy(self.camera_info_static)
            self.camera_info_static.frame_id = _prefixed(
                self.config.frame_id_prefix, self.camera_info_static.frame_id
            )

        self._lifecycle_stop = Event()
        self._connection_lock = RLock()
        self._recovery_lock = Lock()
        self._sensor_subscriptions: CompositeDisposable | None = None
        self._watchdog_thread: Thread | None = None
        self._camera_info_stop = Event()
        self._lidar_pulse_thread: Thread | None = None
        self._last_video_frame_at = 0.0
        self._last_video_rearm_at = 0.0
        self._locomotion_controller: str | None = None
        self._video_rearm_count = 0
        self._last_lidar_frame_at = 0.0
        self._motion_commanded_until = 0.0
        self._lidar_stale_while_moving_since = 0.0
        self._lidar_recovering = False
        self._connected_at = 0.0
        self._reconnecting = False
        self._reconnect_thread: Thread | None = None
        self._last_reconnect_attempt_at = 0.0
        self._last_reconnect_reason: str | None = None
        self._reconnect_count = 0
        self._nightwatch_connection_stopped = False
        self._gate_lidar_publish()

    @rpc
    def start(self) -> None:
        # Reimplement stock start so sensor subscriptions can be replaced
        # independently after a dead WebRTC video track.
        self._nightwatch_connection_stopped = False
        Module.start(self)
        self._lifecycle_stop.clear()
        self._camera_info_stop.clear()
        self.connection.start()
        self.register_disposable(Disposable(self.cmd_vel.subscribe(self.move)))
        self._bind_sensor_streams()

        if self.config.camera:
            self._camera_info_thread = Thread(
                target=self._publish_camera_info_until_stopped,
                name="Go2-camera-info",
                daemon=True,
            )
            self._camera_info_thread.start()

        # Recovery must never depend on the rest of startup succeeding. Every
        # call below this point talks to the firmware and can block for
        # minutes on a half-open peer; when the watchdogs were started last,
        # a single hung request killed camera/lidar recovery for the whole
        # process (observed live 2026-07-25: GO2Connection/start timed out
        # after 1200 s, camera stale 12 min, zero watchdog activity, safety
        # holding on SENSORS_NOT_READY). Start the supervisors FIRST.
        if self.config.lidar and isinstance(self.connection, UnitreeWebRTCConnection):
            self._lidar_pulse_thread = Thread(
                target=self._lidar_pulse_loop,
                name="Go2-lidar-pulse",
                daemon=True,
            )
            self._lidar_pulse_thread.start()

        if self.config.camera and isinstance(self.connection, UnitreeWebRTCConnection):
            self._watchdog_thread = Thread(
                target=self._video_watchdog,
                name="Go2-video-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()

        self._ensure_locomotion_controller()
        if not ensure_motion_ready(self, force=True):
            logger.error(
                "Go2 MCF readiness sequence failed; movement remains fail-stopped"
            )
        self._configure_live_connection()

    @rpc
    def stop(self) -> None:
        # The coordinator invokes stop over RPC, then the worker invokes it
        # again while exiting. A second call into publish_request on an already
        # stopped WebRTC event loop blocks worker shutdown until it is killed.
        if self._nightwatch_connection_stopped:
            logger.info("Ignoring duplicate GO2Connection stop")
            return
        self._nightwatch_connection_stopped = True

        self._lifecycle_stop.set()
        self._camera_info_stop.set()

        lidar_pulser = self._lidar_pulse_thread
        if lidar_pulser is not None and lidar_pulser.is_alive():
            lidar_pulser.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        self._lidar_pulse_thread = None

        watcher = self._watchdog_thread
        if watcher is not None and watcher.is_alive():
            watcher.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        self._watchdog_thread = None

        recovery = getattr(self, "_reconnect_thread", None)
        if recovery is not None and recovery.is_alive():
            recovery.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        self._reconnect_thread = None

        # Process shutdown is not a posture command. Lying the dog down here
        # made every restart depend on a second firmware stand-up transaction,
        # and a rejected StopMove precondition could then strand it down.
        # Stop commanded velocity, but preserve the operator-visible stance.
        try:
            self.connection.stop_movement()
        except Exception:
            logger.warning(
                "movement stop on teardown failed; continuing teardown",
                exc_info=True,
            )

        with self._connection_lock:
            if self._sensor_subscriptions is not None:
                self._sensor_subscriptions.dispose()
                self._sensor_subscriptions = None
            try:
                self.connection.stop()
            except Exception:
                logger.warning("connection stop failed", exc_info=True)

        if self._camera_info_thread and self._camera_info_thread.is_alive():
            self._camera_info_thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        self._camera_info_thread = None
        Module.stop(self)

    def _gate_lidar_publish(self) -> None:
        """Final latest-only guard for non-WebRTC/replay lidar sources."""
        try:
            original_publish = self.lidar.publish
            min_period = 1.0 / LIDAR_PUBLISH_HZ
            last = [0.0]

            def gated_publish(msg: Any) -> None:
                now = time.monotonic()
                if now - last[0] >= min_period:
                    last[0] = now
                    original_publish(msg)

            self.lidar.publish = gated_publish  # type: ignore[method-assign]
            logger.info("Lidar publish gated", hz=LIDAR_PUBLISH_HZ)
        except Exception:
            logger.exception("Could not gate lidar publish rate")

    def _bind_sensor_streams(self) -> None:
        bundle = CompositeDisposable()
        self._sensor_subscriptions = bundle
        self.register_disposable(bundle)
        self._connected_at = time.monotonic()
        self._latest_video_frame = None
        self._last_video_frame_at = 0.0
        self._last_lidar_frame_at = 0.0

        def on_error(sensor: str):
            return lambda exc: logger.error(
                "Go2 sensor stream failed", sensor=sensor, error=repr(exc)
            )

        def onimage(image: Any) -> None:
            image.frame_id = _prefixed(self.config.frame_id_prefix, image.frame_id)
            self._latest_video_frame = image
            self._last_video_frame_at = time.monotonic()
            self.color_image.publish(image)

        def onlidar(pointcloud: Any) -> None:
            self._last_lidar_frame_at = time.monotonic()
            self._lidar_stale_while_moving_since = 0.0
            self.lidar.publish(pointcloud)

        if self.config.lidar:
            # The source is pulsed at 1 Hz because the Unitree SDK expands the
            # compressed packet before invoking this observable.  throttle_first
            # is an additional guard against the few packets that arrive during
            # each 220 ms pulse; only one reaches Open3D and Zenoh.
            if isinstance(self.connection, UnitreeWebRTCConnection):
                lidar_stream = self.connection.raw_lidar_stream().pipe(
                    ops.throttle_first(1.0 / LIDAR_PUBLISH_HZ),
                    ops.map(pointcloud2_from_webrtc_lidar),
                    ops.map(time_is_now),
                )
            else:
                lidar_stream = self.connection.lidar_stream()
            bundle.add(
                lidar_stream.subscribe(
                    onlidar, on_error=on_error("lidar")
                )
            )
        bundle.add(
            self.connection.odom_stream().subscribe(
                self._publish_tf, on_error=on_error("odometry")
            )
        )
        bundle.add(
            self.connection.lowstate_stream().subscribe(
                self._on_lowstate, on_error=on_error("lowstate")
            )
        )
        if self.config.camera:
            bundle.add(
                self.connection.video_stream().subscribe(
                    onimage, on_error=on_error("video")
                )
            )

    def _configure_live_connection(self) -> None:
        try:
            if self.config.mode == Go2Mode.RAGE:
                self.connection.set_rage_mode(True)
            response = self.publish_request(
                RTC_TOPIC["OBSTACLES_AVOID"],
                {
                    "api_id": 1001,
                    "parameter": {
                        "enable": int(self.config.g.obstacle_avoidance)
                    },
                },
            )
            if not _request_succeeded(response):
                logger.warning(
                    "Go2 obstacle-avoidance configuration was rejected",
                    response=repr(response)[:300],
                )
        except Exception:
            logger.exception("Go2 post-connect configuration failed")
        # The stream monitor owns the firmware switch and keeps it enabled for
        # the lifetime of the subscription, as required by Unitree's driver.

    def _publish_camera_info_until_stopped(self) -> None:
        while not self._camera_info_stop.is_set():
            self.camera_info.publish(self.camera_info_static)
            self._camera_info_stop.wait(1.0)

    def _video_watchdog(self) -> None:
        while not self._lifecycle_stop.wait(WATCHDOG_POLL_S):
            # One unhandled exception used to kill this thread outright, and
            # with it every future camera recovery for the life of the
            # process. Recovery is the last line of defence: it must survive
            # its own bugs.
            try:
                self._video_recovery_tick(time.monotonic())
            except Exception:
                logger.exception("video watchdog tick failed; supervisor continues")

    def _video_recovery_tick(self, now: float) -> str:
        """Apply one watchdog decision and return it for focused diagnostics."""
        age = (
            now - self._last_video_frame_at
            if self._last_video_frame_at
            else now - self._connected_at
        )
        peer_state = self._webrtc_peer_state()
        if peer_state in {"closed", "failed"}:
            if self._request_full_recovery(f"peer_{peer_state}"):
                logger.error(
                    "Go2 WebRTC peer is terminal; scheduling one full recovery",
                    peer_state=peer_state,
                    frame_age_s=round(age, 1),
                )
                return "reconnect"
            return "waiting"

        if self._reconnecting:
            return "waiting"

        if age >= VIDEO_RECONNECT_AFTER_S:
            if self._request_full_recovery("video_prolonged_stale"):
                logger.error(
                    "Go2 video remained stale after channel re-arm; "
                    "scheduling one full recovery",
                    peer_state=peer_state,
                    frame_age_s=round(age, 1),
                )
                return "reconnect"
            return "waiting"

        limit = VIDEO_STALE_S if self._last_video_frame_at else VIDEO_START_GRACE_S
        if (
            age <= limit
            or now - self._last_video_rearm_at < VIDEO_REARM_BACKOFF_S
        ):
            return "healthy"

        # Video and robot control share one firmware WebRTC peer. While that
        # peer still reports healthy, reasserting the existing ON state is the
        # bounded recovery that leaves lidar/odom/control untouched.
        logger.warning(
            "Go2 video stale; reasserting video-on without replacing peer",
            frame_age_s=round(age, 1),
            peer_state=peer_state,
            rearm_count=self._video_rearm_count,
        )
        self._rearm_video_channel()
        return "rearm"

    def _ensure_locomotion_controller(self) -> bool:
        """Observe the controller without changing the firmware's MCF mode.

        Go2 firmware 1.1.7+ uses MCF for the sport API. The live robot returns
        status 7004 for SelectMode("normal"), so startup must not try to switch
        away from MCF. CheckMode remains useful read-only diagnostics.
        """
        if not isinstance(self.connection, UnitreeWebRTCConnection):
            return True
        observed = self._motion_mode_name()
        self._locomotion_controller = observed
        if observed is None:
            logger.warning("Go2 locomotion controller could not be observed")
            return False
        logger.info(
            "Go2 locomotion controller observed; preserving firmware mode",
            observed=observed,
            velocity_api=(
                "mcf_sport"
                if self.config.velocity_api
                else "wireless_controller"
            ),
        )
        return True

    def _motion_mode_name(self) -> str | None:
        """Report the firmware's active locomotion controller, or None.

        Read-only motion-switcher CheckMode (api_id 1001). Never raises: this
        is diagnostics on the startup path.
        """
        connection = getattr(self, "connection", None)
        if not isinstance(connection, UnitreeWebRTCConnection):
            return None
        try:
            response = connection.publish_request(
                RTC_TOPIC["MOTION_SWITCHER"], {"api_id": 1001}
            )
            return str(json.loads(response["data"]["data"]).get("name"))
        except Exception:
            return None

    def _webrtc_peer_state(self) -> str | None:
        connection = getattr(self, "connection", None)
        if not isinstance(connection, UnitreeWebRTCConnection):
            return None
        peer = getattr(getattr(connection, "conn", None), "pc", None)
        if peer is None:
            return "closed"
        connection_state = str(
            getattr(peer, "connectionState", "") or ""
        ).lower()
        ice_state = str(getattr(peer, "iceConnectionState", "") or "").lower()
        for state in (connection_state, ice_state):
            if state in {"closed", "failed"}:
                return state
        return connection_state or ice_state or None

    def _request_full_recovery(self, reason: str) -> bool:
        """Start at most one peer rebuild, with a hard retry backoff."""
        with self._recovery_lock:
            now = time.monotonic()
            if (
                self._lifecycle_stop.is_set()
                or self._reconnecting
                or (
                    self._last_reconnect_attempt_at > 0.0
                    and now - self._last_reconnect_attempt_at
                    < RECONNECT_RETRY_BACKOFF_S
                )
            ):
                return False
            self._reconnecting = True
            self._last_reconnect_attempt_at = now
            self._last_reconnect_reason = reason
            recovery = Thread(
                target=self._reconnect,
                name="Go2-WebRTC-recovery",
                daemon=True,
            )
            self._reconnect_thread = recovery
            recovery.start()
            return True

    def _rearm_video_channel(self) -> None:
        """Reassert video ON without toggling it off or replacing the peer."""
        self._last_video_rearm_at = time.monotonic()
        self._video_rearm_count += 1
        connection = self.connection
        if not isinstance(connection, UnitreeWebRTCConnection):
            return
        try:
            connection.loop.call_soon_threadsafe(
                connection.conn.video.switchVideoChannel,
                True,
            )
        except Exception:
            logger.exception("Go2 video-on reassertion failed")

    def _build_replacement_bounded(self) -> Any | None:
        """Build and start a replacement peer, bounded by a hard timeout.

        make_connection()/start() against an out-of-range robot can block
        indefinitely. Running the build on a helper thread and abandoning it
        after RECOVERY_BUILD_TIMEOUT_S guarantees _reconnect always finishes,
        releases the recovery latch, and retries later. An abandoned build
        that eventually completes stops its own connection immediately so the
        firmware's single WebRTC slot is freed (atomic handoff below closes
        the completion race).
        """
        result: dict[str, Any] = {}
        handoff = Lock()
        abandoned = Event()

        def build() -> None:
            try:
                candidate = make_connection(
                    self.config.ip,
                    self.config.g,
                    aes_128_key=self.config.aes_128_key,
                    velocity_api=self.config.velocity_api,
                    mode=self.config.motion_mode,
                )
                candidate.start()
            except Exception:
                logger.exception("Go2 replacement peer build failed")
                return
            with handoff:
                if not abandoned.is_set():
                    result["connection"] = candidate
                    return
            # Abandoned while building: free the firmware slot right away.
            try:
                candidate.stop()
            except Exception:
                logger.warning("Abandoned replacement teardown failed", exc_info=True)

        builder = Thread(target=build, name="Go2-WebRTC-build", daemon=True)
        builder.start()
        builder.join(RECOVERY_BUILD_TIMEOUT_S)
        with handoff:
            if "connection" in result:
                return result["connection"]
            abandoned.set()
        if builder.is_alive():
            logger.error(
                "Go2 replacement peer build exceeded its budget; abandoning it",
                timeout_s=RECOVERY_BUILD_TIMEOUT_S,
            )
        return None

    def _reconnect(self) -> None:
        """Replace a terminal shared peer and restore every stream exactly once."""
        replacement = None
        try:
            with self._connection_lock:
                if self._lifecycle_stop.is_set():
                    return
                old_connection = self.connection
                try:
                    old_connection.stop_movement()
                except Exception:
                    pass
                if self._sensor_subscriptions is not None:
                    self._sensor_subscriptions.dispose()
                    self._sensor_subscriptions = None
                try:
                    old_connection.stop()
                except Exception:
                    logger.warning("Old WebRTC teardown failed", exc_info=True)

                # Give the firmware time to free its single WebRTC slot;
                # connecting sooner creates the half-open-peer death spiral
                # described at WEBRTC_SLOT_FREE_S.
                logger.info(
                    "Waiting for the Go2 WebRTC slot to free before rebuild",
                    wait_s=WEBRTC_SLOT_FREE_S,
                )
                if self._lifecycle_stop.wait(WEBRTC_SLOT_FREE_S):
                    return

                replacement = self._build_replacement_bounded()
                if replacement is None:
                    raise RuntimeError(
                        "replacement peer build timed out or failed; "
                        "will retry after backoff"
                    )
                self.connection = replacement
                # These subscriptions recreate video track callbacks plus
                # lidar, odometry, and lowstate delivery on the new peer.
                self._bind_sensor_streams()
                self._ensure_locomotion_controller()
                if not ensure_motion_ready(self, force=True):
                    logger.error(
                        "Go2 MCF readiness failed after WebRTC recovery; "
                        "movement remains fail-stopped"
                    )
                self._configure_live_connection()
                if self.config.lidar:
                    self._set_lidar_stream(True)
                self._reconnect_count += 1
                replacement = None
                logger.info(
                    "Go2 WebRTC connection recovered",
                    count=self._reconnect_count,
                    reason=self._last_reconnect_reason,
                )
        except Exception:
            logger.exception("Go2 WebRTC reconnect failed")
            self._connected_at = time.monotonic() + RECONNECT_BACKOFF_S
            self._last_video_frame_at = 0.0
        finally:
            # A failed rebuild must not leave a half-open peer holding the
            # firmware's only slot; that would doom every later attempt.
            if replacement is not None:
                try:
                    replacement.stop()
                except Exception:
                    logger.warning(
                        "Failed replacement teardown failed", exc_info=True
                    )
            with self._recovery_lock:
                self._reconnecting = False

    @rpc
    def camera_health(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "has_frame": self._latest_video_frame is not None,
            "frame_age_s": (
                round(now - self._last_video_frame_at, 3)
                if self._last_video_frame_at
                else None
            ),
            "reconnecting": self._reconnecting,
            "reconnect_count": self._reconnect_count,
            "reconnect_reason": getattr(self, "_last_reconnect_reason", None),
            "peer_state": self._webrtc_peer_state(),
            "video_rearm_count": self._video_rearm_count,
            "lidar_age_s": (
                round(now - self._last_lidar_frame_at, 3)
                if self._last_lidar_frame_at
                else None
            ),
            "lidar_recovering": self._lidar_recovering,
            "motion_commanded": now <= self._motion_commanded_until,
            "locomotion_controller": self._locomotion_controller,
            "velocity_wire_api": (
                "mcf_sport"
                if self.config.velocity_api
                else "wireless_controller"
            ),
        }

    @rpc
    def move(self, twist: Any, duration: float = 0.0) -> bool:
        """Forward motion while exposing intent to the lidar health monitor.

        Raw Go2 odometry drifts by centimetres while BalanceStand is stationary,
        so it cannot distinguish real motion from pose-estimator noise.  The
        command stream is authoritative: planners and WASD both pass through
        this method continuously while the robot is expected to move.
        """
        speed = (
            abs(float(twist.linear.x))
            + abs(float(twist.linear.y))
            + abs(float(twist.angular.z))
        )
        if speed > 1e-4:
            self._motion_commanded_until = time.monotonic() + max(
                MOTION_COMMAND_GRACE_S, float(duration) + 0.5
            )
        # Stock UnitreeWebRTCConnection.move waits forever for a callback
        # scheduled onto the WebRTC asyncio loop.  If that peer stalls, the
        # Zenoh cmd_vel subscriber is permanently consumed and every later
        # navigation command disappears even though the planner keeps running.
        # Continuous planner commands only need a thread-safe enqueue; retain
        # the driver's deadman timer and never wait on the unhealthy loop.
        connection = self.connection
        if isinstance(connection, UnitreeWebRTCConnection) and duration <= 0.0:
            if not connection.loop.is_running():
                logger.error("Dropped movement command: WebRTC loop is not running")
                return False
            if connection.stop_timer:
                connection.stop_timer.cancel()
            connection.stop_timer = ThreadTimer(
                connection.cmd_vel_timeout, connection.stop_movement
            )
            connection.stop_timer.daemon = True
            connection.stop_timer.start()
            connection.loop.call_soon_threadsafe(
                connection._publish_movement,
                float(twist.linear.x),
                float(twist.linear.y),
                float(twist.angular.z),
            )
            return True
        return bool(self.connection.move(twist, duration))

    @rpc
    def publish_request(self, topic: str, data: dict[str, Any]) -> Any:
        """Publish a bounded firmware RPC without wedging the module worker."""
        connection = self.connection
        if not isinstance(connection, UnitreeWebRTCConnection):
            return connection.publish_request(topic, data)
        if not connection.loop.is_running():
            return {"error": "WebRTC loop is not running"}
        future = asyncio.run_coroutine_threadsafe(
            connection.conn.datachannel.pub_sub.publish_request_new(topic, data),
            connection.loop,
        )
        try:
            return future.result(timeout=WEBRTC_REQUEST_TIMEOUT_S)
        except FutureTimeoutError:
            future.cancel()
            logger.error(
                "Go2 firmware request timed out",
                topic=topic,
                api_id=data.get("api_id"),
                timeout_s=WEBRTC_REQUEST_TIMEOUT_S,
            )
            return {"error": "firmware request timeout"}

    def _set_lidar_stream(self, enabled: bool) -> None:
        conn: Any = getattr(self, "connection", None)
        if conn is None or not hasattr(conn, "conn"):
            logger.warning("No live WebRTC connection; lidar stream switch skipped")
            return
        try:
            pub_sub = conn.conn.datachannel.pub_sub

            def _send() -> None:
                try:
                    value = "on" if enabled else "off"
                    pub_sub.publish_without_callback(LIDAR_SWITCH_TOPIC, value)
                except Exception:
                    logger.exception(
                        "Firmware lidar switch send failed", enabled=enabled
                    )

            conn.loop.call_soon_threadsafe(_send)
        except Exception:
            logger.exception("Failed to switch lidar stream", enabled=enabled)

    def _lidar_pulse_loop(self) -> None:
        """Keep the firmware stream enabled and detect a genuinely dead feed.

        The Go2 occasionally leaves the lidar subscription silent even though
        the WebRTC peer, video, odometry, and data channel all remain healthy.
        Re-publishing ``rt/utlidar/switch`` is safe and idempotent. Never
        rebuild the shared peer here: doing so removes motor control and all
        sensors at once, while the firmware keeps its single signaling slot
        occupied. Stationary estimator drift is ignored because motion intent
        comes from the command stream.
        """
        logger.info("Lidar stream monitor started", publish_hz=LIDAR_PUBLISH_HZ)
        self._set_lidar_stream(True)
        while not self._lifecycle_stop.wait(1.0):
            try:
                self._lidar_pulse_tick()
            except Exception:
                logger.exception("lidar monitor tick failed; supervisor continues")
        self._set_lidar_stream(False)

    def _lidar_pulse_tick(self) -> None:
        """One lidar-health decision; see _lidar_pulse_loop for the policy."""
        # (extracted from the loop so one bad tick cannot kill the supervisor)
        now = time.monotonic()
        age = (
            now - self._last_lidar_frame_at
            if self._last_lidar_frame_at
            else now - self._connected_at
        )
        raw_stale = age > (
            LIDAR_STALE_S if self._last_lidar_frame_at else LIDAR_START_GRACE_S
        )
        moving_recently = now <= self._motion_commanded_until
        # The firmware voxel-map topic is change-driven: while the robot
        # is stationary, receiving no new cloud is normal.  It is a fault
        # only if no first cloud arrives, or odometry shows continued
        # motion without corresponding map updates.
        stale = raw_stale and (
            not self._last_lidar_frame_at or moving_recently
        )
        if stale and moving_recently:
            if not self._lidar_stale_while_moving_since:
                self._lidar_stale_while_moving_since = now
        else:
            self._lidar_stale_while_moving_since = 0.0
        if stale and not self._lidar_recovering:
            self._lidar_recovering = True
            logger.warning(
                "Lidar stale during commanded motion; reasserting stream switch",
                frame_age_s=round(age, 1),
            )
        elif not stale and self._lidar_recovering:
            self._lidar_recovering = False
            logger.info("Lidar stream recovered", frame_age_s=round(age, 2))
        if (
            self._lidar_stale_while_moving_since
            and now - self._lidar_stale_while_moving_since
            > LIDAR_RECONNECT_AFTER_S
            and not self._reconnecting
            and not self._lifecycle_stop.is_set()
        ):
            logger.error(
                "Lidar remained stale after re-arm; preserving shared WebRTC peer",
                frame_age_s=round(age, 1),
                reconnect_count=self._reconnect_count,
            )
            # Report again only after another full bounded interval. The
            # safety supervisor will hold if navigation inputs are truly
            # unavailable, but the watchdog must not cause that outage.
            self._lidar_stale_while_moving_since = now
            self._set_lidar_stream(True)
            return
        if stale:
            self._set_lidar_stream(True)
