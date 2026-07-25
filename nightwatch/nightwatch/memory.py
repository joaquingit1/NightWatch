"""Persistent semantic and geometric memory for the Nightwatch scout."""

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any

import numpy as np
import reactivex as rx
from reactivex.disposable import CompositeDisposable

from dimos.core.core import rpc
from dimos.core.stream import In, Out
from dimos.mapping.relocalization.module import (
    FRAME_MAP,
    FRAME_WORLD,
    MAP_SUFFIX,
    RelocalizationModule,
)
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
from nightwatch.planar_relocalize import relocalize_planar

logger = setup_logger()

_RELOCALIZATION_STATE_MAX_AGE_S = 12.0 * 60.0 * 60.0
_RELOCALIZATION_ODOM_PROXIMITY_M = 4.0
# A power-cycled Go2 restarts odometry at position ~(0, 0) and yaw ~0, so
# positional proximity alone cannot rule out a physically moved WORLD frame
# when the previous session died near its boot spot. Require heading
# continuity too whenever both sides recorded it (mirrors
# nightwatch/navigation.py _ODOM_EPOCH_YAW_TOLERANCE_RAD).
_RELOCALIZATION_ODOM_YAW_TOLERANCE_RAD = 0.7
# A restored alignment is a hypothesis carried over from a previous session.
# Once the live map is informative enough, verify it against the planar
# matcher; a disagreement beyond these tolerances proves the WORLD frame
# moved (undetected odometry reset) and the fresh match wins.
_RESTORE_VERIFY_TRANSLATION_TOLERANCE_M = 0.75
_RESTORE_VERIFY_YAW_TOLERANCE_RAD = 0.15
_RESTORE_VERIFY_MAX_ATTEMPTS = 10


def _wrap_angle(angle: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    wrapped = math.fmod(angle + math.pi, 2.0 * math.pi)
    if wrapped <= 0.0:
        wrapped += 2.0 * math.pi
    return wrapped - math.pi


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Relocalization can publish the accepted TF from two callbacks at nearly
    # the same time. A fixed ``.tmp`` name lets one callback rename the other
    # callback's file, after which the second rename fails. Give every writer a
    # same-directory temporary file so the final replace remains atomic.
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _transform_from_2d(x: float, y: float, yaw: float) -> np.ndarray:
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    transform = np.eye(4)
    transform[:3, :3] = np.array(
        [
            [cos_yaw, -sin_yaw, 0.0],
            [sin_yaw, cos_yaw, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    transform[:3, 3] = [x, y, 0.0]
    return transform


def _transform_to_tf(transform: np.ndarray) -> Transform:
    transform_inv = np.linalg.inv(transform)
    return Transform(
        translation=Vector3(*transform_inv[:3, 3]),
        rotation=Quaternion.from_rotation_matrix(transform_inv[:3, :3]),
        frame_id=FRAME_WORLD,
        child_frame_id=FRAME_MAP,
    )


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
    supervisor recognize an already-mapped floor right after alignment locks.

    One venue-hardening change on top of stock:
    - The premap is published to the viewer immediately (not only after the
      first lock), so an operator sees the remembered floor at startup.

    The stock 3-D global registration is not used at startup. On indoor maps it
    spent 40-90 seconds comparing rotationally symmetric floor/ceiling planes
    and repeatedly proposed the same wrong room. A fast planar matcher instead
    searches the whole venue at every heading, validates overlap in both
    directions, and rejects ambiguous repeated-room hypotheses. While a match
    is ambiguous, exploration continues on the live map and retries in the
    background as the observed footprint becomes more distinctive.
    """

    # The stock relocalizer waits for 50k points, but this stack deliberately
    # runs a bounded, replace-window voxel mapper to keep the camera and control
    # loop responsive. At the current venue the authoritative rolling window
    # plateaus around 28,970 points, so even the former 30k Nightwatch gate
    # could never open. 25k remains a substantial geometric sample; the planar
    # matcher's bidirectional-overlap and runner-up-margin gates (not point
    # count) are the protections against repeated-room false locks.
    MIN_NIGHTWATCH_LOCAL_POINTS = 25_000
    # Visualization-only copy transformed into the current session's WORLD
    # frame. Navigation and the web map keep consuming canonical ``loaded_map``
    # in the persistent MAP frame.
    aligned_loaded_map: Out[PointCloud2]
    # Rerun-only provisional copy whose coordinates are deliberately attached
    # to WORLD so it remains visible before the MAP TF exists.
    preview_loaded_map: Out[PointCloud2]
    # Current WORLD-frame pose used only to prove an odometry epoch survived a
    # short stack restart before restoring a previously accepted transform.
    odom: In[PoseStamped]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # The accepted world->map transform (session WORLD -> persistent MAP),
        # kept in full 4x4 form so world_to_map_2d can project it to the plane.
        self._accepted_world_to_map: np.ndarray | None = None
        self._latest_odom_pose: tuple[float, float] | None = None
        self._premap_path: Path | None = None
        self._premap_digest: str | None = None
        self._premap_preview_world: PointCloud2 | None = None
        odom_epoch_path = Path(
            os.getenv(
                "NIGHTWATCH_ODOM_EPOCH_PATH",
                "assets/output/maps/nightwatch_odom_epoch.json",
            )
        )
        self._odom_epoch_path = odom_epoch_path
        self._alignment_state_path = Path(
            os.getenv("NIGHTWATCH_RELOCALIZATION_STATE_PATH")
            or odom_epoch_path.with_name("nightwatch_relocalization.json")
        )
        self._restore_rejection: str | None = None
        self._alignment_persisted = False
        self._restore_verification_pending = False
        self._restore_verify_attempts = 0

    @rpc
    def start(self) -> None:
        map_file = self.config.map_file
        if map_file:
            try:
                path = resolve_named_path(map_file, MAP_SUFFIX)
                encoded = path.read_bytes()
                PointCloud2.lcm_decode(encoded)
                self._premap_path = path.resolve()
                self._premap_digest = hashlib.sha256(encoded).hexdigest()
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
        if self._premap is not None:
            self._premap_preview_world = PointCloud2.from_numpy(
                self._premap.points_f32(),
                frame_id=FRAME_WORLD,
                timestamp=self._premap.ts,
            )
            self.register_disposable(
                self.odom.observable().subscribe(self._on_relocalization_odometry)
            )
        # Stock publishes loaded_map only after the first successful lock
        # (its interval stream gates on the world->map TF). Show the
        # remembered floor right away instead; once alignment locks, the
        # stock periodic publisher takes over.
        if self._premap is not None and self.config.publish_loaded_map:
            # Do not make the viewer wait for the first two-second interval.
            # This raw MAP-frame copy is display-only; merged navigation
            # remains gated on a trusted world->map transform in the parent.
            self._publish_premap_prelock(0)
            self.register_disposable(
                rx.interval(2.0).subscribe(self._publish_premap_prelock)
            )
        initial_pose = os.getenv("NIGHTWATCH_INITIAL_POSE_2D", "").strip()
        if initial_pose:
            try:
                x_text, y_text, yaw_text = (
                    part.strip() for part in initial_pose.split(",")
                )
                self.set_initial_pose_2d(
                    float(x_text),
                    float(y_text),
                    float(yaw_text),
                )
            except Exception:
                logger.exception(
                    "NIGHTWATCH_INITIAL_POSE_2D must be 'x,y,yaw_radians'; "
                    "automatic relocalization remains active"
                )

    def _publish_premap_prelock(self, _tick: int) -> None:
        if self._alignment_locked or self._premap is None:
            return
        try:
            self.loaded_map.publish(self._premap)
            preview = getattr(self, "_premap_preview_world", None)
            if preview is not None:
                self.preview_loaded_map.publish(preview)
        except Exception:
            logger.exception("premap preview publish failed")

    def _publish_aligned_premap(self, map_in_world: Transform) -> None:
        if getattr(self, "_premap", None) is None:
            return
        try:
            self.aligned_loaded_map.publish(self._premap.transform(map_in_world))
        except Exception:
            logger.exception("aligned premap viewer publish failed")

    def _publish_periodic(self, pair: tuple[int, Transform]) -> None:
        """Publish canonical and Rerun-ready saved-map layers."""
        super()._publish_periodic(pair)
        _, map_in_world = pair
        self._publish_aligned_premap(map_in_world)

    def _publish_tf(self, tf: Transform | None) -> None:
        """Publish a newly accepted/restored alignment without a 2 s delay."""
        if tf is None:
            return
        super()._publish_tf(tf)
        try:
            self.tf.publish(tf.now())
            # Replace the provisional raw-coordinate layer with an empty frame
            # as soon as the authoritative blue aligned layer is available.
            self.preview_loaded_map.publish(
                PointCloud2.from_numpy(
                    np.empty((0, 3), dtype=np.float32),
                    frame_id=FRAME_WORLD,
                    timestamp=time.time(),
                )
            )
        except AttributeError:
            # A few unit tests exercise the RPC on a deliberately bare object;
            # deployed modules always own the inherited TF publisher.
            pass
        self._publish_aligned_premap(tf)
        self._persist_accepted_alignment()

    def _on_relocalization_odometry(self, msg: PoseStamped) -> None:
        try:
            pose = (float(msg.position.x), float(msg.position.y))
        except Exception:
            logger.exception("relocalization odometry payload malformed")
            return
        yaw: float | None
        try:
            euler = msg.orientation.to_euler()
            yaw = float(euler.z)
            if not math.isfinite(yaw):
                yaw = None
        except Exception:
            yaw = None
        self._latest_odom_pose = pose
        if not self._alignment_locked:
            self._try_restore_accepted_alignment(pose, yaw=yaw)
        elif not self._alignment_persisted:
            # Operator initial pose can be accepted before the first odometry
            # sample. Persist it as soon as continuity can be proven.
            self._persist_accepted_alignment()

    def _current_epoch(
        self,
        pose: tuple[float, float],
        *,
        yaw: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        current_time = time.time() if now is None else float(now)
        try:
            data = json.loads(self._odom_epoch_path.read_text(encoding="utf-8"))
            last_pose = (
                float(data["last_pose"][0]),
                float(data["last_pose"][1]),
            )
            updated_at = float(data["updated_at"])
            epoch_id = str(data["epoch_id"])
            raw_yaw = data.get("last_yaw")
            last_yaw = float(raw_yaw) if raw_yaw is not None else None
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        except Exception:
            logger.exception(
                "relocalization odometry epoch read failed",
                path=str(self._odom_epoch_path),
            )
            return None
        age = current_time - updated_at
        if not 0.0 <= age <= _RELOCALIZATION_STATE_MAX_AGE_S:
            return None
        if (
            math.hypot(pose[0] - last_pose[0], pose[1] - last_pose[1])
            > _RELOCALIZATION_ODOM_PROXIMITY_M
        ):
            return None
        if (
            yaw is not None
            and last_yaw is not None
            and abs(_wrap_angle(yaw - last_yaw))
            > _RELOCALIZATION_ODOM_YAW_TOLERANCE_RAD
        ):
            return None
        return {
            "epoch_id": epoch_id,
            "updated_at": updated_at,
            "last_pose": last_pose,
        }

    def _persist_accepted_alignment(self) -> None:
        transform = getattr(self, "_accepted_world_to_map", None)
        pose = getattr(self, "_latest_odom_pose", None)
        premap_path = getattr(self, "_premap_path", None)
        premap_digest = getattr(self, "_premap_digest", None)
        if (
            transform is None
            or pose is None
            or premap_path is None
            or premap_digest is None
        ):
            return
        epoch = self._current_epoch(pose)
        if epoch is None:
            return
        yaw = math.atan2(float(transform[1, 0]), float(transform[0, 0]))
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "map_path": str(premap_path),
            "map_sha256": premap_digest,
            "odom_epoch": epoch["epoch_id"],
            "odom_pose": [pose[0], pose[1]],
            "world_to_map": {
                "x": float(transform[0, 3]),
                "y": float(transform[1, 3]),
                "yaw": yaw,
            },
        }
        try:
            _atomic_write_json(self._alignment_state_path, payload)
            self._alignment_persisted = True
            logger.info(
                "accepted world-to-map alignment persisted",
                epoch=str(epoch["epoch_id"])[:8],
                path=str(self._alignment_state_path),
            )
        except Exception:
            logger.exception(
                "accepted world-to-map persistence failed",
                path=str(self._alignment_state_path),
            )

    def _try_restore_accepted_alignment(
        self,
        pose: tuple[float, float],
        *,
        yaw: float | None = None,
        now: float | None = None,
    ) -> bool:
        current_time = time.time() if now is None else float(now)
        try:
            state = json.loads(
                self._alignment_state_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return False
        except Exception:
            self._log_restore_rejection("saved transform is unreadable")
            return False

        reason: str | None = None
        premap_path = self._premap_path
        try:
            version = int(state.get("version", 0))
        except (TypeError, ValueError):
            version = 0
        if version != 1:
            reason = "saved transform version is unsupported"
        elif premap_path is None or str(premap_path) != str(state.get("map_path", "")):
            reason = "saved transform belongs to a different venue map path"
        elif getattr(self, "_premap_digest", None) != state.get("map_sha256"):
            reason = "venue map contents changed since transform acceptance"
        else:
            try:
                saved_age = current_time - float(state["saved_at"])
            except (KeyError, TypeError, ValueError):
                saved_age = float("inf")
            if not 0.0 <= saved_age <= _RELOCALIZATION_STATE_MAX_AGE_S:
                reason = "saved transform is too old"

        epoch = self._current_epoch(pose, yaw=yaw, now=current_time)
        if reason is None and epoch is None:
            # The navigation worker may not have created/refreshed the shared
            # epoch file yet. Keep trying on later odometry messages.
            return False
        if reason is None and str(state.get("odom_epoch", "")) != epoch["epoch_id"]:
            reason = "odometry epoch changed"

        try:
            values = state["world_to_map"]
            x, y, yaw = (
                float(values["x"]),
                float(values["y"]),
                float(values["yaw"]),
            )
            finite = all(math.isfinite(value) for value in (x, y, yaw))
        except (KeyError, TypeError, ValueError):
            finite = False
            x = y = yaw = 0.0
        if reason is None and not finite:
            reason = "saved transform is malformed"

        if reason is not None:
            self._log_restore_rejection(reason)
            return False

        transform = _transform_from_2d(x, y, yaw)
        self._accepted_world_to_map = transform
        self._alignment_locked = True
        self._failed_attempts = 0
        # The restore is a carried-over hypothesis: schedule a one-shot live
        # verification against the planar matcher once the local map is
        # informative enough (see _verify_restored_alignment).
        self._restore_verification_pending = True
        self._restore_verify_attempts = 0
        tf = _transform_to_tf(transform)
        # This immediately unlocks merged-map navigation and creates the
        # aligned Rerun layer; neither waits for the periodic publisher.
        self._publish_tf(tf)
        logger.warning(
            "accepted world-to-map alignment restored",
            epoch=str(epoch["epoch_id"])[:8],
            x=round(x, 3),
            y=round(y, 3),
            yaw=round(yaw, 3),
        )
        return True

    def _log_restore_rejection(self, reason: str) -> None:
        if getattr(self, "_restore_rejection", None) == reason:
            return
        self._restore_rejection = reason
        logger.warning("saved world-to-map alignment not restored", reason=reason)


    def _has_enough_points(self, msg: PointCloud2) -> bool:
        return len(msg) >= self.MIN_NIGHTWATCH_LOCAL_POINTS

    def _should_attempt(self, msg: PointCloud2) -> bool:
        """Allow bounded planar attempts to verify a restored alignment.

        The stock gate stops all matching once locked. A RESTORED lock is a
        hypothesis from a previous session; keep the matcher eligible (at the
        normal retry cadence, with its own attempt budget) until the live map
        either confirms or overturns it.
        """
        if self._alignment_locked and getattr(
            self, "_restore_verification_pending", False
        ):
            if (
                getattr(self, "_restore_verify_attempts", 0)
                >= _RESTORE_VERIFY_MAX_ATTEMPTS
            ):
                return False
            now = time.monotonic()
            if now - self._last_attempt_at < self.config.retry_interval_s:
                return False
            self._last_attempt_at = now
            return True
        return super()._should_attempt(msg)

    def _verify_restored_alignment(self, msg: PointCloud2) -> Transform | None:
        """Confirm or overturn a restored world->map transform live.

        Agreement (within tolerance) keeps the restored transform so the map
        never shifts under active navigation for a sub-tolerance refinement.
        Disagreement means the session WORLD frame is not the one the
        transform was saved in (typically an undetected odometry reset after
        a power cycle): adopt the fresh match, republish the TF, and
        re-persist, so zones/areas snap back to their true positions.
        """
        assert self._premap is not None
        self._restore_verify_attempts = (
            getattr(self, "_restore_verify_attempts", 0) + 1
        )
        try:
            result = relocalize_planar(self._premap.pointcloud, msg.pointcloud)
        except Exception:
            logger.exception("restored-alignment verification failed")
            result = None
        if result is None:
            if self._restore_verify_attempts >= _RESTORE_VERIFY_MAX_ATTEMPTS:
                self._restore_verification_pending = False
                logger.warning(
                    "restored world-to-map alignment could not be verified "
                    "against the live map; keeping it (planar matcher stayed "
                    "ambiguous)"
                )
            return None
        fresh = np.asarray(result.transform)
        restored = self._accepted_world_to_map
        if restored is None:
            # An operator pose or live lock replaced the restore concurrently.
            self._restore_verification_pending = False
            return None
        translation_delta = float(
            np.hypot(
                fresh[0, 3] - restored[0, 3],
                fresh[1, 3] - restored[1, 3],
            )
        )
        yaw_delta = abs(
            _wrap_angle(
                math.atan2(float(fresh[1, 0]), float(fresh[0, 0]))
                - math.atan2(float(restored[1, 0]), float(restored[0, 0]))
            )
        )
        self._restore_verification_pending = False
        if (
            translation_delta <= _RESTORE_VERIFY_TRANSLATION_TOLERANCE_M
            and yaw_delta <= _RESTORE_VERIFY_YAW_TOLERANCE_RAD
        ):
            logger.info(
                "restored world-to-map alignment verified live",
                translation_delta_m=round(translation_delta, 3),
                yaw_delta_rad=round(yaw_delta, 3),
            )
            return None
        logger.warning(
            "restored world-to-map alignment overturned by live match; "
            "adopting the fresh transform",
            translation_delta_m=round(translation_delta, 3),
            yaw_delta_rad=round(yaw_delta, 3),
        )
        self._accepted_world_to_map = fresh.copy()
        self._alignment_persisted = False
        return _transform_to_tf(fresh)

    def _maybe_log_skip(self, msg: PointCloud2) -> None:
        if self._has_enough_points(msg):
            return
        now = time.monotonic()
        if now - self._last_skip_log > 5.0:
            logger.warning(
                "relocalize skipped: "
                f"n_pts={len(msg)} < "
                f"MIN_NIGHTWATCH_LOCAL_POINTS={self.MIN_NIGHTWATCH_LOCAL_POINTS}"
            )
            self._last_skip_log = now

    def _try_relocalize(self, msg: PointCloud2) -> Transform | None:
        if self._alignment_locked and getattr(
            self, "_restore_verification_pending", False
        ):
            return self._verify_restored_alignment(msg)
        assert self._premap is not None
        t0 = time.monotonic()
        try:
            result = relocalize_planar(
                self._premap.pointcloud,
                msg.pointcloud,
            )
        except Exception:
            logger.exception("planar relocalization failed")
            self._failed_attempts += 1
            return None
        dt = time.monotonic() - t0
        n_pts = len(msg)

        if result is None:
            self._failed_attempts += 1
            logger.warning(
                "planar relocalization ambiguous; continuing live-map "
                f"exploration time_cost={dt:.1f}s n_pts={n_pts} "
                f"attempt={self._failed_attempts}/{self.config.max_failed_attempts}"
            )
            return None

        T = result.transform
        T_inv = np.linalg.inv(T)
        new_tf = _transform_to_tf(T)
        logger.info(
            f"planar relocalize: score={result.score:.3f} "
            f"overlap={result.overlap:.3f} "
            f"margin={result.score - result.runner_up_score:.3f} "
            f"time_cost={dt:.1f}s n_pts={n_pts} "
            f"reloc_t={T[:3, 3].round(3).tolist()} "
            f"TF {FRAME_WORLD!r} -> {FRAME_MAP!r} "
            f"published_t={T_inv[:3, 3].round(3).tolist()}"
        )
        # relocalize_planar(premap, live) places the live (session WORLD) cloud
        # into the premap (MAP) frame, so p_map = T @ p_world: T itself is the
        # world->map transform. Keep it (the published TF is its inverse).
        self._accepted_world_to_map = np.asarray(T).copy()
        self._alignment_locked = True
        return new_tf

    @rpc
    def set_initial_pose_2d(self, x: float, y: float, yaw: float) -> dict[str, Any]:
        """Apply an operator-confirmed initial pose in the persistent map.

        Symmetric indoor rooms can make global point-cloud registration
        genuinely ambiguous. Robotics consoles therefore need the standard
        "set initial pose" escape hatch: ``x``, ``y`` and ``yaw`` describe the
        transform from this boot's WORLD frame into the selected venue's MAP
        frame. The same lock and TF publication path as automatic
        relocalization is used, so saved zones and semantic observations
        immediately share one coordinate frame.

        This deliberately is not persisted. WORLD resets when the robot boots
        or the localization source restarts, so replaying a prior session's
        transform would silently place the dog in the wrong room.
        """
        values = (float(x), float(y), float(yaw))
        if not all(math.isfinite(value) for value in values):
            raise ValueError("initial pose x, y and yaw must be finite")

        transform = _transform_from_2d(*values)
        tf = _transform_to_tf(transform)
        self._accepted_world_to_map = transform
        self._alignment_locked = True
        self._failed_attempts = 0
        # An operator-confirmed pose is authoritative; cancel any pending
        # verification of a previously restored transform.
        self._restore_verification_pending = False
        self._publish_tf(tf)
        logger.warning(
            "operator initial pose accepted: "
            f"world_to_map=({values[0]:.3f}, {values[1]:.3f}, "
            f"{values[2]:.3f} rad)"
        )
        return {
            "accepted": True,
            "x": values[0],
            "y": values[1],
            "yaw": values[2],
        }

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
            "verified": bool(
                self._alignment_locked
                and not getattr(self, "_restore_verification_pending", False)
            ),
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
