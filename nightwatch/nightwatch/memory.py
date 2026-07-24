"""Persistent semantic and geometric memory for the Nightwatch scout."""

import asyncio
import math
from pathlib import Path
import time
from typing import Any

import numpy as np
import reactivex as rx
from reactivex.disposable import CompositeDisposable

from dimos.core.core import rpc
from dimos.core.stream import In
from dimos.mapping.relocalization.module import (
    FRAME_MAP,
    FRAME_WORLD,
    MAP_SUFFIX,
    RelocalizationModule,
)
from dimos.mapping.relocalization.relocalize import relocalize as _relocalize
from dimos.memory2.module import Recorder, RecorderConfig, pose_setter_for
from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Transform import Transform
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.perception.spatial_perception import SpatialMemory
from dimos.types.robot_location import RobotLocation
from dimos.utils.data import resolve_named_path
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class NightwatchSpatialMemory(SpatialMemory):
    """Make DimOS' semantic-memory shutdown safe to call more than once.

    The coordinator calls ``stop`` over RPC and the worker calls it again while
    exiting. Upstream saves and clears on every call, so the second invocation
    overwrites the just-saved pickle with zero images. This guard deliberately
    lives in the module instance, where both lifecycle calls occur.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._nightwatch_memory_stopped = False

    @rpc
    def start(self) -> None:
        self._nightwatch_memory_stopped = False
        super().start()

    @rpc
    def stop(self) -> None:
        if self._nightwatch_memory_stopped:
            logger.info("Ignoring duplicate SpatialMemory stop")
            return
        self._nightwatch_memory_stopped = True
        super().stop()

    @rpc
    def add_named_location(
        self,
        name: str,
        position: list[float] | None = None,
        rotation: list[float] | None = None,
        description: str | None = None,
    ) -> bool:
        """Store an exact supplied position, or the current pose if omitted.

        Upstream accepts ``position`` and ``rotation`` arguments but ignores
        both and always reads the current TF pose. That would put an observed
        AprilTag at the robot rather than at the marker.
        """
        if position is None or rotation is None:
            tf = self.tf.get("world", "base_link")
            if tf is None:
                logger.error("No position available for robot location")
                return False
            if position is None:
                position = [
                    float(tf.translation.x),
                    float(tf.translation.y),
                    float(tf.translation.z),
                ]
            if rotation is None:
                euler = tf.rotation.to_euler()
                rotation = [float(euler.x), float(euler.y), float(euler.z)]
        location = RobotLocation(
            name=name,
            position=tuple(float(v) for v in position[:3]),
            rotation=tuple(float(v) for v in rotation[:3]),
            metadata={"description": description or name},
        )
        return self.tag_location(location)


class NightwatchRelocalization(RelocalizationModule):
    """Load the saved premap defensively and expose alignment state.

    The stock module raises out of ``start`` when the configured premap is
    missing or corrupt, which would take the whole scout stack down at boot.
    A bad premap must instead degrade to live-map-only navigation, exactly
    like an unset ``NIGHTWATCH_PREMAP``. The status RPC lets the curiosity
    supervisor recognize an already-mapped floor right after ICP locks.

    One venue-hardening change on top of stock:
    - The premap is published to the viewer immediately (not only after the
      first lock), so an operator sees the remembered floor at startup.

    Relocalization deliberately keeps the configured fitness threshold as a
    hard gate. Repeated geometry on this floor produced both the same wrong
    37 m corridor alignment and marginal 0.45-0.52 matches that destabilized
    live navigation. The blueprint therefore uses a conservative 0.55 gate;
    the former below-threshold "consensus" escape hatch accepted a false match,
    shifted every persistent keep-out, and sent patrol back to glass.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # The accepted world->map transform (session WORLD -> persistent MAP),
        # kept in full 4x4 form so world_to_map_2d can project it to the plane.
        self._accepted_world_to_map: np.ndarray | None = None
        self._rejected_fitnesses: list[float] = []

    @rpc
    def start(self) -> None:
        map_file = self.config.map_file
        if map_file:
            try:
                path = resolve_named_path(map_file, MAP_SUFFIX)
                PointCloud2.lcm_decode(path.read_bytes())
            except Exception:
                logger.exception(
                    "Premap unreadable; relocalization disabled for this session",
                    map_file=str(map_file),
                )
                try:
                    self.config.map_file = None
                except Exception:
                    object.__setattr__(self.config, "map_file", None)
        super().start()
        # Stock publishes loaded_map only after the first successful lock
        # (its interval stream gates on the world->map TF). Show the
        # remembered floor right away instead; once alignment locks, the
        # stock periodic publisher takes over.
        if self._premap is not None and self.config.publish_loaded_map:
            self.register_disposable(
                rx.interval(2.0).subscribe(self._publish_premap_prelock)
            )

    def _publish_premap_prelock(self, _tick: int) -> None:
        if self._alignment_locked or self._premap is None:
            return
        try:
            self.loaded_map.publish(self._premap)
        except Exception:
            logger.exception("premap preview publish failed")

    def _try_relocalize(self, msg: PointCloud2) -> Transform | None:
        assert self._premap is not None
        t0 = time.monotonic()
        try:
            T, fitness = _relocalize(self._premap.pointcloud, msg.pointcloud)
        except Exception:
            logger.exception("relocalize() failed")
            self._failed_attempts += 1
            return None
        dt = time.monotonic() - t0
        n_pts = len(msg)

        if fitness < self.config.fitness_threshold:
            self._failed_attempts += 1
            rejected_fitnesses = getattr(self, "_rejected_fitnesses", None)
            if rejected_fitnesses is None:
                rejected_fitnesses = []
                self._rejected_fitnesses = rejected_fitnesses
            rejected_fitnesses.append(float(fitness))
            hopeless = (
                len(rejected_fitnesses) >= 4
                and max(rejected_fitnesses[-4:]) < 0.30
            )
            if hopeless:
                # Four mature-map attempts with no coarse geometric overlap are
                # not a near miss. Continuing expensive 60-90 s ICP solves
                # starves camera/map delivery and cannot safely recover this
                # frame mismatch. Preserve live-map-only navigation and leave
                # the premap visible as an unaligned reference.
                self._failed_attempts = self.config.max_failed_attempts
            logger.warning(
                f"relocalize rejected: fitness={fitness:.3f} < threshold="
                f"{self.config.fitness_threshold} time_cost={dt:.1f}s "
                f"n_pts={n_pts} candidate_t={T[:3, 3].round(3).tolist()} "
                f"attempt={self._failed_attempts}/{self.config.max_failed_attempts}"
                f"{' (aborting hopeless alignment)' if hopeless else ''}"
            )
            return None

        T_inv = np.linalg.inv(T)
        new_tf = Transform(
            translation=Vector3(*T_inv[:3, 3]),
            rotation=Quaternion.from_rotation_matrix(T_inv[:3, :3]),
            frame_id=FRAME_WORLD,
            child_frame_id=FRAME_MAP,
        )
        logger.info(
            f"relocalize: fitness={fitness:.3f} "
            f"time_cost={dt:.1f}s n_pts={n_pts} "
            f"reloc_t={T[:3, 3].round(3).tolist()} "
            f"TF {FRAME_WORLD!r} -> {FRAME_MAP!r} "
            f"published_t={T_inv[:3, 3].round(3).tolist()}"
        )
        # _relocalize(premap, live) places the live (session WORLD) cloud into
        # the premap (MAP) frame, so p_map = T @ p_world: T itself is the
        # world->map transform. Keep it (the published TF is its inverse). This
        # is what world_to_map_2d exposes for persistent-tag reprojection.
        self._accepted_world_to_map = np.asarray(T).copy()
        self._alignment_locked = True
        return new_tf

    @rpc
    def world_to_map_2d(self) -> dict | None:
        """Return the locked world->map transform projected onto the 2D plane.

        Applying the returned transform maps SESSION WORLD coordinates into the
        persistent MAP frame:

            map_x = cos(yaw) * world_x - sin(yaw) * world_y + x
            map_y = sin(yaw) * world_x + cos(yaw) * world_y + y

        Direction, from first principles: ``_relocalize(premap, live)`` returns
        T that places the live cloud (session WORLD frame) into the premap (MAP
        frame), i.e. p_map = T @ p_world, so T IS the world->map transform and
        is what is stored at accept time. The stack publishes its inverse as the
        world->map TF (frame_id=world, child_frame_id=map) only because a
        ROS-style TF stores the child frame's pose in the parent frame (it maps
        map coordinates back to world), which confirms the direction. Returns
        None until ICP has locked an alignment.
        """
        if not self._alignment_locked:
            return None
        transform = self._accepted_world_to_map
        if transform is None:
            return None
        yaw = math.atan2(float(transform[1, 0]), float(transform[0, 0]))
        return {
            "x": float(transform[0, 3]),
            "y": float(transform[1, 3]),
            "yaw": yaw,
        }

    @rpc
    def relocalization_status(self) -> dict[str, Any]:
        configured = bool(self.config.map_file) and self._premap is not None
        failed = int(self._failed_attempts)
        return {
            "configured": configured,
            "locked": bool(self._alignment_locked),
            "failed_attempts": failed,
            "exhausted": bool(
                configured
                and not self._alignment_locked
                and failed >= self.config.max_failed_attempts
            ),
        }


class NightwatchMapRecorderConfig(RecorderConfig):
    """A per-run mapping database; an existing run is backed up on startup."""

    db_path: str | Path = "assets/output/maps/nightwatch_map.db"
    # Each lidar observation is already stored with the current world pose, and
    # odometry is recorded separately. The raw transform tree arrives around
    # 50-60 Hz on the Go2; persisting it adds SQLite/serialization pressure but
    # no information needed by the map exporter.
    record_tf: bool = False
    # The live mapper still receives the bounded 3 Hz LiDAR stream. Recording
    # needs much less: the exporter spatially deduplicated 82.5% of a live
    # mapping run, so 1 Hz clouds and 5 Hz trajectory retain reconstruction
    # fidelity without producing ~600 MB every nine minutes.
    lidar_record_interval_s: float = 1.0
    odom_record_interval_s: float = 0.2


class NightwatchMapRecorder(Recorder):
    """Record the minimum data needed to reconstruct/export an occupancy map.

    Camera frames are intentionally excluded: semantic images already live in
    :class:`NightwatchSpatialMemory`, and writing raw 720p RGB at camera rate
    would recreate the I/O pressure that delayed the live feed.
    """

    config: NightwatchMapRecorderConfig
    lidar: In[PointCloud2]
    odom: In[PoseStamped]

    _last_odom_pose: Pose | None = None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._nightwatch_recorder_stopped = False
        self._recording_disposables = CompositeDisposable()
        self._last_recorded_ts: dict[str, float] = {}
        Path(self.config.db_path).parent.mkdir(parents=True, exist_ok=True)

    @rpc
    def start(self) -> None:
        self._nightwatch_recorder_stopped = False
        self._recording_disposables = CompositeDisposable()
        self._last_recorded_ts = {}
        super().start()

    def _port_to_stream(self, name: str, input_topic: In[Any], stream: Any) -> None:
        """Persist at reconstruction cadence; never throttle the live mapper."""
        interval = (
            self.config.lidar_record_interval_s
            if name == "lidar"
            else self.config.odom_record_interval_s
            if name == "odom"
            else 0.0
        )

        async def on_msg(msg: Any) -> None:
            ts = self._resolve_ts(name, msg)
            previous = self._last_recorded_ts.get(name)
            if previous is not None and ts - previous < interval:
                return
            pose = await self._resolve_pose(name, msg, ts)
            if not pose:
                logger.warning(
                    "[%s] No pose for time %s (msg ts: %s), storing without pose",
                    name,
                    ts,
                    getattr(msg, "ts", None),
                )
            stream.append(msg, ts=ts, pose=pose)
            self._last_recorded_ts[name] = ts

        self.process_observable(input_topic.pure_observable(), on_msg)

    def process_observable(self, observable: Any, async_cb: Any) -> Any:
        """Own recorder callbacks separately so they stop before SQLite.

        ``MemoryModule.store`` registers its SQLite resource before Recorder
        registers its input subscriptions. CompositeResource consequently
        closes SQLite first during shutdown, while a latest-message callback
        can still be queued on the module loop. Keep these subscriptions in a
        separate group and drain cancellation before delegating to the normal
        resource teardown.
        """
        on_msg, dispatcher = self._make_async_dispatch(async_cb)
        subscription = observable.subscribe(on_msg)
        disposable = CompositeDisposable(subscription, dispatcher)
        self._recording_disposables.add(disposable)
        return disposable

    @rpc
    def stop(self) -> None:
        if self._nightwatch_recorder_stopped:
            logger.info("Ignoring duplicate map recorder stop")
            return
        self._nightwatch_recorder_stopped = True

        self._recording_disposables.dispose()
        loop = self._loop
        if loop is not None and loop.is_running():
            # Cancellation is scheduled thread-safely. This barrier runs after
            # it, and after any synchronous SQLite append already in progress.
            try:
                asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=2.0)
            except Exception:
                logger.warning("Timed out draining recorder callbacks during stop")
        super().stop()

    @pose_setter_for("odom")
    async def _odom_pose(self, msg: PoseStamped) -> Pose | None:
        self._last_odom_pose = msg
        return self._last_odom_pose

    @pose_setter_for("lidar")
    async def _lidar_pose(self, _msg: PointCloud2) -> Pose | None:
        # Go2's cloud is already expressed in world, but the map exporter also
        # uses this pose as the robot trajectory anchor.
        return self._last_odom_pose
