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
from threading import Event, RLock, Thread, Timer as ThreadTimer
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
WATCHDOG_POLL_S = 2.0
RECONNECT_BACKOFF_S = 5.0
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_S = 10.0
# The Go2 firmware holds a single WebRTC slot and frees it roughly 20 s
# after the previous session dies. Rebuilding sooner produces a half-open
# peer stuck at have-local-offer that itself occupies signaling, after which
# every subsequent attempt fails too (observed July 23: 49 minutes of
# "reconnect failed" every 20 s).
WEBRTC_SLOT_FREE_S = 20.0
WEBRTC_REQUEST_TIMEOUT_S = 5.0


class GO2Connection(_StockGO2Connection):
    """Go2 connection with bounded sensor load and WebRTC self-recovery.

    Upstream's video callback exits permanently when ``track.recv()`` raises
    ``MediaStreamError``.  The data channel can remain healthy, so every module
    and HTTP endpoint looks alive while no further image can ever arrive.
    This module watches frame age and replaces the entire peer connection,
    then re-subscribes lidar, odometry, lowstate and video.
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
        self._sensor_subscriptions: CompositeDisposable | None = None
        self._watchdog_thread: Thread | None = None
        self._camera_info_stop = Event()
        self._lidar_pulse_thread: Thread | None = None
        self._last_video_frame_at = 0.0
        self._last_lidar_frame_at = 0.0
        self._motion_commanded_until = 0.0
        self._lidar_stale_while_moving_since = 0.0
        self._lidar_recovering = False
        self._connected_at = 0.0
        self._reconnecting = False
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

        if self.config.motion_mode and isinstance(self.connection, UnitreeWebRTCConnection):
            self.connection.set_motion_mode(self.config.motion_mode)

        self.standup()
        time.sleep(3.0)
        self._configure_live_connection()

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

        try:
            self.liedown()
        except Exception:
            logger.warning("liedown on stop failed; continuing teardown", exc_info=True)

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
            self.connection.balance_stand()
            if self.config.mode == Go2Mode.RAGE:
                self.connection.set_rage_mode(True)
            self.connection.set_obstacle_avoidance(self.config.g.obstacle_avoidance)
            if hasattr(self.connection, "switch_joystick"):
                self.connection.switch_joystick(True)
                logger.info("Firmware joystick listening enabled")
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
            now = time.monotonic()
            age = (
                now - self._last_video_frame_at
                if self._last_video_frame_at
                else now - self._connected_at
            )
            limit = VIDEO_STALE_S if self._last_video_frame_at else VIDEO_START_GRACE_S
            if age <= limit or self._reconnecting:
                continue
            logger.error(
                "Go2 video stale; rebuilding WebRTC connection",
                frame_age_s=round(age, 1),
                reconnect_count=self._reconnect_count,
            )
            self._reconnect()

    def _reconnect(self) -> None:
        self._reconnecting = True
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

                replacement = make_connection(
                    self.config.ip,
                    self.config.g,
                    aes_128_key=self.config.aes_128_key,
                    velocity_api=self.config.velocity_api,
                )
                replacement.start()
                self.connection = replacement
                replacement = None
                self._bind_sensor_streams()
                self._configure_live_connection()
                self._reconnect_count += 1
                logger.info("Go2 WebRTC connection recovered", count=self._reconnect_count)
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
            "lidar_age_s": (
                round(now - self._last_lidar_frame_at, 3)
                if self._last_lidar_frame_at
                else None
            ),
            "lidar_recovering": self._lidar_recovering,
            "motion_commanded": now <= self._motion_commanded_until,
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
        Re-publishing ``rt/utlidar/switch`` is attempted first; after a bounded
        interval of commanded motion without clouds, rebuild the peer so the
        SDK re-subscribes to ``voxel_map_compressed``.  Stationary estimator
        drift is ignored because motion intent comes from the command stream.
        """
        logger.info("Lidar stream monitor started", publish_hz=LIDAR_PUBLISH_HZ)
        self._set_lidar_stream(True)
        while not self._lifecycle_stop.wait(1.0):
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
                    "Lidar remained stale after re-arm; rebuilding WebRTC connection",
                    frame_age_s=round(age, 1),
                    reconnect_count=self._reconnect_count,
                )
                self._reconnect()
                # _bind_sensor_streams resets the lidar clock/start grace.
                self._set_lidar_stream(True)
                continue
            if stale:
                self._set_lidar_stream(True)
        self._set_lidar_stream(False)
