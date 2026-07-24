"""Persistent semantic world model and experience memory.

This module deliberately samples perception. Navigation and person following
must consume fresh camera frames first; semantic cataloguing may be slower and
must never build a queue behind the camera callback.
"""

from __future__ import annotations

from collections import Counter, deque
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Protocol
import uuid

from dimos_lcm.std_msgs import Bool
from pydantic import Field
from reactivex.disposable import Disposable

from dimos.agents.annotation import skill
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT, DIMOS_PROJECT_ROOT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.memory2.store.sqlite import SqliteStore
from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.Path import Path as NavPath
from dimos.msgs.sensor_msgs.Image import Image
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.msgs.vision_msgs.Detection3DArray import Detection3DArray
from dimos.msgs.visualization_msgs.EntityMarkers import EntityMarkers, Marker
from dimos.spec.utils import Spec
from dimos.types.robot_location import RobotLocation
from dimos.utils.logging_config import setup_logger
from nightwatch.contracts import BehaviorKind, RobotActivity
from nightwatch.vision import VisionSpec

logger = setup_logger()

_TRANSLATIONAL_BEHAVIORS = frozenset(
    {
        BehaviorKind.EXPLORE,
        BehaviorKind.PATROL,
        BehaviorKind.RETURN_HOME,
        BehaviorKind.FOLLOW,
        BehaviorKind.INTERVENE,
        BehaviorKind.ESCORT,
        BehaviorKind.EXPLICIT_TASK,
        BehaviorKind.MANUAL,
    }
)

_SPATIAL_NAVIGATION_FAILURES = frozenset(
    {
        "stuck",
        "coverage_loop",
        "repeated_goal",
    }
)

_AREA_TYPES = (
    "sleeping_area",
    "workspace",
    "kitchen",
    "lounge",
    "restroom",
    "corridor",
    "entrance",
    "storage",
    "open_common_area",
    "unknown",
)

_OBJECT_AREA_HINTS: dict[str, str] = {
    "tent": "sleeping_area",
    "sleeping bag": "sleeping_area",
    "camp bed": "sleeping_area",
    "cot": "sleeping_area",
    "bed": "sleeping_area",
    "pillow": "sleeping_area",
    "blanket": "sleeping_area",
    "desk": "workspace",
    "laptop": "workspace",
    "monitor": "workspace",
    "office chair": "workspace",
    "refrigerator": "kitchen",
    "microwave": "kitchen",
    "sink": "kitchen",
    "couch": "lounge",
    "sofa": "lounge",
    "toilet": "restroom",
}

_SLEEPING_CUES = (
    "sleeping bag",
    "camp bed",
    "pillow",
    "blanket",
    "tent",
    "cot",
    "bed",
)


def _sleeping_cue(label: str) -> str | None:
    """Normalize detector variants such as ``camping tent`` to one cue."""
    normalized = " ".join(str(label).strip().lower().replace("_", " ").split())
    return next((cue for cue in _SLEEPING_CUES if cue in normalized), None)


def _object_area_hint(label: str) -> str | None:
    normalized = " ".join(str(label).strip().lower().replace("_", " ").split())
    direct = _OBJECT_AREA_HINTS.get(normalized)
    if direct is not None:
        return direct
    if _sleeping_cue(normalized) is not None:
        return "sleeping_area"
    return None

_DEFAULT_OBJECT_PROMPTS = [
    "tent",
    "camping tent",
    "sleeping bag",
    "camp bed",
    "cot",
    "bed",
    "pillow",
    "blanket",
    "desk",
    "office chair",
    "laptop",
    "monitor",
    "table",
    "couch",
    "refrigerator",
    "microwave",
    "sink",
    "toilet",
    "door",
    "exit sign",
    "first aid kit",
    "fire extinguisher",
    "charging station",
]


class SpatialTagSpec(Spec, Protocol):
    def tag_location(self, robot_location: RobotLocation) -> bool: ...
    # Match SpatialMemory's public annotation exactly; DimOS uses strict
    # structural annotation checks when binding cross-worker ModuleRefs.
    def query_by_text(self, text: str, limit: int = 5) -> list[dict]: ...  # type: ignore[type-arg]


class ExplorationFeedbackSpec(Spec, Protocol):
    def report_navigation_failure(
        self, x: float, y: float, reason: str = "navigation failure"
    ) -> bool: ...

    def coverage_status(self) -> dict[str, Any]: ...


class PatrolFeedbackSpec(Spec, Protocol):
    def report_patrol_failure(
        self, x: float, y: float, reason: str = "stuck"
    ) -> bool: ...


class RelocSpec(Spec, Protocol):
    # Structural bind to NightwatchRelocalization: returns the locked world->map
    # transform (session WORLD -> persistent MAP) projected to 2D, or None while
    # unlocked. Mirrors curiosity.py's RelocSpec binding.
    def world_to_map_2d(self) -> dict | None: ...  # type: ignore[type-arg]


# The world->map transform is stable once ICP locks, so poll the reloc worker
# at most this often instead of once per row during backfill/lookup.
_WORLD_TO_MAP_CACHE_S = 5.0


class WorldModelConfig(ModuleConfig):
    enabled: bool = True
    experience_db_path: str = "assets/output/memory/nightwatch_experience.db"
    world_db_path: str = "assets/output/memory/nightwatch_world.sqlite3"
    keyframe_min_period_s: float = 15.0
    keyframe_min_distance_m: float = 1.0
    # Image.sharpness is normalized to [0, 1].
    keyframe_min_sharpness: float = 0.20
    semantic_interval_s: float = 20.0
    semantic_min_distance_m: float = 1.25
    # Optional semantic perception must fail open: a missing model dependency
    # must not create a hot retry loop that competes with navigation/camera I/O.
    semantic_failure_cooldown_s: float = 300.0
    area_vlm_enabled: bool = True
    area_vlm_failure_cooldown_s: float = 300.0
    area_radius_m: float = 2.75
    # Two areas of the same resolved type whose centers fall within this
    # distance are the same place and are merged into one. Left unset it
    # defaults to 2.0 * area_radius_m, so one large open room scanned across
    # several area_radius_m hops collapses back into a single space instead of
    # fragmenting into workspace_1, workspace_2, ...
    area_merge_radius_m: float | None = None
    object_prompts: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_OBJECT_PROMPTS)
    )
    object_min_evidence: int = 3
    marker_names: dict[int, str] = Field(
        default_factory=lambda: {
            0: "checkpoint_0",
            1: "rest_area",
            2: "sleeping_area",
        }
    )
    marker_min_evidence: int = 2
    stuck_window_s: float = 8.0
    stuck_displacement_m: float = 0.12
    loop_window_s: float = 30.0
    loop_path_m: float = 3.0
    loop_displacement_m: float = 0.8
    failure_cooldown_s: float = 30.0


class NightwatchWorldModel(Module):
    """Self-tags areas, remembers objects, and records explainable failures."""

    dedicated_worker = True
    config: WorldModelConfig

    color_image: In[Image]
    lidar: In[PointCloud2]
    odom: In[PoseStamped]
    detections: In[Detection3DArray]  # fiducial marker detections
    activity: In[RobotActivity]
    goal_request: In[PoseStamped]
    goal_reached: In[Bool]
    # Rendered by the Rerun bridge with zero extra wiring: EntityMarkers
    # becomes labeled colored dots (world/tags) and Path a line strip
    # (world/trail) in the existing 3D view. This is what makes self-tagged
    # areas, AprilTag anchors, and remembered objects VISIBLE on the map.
    tags: Out[EntityMarkers]
    trail: Out[NavPath]

    _vision: VisionSpec
    _spatial_memory: SpatialTagSpec
    _explore: ExplorationFeedbackSpec
    _patrol_feedback: PatrolFeedbackSpec
    _reloc: RelocSpec

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._semantic_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._latest_image: Image | None = None
        self._latest_lidar: PointCloud2 | None = None
        self._latest_odom: PoseStamped | None = None
        self._latest_activity: RobotActivity | None = None
        # Monotonic start of the current uninterrupted translational-motion
        # epoch.  Odom collected while booting, expressing, holding, or under
        # another non-translational owner must never be reused to declare a
        # newly started explorer/patrol stuck.
        self._motion_expected_since: float | None = None
        self._last_camera_wall = 0.0
        self._last_keyframe_at = 0.0
        self._last_keyframe_xy: tuple[float, float] | None = None
        self._last_semantic_at = 0.0
        self._last_semantic_xy: tuple[float, float] | None = None
        self._odom_history: deque[tuple[float, float, float]] = deque(maxlen=600)
        self._goal_history: deque[tuple[float, float, float]] = deque(maxlen=100)
        self._failure_last: dict[str, float] = {}
        self._marker_counts: Counter[int] = Counter()
        self._detector: Any = None
        self._detector_retry_after = 0.0
        self._detector_error: str | None = None
        self._area_vlm_retry_after = 0.0
        self._area_vlm_error: str | None = None
        self._experience_store: SqliteStore | None = None
        self._experience_images: Any = None
        self._experience_events: Any = None
        self._semantic_events: Any = None
        self._db: sqlite3.Connection | None = None
        # Area/object rows created or re-observed this session, so their
        # center_x/center_y are in the CURRENT world frame. Rows not in these
        # sets came from an earlier boot and are only usable via map coords.
        self._session_area_ids: set[str] = set()
        self._session_object_ids: set[str] = set()
        self._session_id = uuid.uuid4().hex
        self._world_to_map_cache: tuple[float, float, float] | None = None
        self._world_to_map_cache_until = 0.0

    @staticmethod
    def _resolve_path(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else DIMOS_PROJECT_ROOT / path

    @rpc
    def start(self) -> None:
        super().start()
        if not self.config.enabled:
            logger.info("NightwatchWorldModel disabled")
            return

        experience_path = self._resolve_path(self.config.experience_db_path)
        world_path = self._resolve_path(self.config.world_db_path)
        experience_path.parent.mkdir(parents=True, exist_ok=True)
        world_path.parent.mkdir(parents=True, exist_ok=True)

        store = SqliteStore(path=str(experience_path))
        store.start()
        self._experience_store = self.register_disposable(store)
        self._experience_images = store.stream("experience_images", Image)
        self._experience_events = store.stream("experience_events", str)
        self._semantic_events = store.stream("semantic_events", str)

        self._db = sqlite3.connect(world_path, timeout=10.0, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        # Collapse duplicate rows left by earlier boots: a single open workspace
        # that fragmented into workspace_1..workspace_N folds back to one area.
        try:
            merged = self._merge_adjacent_areas()
            if merged:
                logger.info(
                    "collapsed adjacent duplicate areas at startup", merges=merged
                )
        except Exception:
            logger.exception("startup area merge failed")

        # Rows from an earlier boot with no persistent map coords cannot be
        # reprojected into this session's world frame, so cross-session lookups
        # (nearest_area) must skip them. Count them once so the drop is visible.
        try:
            legacy = int(
                self._db.execute(
                    "SELECT COUNT(*) FROM areas WHERE map_x IS NULL OR map_y IS NULL"
                ).fetchone()[0]
            )
            if legacy:
                logger.info(
                    "legacy areas without persistent map coords will be "
                    "excluded from cross-session lookup",
                    legacy_areas=legacy,
                )
        except Exception:
            logger.exception("legacy area count failed")

        self.register_disposable(Disposable(self.color_image.subscribe(self._on_image)))
        self.register_disposable(Disposable(self.lidar.subscribe(self._on_lidar)))
        self.register_disposable(Disposable(self.odom.subscribe(self._on_odom)))
        self.register_disposable(
            Disposable(self.detections.subscribe(self._on_marker_detections))
        )
        self.register_disposable(Disposable(self.activity.subscribe(self._on_activity)))
        self.register_disposable(
            Disposable(self.goal_request.subscribe(self._on_goal_request))
        )
        self.register_disposable(
            Disposable(self.goal_reached.subscribe(self._on_goal_reached))
        )

        self._stop_event.clear()
        self._semantic_thread = threading.Thread(
            target=self._semantic_loop,
            name="Nightwatch-semantic-memory",
            daemon=True,
        )
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="Nightwatch-experience-watchdog",
            daemon=True,
        )
        self._semantic_thread.start()
        self._watchdog_thread.start()
        logger.info(
            "Nightwatch world and experience memory started",
            experience_db=str(experience_path),
            world_db=str(world_path),
        )

    @rpc
    def stop(self) -> None:
        self._stop_event.set()
        for thread in (self._semantic_thread, self._watchdog_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        self._semantic_thread = None
        self._watchdog_thread = None
        detector = self._detector
        self._detector = None
        if detector is not None:
            try:
                detector.stop()
            except Exception:
                logger.exception("semantic detector stop failed")
        db = self._db
        self._db = None
        if db is not None:
            db.commit()
            db.close()
        super().stop()

    def _create_schema(self) -> None:
        assert self._db is not None
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS areas (
                area_id TEXT PRIMARY KEY,
                center_x REAL NOT NULL,
                center_y REAL NOT NULL,
                samples INTEGER NOT NULL,
                area_type TEXT NOT NULL,
                confidence REAL NOT NULL,
                auto_tag TEXT,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                map_x REAL,
                map_y REAL
            );
            CREATE TABLE IF NOT EXISTS area_votes (
                area_id TEXT NOT NULL,
                area_type TEXT NOT NULL,
                votes INTEGER NOT NULL,
                PRIMARY KEY (area_id, area_type)
            );
            CREATE TABLE IF NOT EXISTS objects (
                object_id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                center_x REAL NOT NULL,
                center_y REAL NOT NULL,
                center_z REAL NOT NULL,
                evidence INTEGER NOT NULL,
                stable INTEGER NOT NULL,
                area_id TEXT,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                map_x REAL,
                map_y REAL
            );
            CREATE INDEX IF NOT EXISTS idx_objects_label
                ON objects(label, last_seen);
            CREATE TABLE IF NOT EXISTS markers (
                marker_id INTEGER PRIMARY KEY,
                name TEXT,
                center_x REAL NOT NULL,
                center_y REAL NOT NULL,
                center_z REAL NOT NULL,
                evidence INTEGER NOT NULL,
                last_seen REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operator_destinations (
                name TEXT PRIMARY KEY,
                map_x REAL NOT NULL,
                map_y REAL NOT NULL,
                map_z REAL NOT NULL DEFAULT 0.0,
                world_x REAL NOT NULL,
                world_y REAL NOT NULL,
                world_z REAL NOT NULL DEFAULT 0.0,
                source_session TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_ts REAL NOT NULL
            );
            """
        )
        # In-place migration for DBs created before persistent map coords
        # existed. Fresh schemas already declare the columns, so the ALTER
        # raises "duplicate column name" and is harmless; a legacy DB gains the
        # columns (NULL for every existing, session-invalid row).
        for table in ("areas", "objects"):
            for column in ("map_x", "map_y"):
                try:
                    self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} REAL"
                    )
                except sqlite3.OperationalError:
                    pass
        self._db.commit()

    def _on_image(self, image: Image) -> None:
        with self._lock:
            self._latest_image = image
            self._last_camera_wall = time.time()

    def _on_lidar(self, pointcloud: PointCloud2) -> None:
        with self._lock:
            self._latest_lidar = pointcloud

    def _on_odom(self, odom: PoseStamped) -> None:
        now = time.monotonic()
        with self._lock:
            self._latest_odom = odom
            self._odom_history.append(
                (now, float(odom.position.x), float(odom.position.y))
            )

    def _on_activity(self, activity: RobotActivity) -> None:
        now = time.monotonic()
        with self._lock:
            previous = self._latest_activity
            translating = (
                activity.moving_expected
                and activity.behavior in _TRANSLATIONAL_BEHAVIORS
            )
            previous_translating = (
                previous is not None
                and previous.moving_expected
                and previous.behavior in _TRANSLATIONAL_BEHAVIORS
            )
            if (
                not translating
                or not previous_translating
                or previous.behavior != activity.behavior
                or previous.owner != activity.owner
            ):
                self._motion_expected_since = now if translating else None
            self._latest_activity = activity

    def _on_goal_request(self, goal: PoseStamped) -> None:
        now = time.monotonic()
        x, y = float(goal.position.x), float(goal.position.y)
        with self._lock:
            # WavefrontFrontierExplorer stops by publishing the robot's current
            # pose as a cancellation goal. It is not an exploration attempt.
            # Counting it taught the failure model that wherever the robot
            # happened to pause was a repeatedly failed destination.
            odom = self._latest_odom
            if (
                odom is not None
                and math.hypot(x - float(odom.position.x), y - float(odom.position.y))
                < 0.50
            ):
                return
            self._goal_history.append((now, x, y))
            repeated = sum(
                now - ts <= 60.0 and math.hypot(x - gx, y - gy) < 0.8
                for ts, gx, gy in self._goal_history
            )
        if repeated >= 3:
            self._record_failure(
                "repeated_goal",
                f"Goal region ({x:.2f}, {y:.2f}) selected {repeated} times in 60s",
                "Temporarily penalize this goal region and choose a more novel frontier.",
                x=x,
                y=y,
            )

    def _on_goal_reached(self, msg: Bool) -> None:
        self._event(
            "navigation_goal",
            "Navigation goal reached"
            if msg.data
            else "Navigation goal cancelled/replanned",
            severity="info",
            outcome="reached" if msg.data else "cancelled",
        )

    def _on_marker_detections(self, msg: Detection3DArray) -> None:
        now = time.time()
        for det in msg.detections[: msg.detections_length]:
            raw_id = str(getattr(det, "id", "")).strip()
            try:
                marker_id = int(raw_id)
            except ValueError:
                continue
            center = det.bbox.center.position
            self._marker_counts[marker_id] += 1
            evidence = self._marker_counts[marker_id]
            name = self.config.marker_names.get(marker_id)
            db = self._db
            if db is not None:
                with self._lock:
                    db.execute(
                        """
                        INSERT INTO markers(marker_id,name,center_x,center_y,center_z,evidence,last_seen)
                        VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(marker_id) DO UPDATE SET
                          name=excluded.name, center_x=excluded.center_x,
                          center_y=excluded.center_y, center_z=excluded.center_z,
                          evidence=markers.evidence+1, last_seen=excluded.last_seen
                        """,
                        (
                            marker_id,
                            name,
                            float(center.x),
                            float(center.y),
                            float(center.z),
                            1,
                            now,
                        ),
                    )
                    db.commit()
            if name and evidence == self.config.marker_min_evidence:
                # The marker itself is commonly on a wall or tent. Store a
                # collision-safe approach pose on the observed side instead
                # of teaching navigation to drive into the marker.
                with self._lock:
                    odom = self._latest_odom
                marker_x, marker_y = float(center.x), float(center.y)
                if odom is not None:
                    robot_x = float(odom.position.x)
                    robot_y = float(odom.position.y)
                    distance = math.hypot(robot_x - marker_x, robot_y - marker_y)
                    standoff = min(0.8, max(0.0, distance - 0.2))
                    if distance > 0.05:
                        target_x = marker_x + (robot_x - marker_x) / distance * standoff
                        target_y = marker_y + (robot_y - marker_y) / distance * standoff
                    else:
                        target_x, target_y = robot_x, robot_y
                else:
                    target_x, target_y = marker_x, marker_y
                self._tag_location(
                    name,
                    target_x,
                    target_y,
                    0.0,
                    source=f"AprilTag {marker_id} safe approach",
                )

    def _semantic_loop(self) -> None:
        while not self._stop_event.wait(1.0):
            try:
                self._maybe_record_keyframe()
                self._maybe_scan_semantics()
            except Exception:
                logger.exception("semantic memory iteration failed")
                self._stop_event.wait(2.0)

    def _maybe_record_keyframe(self, *, force: bool = False, event: str = "") -> None:
        with self._lock:
            image = self._latest_image
            odom = self._latest_odom
        if image is None or odom is None or self._experience_images is None:
            return
        now = time.time()
        xy = (float(odom.position.x), float(odom.position.y))
        moved = (
            float("inf")
            if self._last_keyframe_xy is None
            else math.dist(xy, self._last_keyframe_xy)
        )
        if not force and (
            now - self._last_keyframe_at < self.config.keyframe_min_period_s
            and moved < self.config.keyframe_min_distance_m
        ):
            return
        try:
            sharpness = float(image.sharpness)
        except Exception:
            sharpness = 0.0
        if not force and sharpness < self.config.keyframe_min_sharpness:
            return
        pose = Pose(odom)
        self._experience_images.append(
            image,
            ts=float(image.ts or now),
            pose=pose,
            tags={
                "kind": "failure" if force else "keyframe",
                "event": event or "coverage",
                "sharpness": round(sharpness, 2),
            },
        )
        self._last_keyframe_at = now
        self._last_keyframe_xy = xy

    def _get_detector(self) -> Any | None:
        if self._detector is None:
            now = time.monotonic()
            if now < self._detector_retry_after:
                return None
            try:
                from dimos.perception.detection.detectors.yoloe import (
                    Yoloe2DDetector,
                    YoloePromptMode,
                )

                detector = Yoloe2DDetector(
                    device="cpu",
                    prompt_mode=YoloePromptMode.PROMPT,
                    conf=0.55,
                    max_area_ratio=0.8,
                )
                detector.set_prompts(text=list(self.config.object_prompts))
                self._detector = detector
                self._detector_error = None
                logger.info(
                    "YOLO-E semantic detector ready",
                    prompts=len(self.config.object_prompts),
                )
            except Exception as exc:
                cooldown = max(5.0, self.config.semantic_failure_cooldown_s)
                self._detector_retry_after = now + cooldown
                self._detector_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "YOLO-E semantic detector unavailable; mapping continues",
                    retry_in_s=round(cooldown),
                )
                return None
        return self._detector

    def _maybe_scan_semantics(self) -> None:
        with self._lock:
            image = self._latest_image
            pointcloud = self._latest_lidar
            odom = self._latest_odom
        if image is None or pointcloud is None or odom is None:
            return
        now = time.time()
        xy = (float(odom.position.x), float(odom.position.y))
        moved = (
            float("inf")
            if self._last_semantic_xy is None
            else math.dist(xy, self._last_semantic_xy)
        )
        if (
            now - self._last_semantic_at < self.config.semantic_interval_s
            and moved < self.config.semantic_min_distance_m
        ):
            return
        if (
            image.ts
            and pointcloud.ts
            and abs(float(image.ts) - float(pointcloud.ts)) > 1.0
        ):
            return

        detector = self._get_detector()
        if detector is None:
            # Mark this scan as handled. The detector has its own longer retry
            # deadline, while the rest of semantic-memory work remains alive.
            self._last_semantic_at = now
            return
        detections = detector.process_image(image)
        transform = self.tf.get(
            "camera_optical",
            pointcloud.frame_id,
            time_point=float(image.ts or now),
            time_tolerance=1.0,
        )
        area_id = self._ensure_area(xy[0], xy[1], now)
        labels: list[str] = []
        if transform is not None:
            from dimos.perception.detection.type.detection3d.pointcloud import (
                Detection3DPC,
            )

            for detection in sorted(
                detections.detections,
                key=lambda d: float(d.confidence),
                reverse=True,
            )[:20]:
                label = str(detection.name).strip().lower()
                if not label or label in {"person", "people", "human"}:
                    continue
                try:
                    lifted = Detection3DPC.from_2d(
                        detection,
                        pointcloud,
                        self.configured_camera_info(),
                        transform,
                    )
                except Exception:
                    logger.exception("semantic 2D-to-3D lift failed", label=label)
                    continue
                if lifted is None:
                    continue
                labels.append(label)
                self._upsert_object(
                    label,
                    float(lifted.center.x),
                    float(lifted.center.y),
                    float(lifted.center.z),
                    area_id,
                    now,
                )

        for label in labels:
            hint = _object_area_hint(label)
            if hint:
                # Sleeping furniture is more diagnostic than generic office
                # furniture. Four votes let several independent cues (tent,
                # sleeping bag, camp bed) beat a noisy repeated VLM
                # "workspace" answer without relying on two 3-D lifts landing
                # within 75 cm of one another.
                self._vote_area(area_id, hint, 4 if hint == "sleeping_area" else 1)

        # Object lifting can scatter repeated detections around a large tent,
        # leaving every individual object below the stability threshold. Room-
        # level diversity is stronger evidence: two different sleeping cues
        # make the classification decisive and can correct an early wrong tag.
        self._reinforce_sleeping_area_from_objects(area_id)

        # VLM classification is limited to new/uncertain areas. It shares the
        # existing model and cannot starve the camera callback.
        area = self._area_row(area_id)
        if area is not None and (
            area["samples"] <= 2
            or area["area_type"] == "unknown"
            and now - area["last_seen"] > 90.0
        ):
            inferred = self._classify_area_with_vlm()
            if inferred is not None:
                self._vote_area(area_id, inferred, 1)

        self._resolve_area(area_id)
        self._semantic_events.append(
            json.dumps(
                {
                    "area_id": area_id,
                    "robot_xy": [round(xy[0], 3), round(xy[1], 3)],
                    "objects": labels,
                },
                sort_keys=True,
            ),
            ts=now,
            pose=Pose(odom),
            tags={"kind": "semantic_scan", "area_id": area_id},
        )
        self._last_semantic_at = now
        self._last_semantic_xy = xy

    def _classify_area_with_vlm(self) -> str | None:
        """Best-effort room naming; never let an accelerator fault stop mapping."""
        if not self.config.area_vlm_enabled:
            return None
        now = time.monotonic()
        if now < self._area_vlm_retry_after:
            return None
        try:
            answer = self._vision.moondream_query(
                "Classify this place using exactly one label: "
                + ", ".join(_AREA_TYPES)
                + ". Consider visible furniture and the purpose of the space. "
                "Reply with only the label."
            )
        except Exception as exc:
            cooldown = max(5.0, self.config.area_vlm_failure_cooldown_s)
            self._area_vlm_retry_after = now + cooldown
            self._area_vlm_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "area VLM unavailable; object-based self-tagging continues",
                error=self._area_vlm_error,
                retry_in_s=round(cooldown),
            )
            return None
        self._area_vlm_error = None
        return self._parse_area_type(answer)

    def configured_camera_info(self) -> Any:
        """Return the Go2 static camera calibration without another stream."""
        from dimos.robot.unitree.go2.connection import GO2Connection

        return GO2Connection.camera_info_static

    @staticmethod
    def _parse_area_type(answer: str) -> str:
        normalized = str(answer).strip().lower().replace(" ", "_")
        return next(
            (area_type for area_type in _AREA_TYPES if area_type in normalized),
            "unknown",
        )

    def _ensure_area(self, x: float, y: float, now: float) -> str:
        db = self._db
        if db is None:
            return "unavailable"
        with self._lock:
            rows = db.execute(
                "SELECT area_id,center_x,center_y,samples FROM areas"
            ).fetchall()
            nearest = min(
                rows,
                key=lambda row: math.hypot(x - row[1], y - row[2]),
                default=None,
            )
            if (
                nearest is not None
                and math.hypot(x - nearest[1], y - nearest[2])
                <= self.config.area_radius_m
            ):
                area_id, cx, cy, samples = nearest
                n = int(samples) + 1
                db.execute(
                    """
                    UPDATE areas SET center_x=?,center_y=?,samples=?,last_seen=?
                    WHERE area_id=?
                    """,
                    ((cx * samples + x) / n, (cy * samples + y) / n, n, now, area_id),
                )
                db.commit()
                self._session_area_ids.add(str(area_id))
                return str(area_id)

            area_id = f"area_{uuid.uuid4().hex[:10]}"
            db.execute(
                """
                INSERT INTO areas
                (area_id,center_x,center_y,samples,area_type,confidence,auto_tag,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (area_id, x, y, 1, "unknown", 0.0, None, now, now),
            )
            db.execute(
                "INSERT INTO area_votes(area_id,area_type,votes) VALUES(?,?,?)",
                (area_id, "unknown", 1),
            )
            db.commit()
            self._session_area_ids.add(area_id)
            return area_id

    def _area_row(self, area_id: str) -> dict[str, Any] | None:
        db = self._db
        if db is None:
            return None
        with self._lock:
            row = db.execute(
                """
                SELECT area_id,center_x,center_y,samples,area_type,confidence,
                       auto_tag,first_seen,last_seen
                FROM areas WHERE area_id=?
                """,
                (area_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(
            zip(
                (
                    "area_id",
                    "center_x",
                    "center_y",
                    "samples",
                    "area_type",
                    "confidence",
                    "auto_tag",
                    "first_seen",
                    "last_seen",
                ),
                row,
                strict=True,
            )
        )

    def _vote_area(self, area_id: str, area_type: str, votes: int) -> None:
        if area_type not in _AREA_TYPES:
            return
        db = self._db
        if db is None:
            return
        with self._lock:
            db.execute(
                """
                INSERT INTO area_votes(area_id,area_type,votes) VALUES(?,?,?)
                ON CONFLICT(area_id,area_type) DO UPDATE SET
                  votes=area_votes.votes+excluded.votes
                """,
                (area_id, area_type, int(votes)),
            )
            db.commit()

    def _reinforce_sleeping_area_from_objects(self, area_id: str) -> bool:
        """Make diverse sleeping furniture decisive at room level.

        A single unstable detector hit retains its ordinary vote. A stable tent
        or two distinct cue kinds is enough to outrank all competing votes.
        The update is idempotent: it raises the sleeping score to the required
        floor instead of adding the same bonus on every semantic scan.
        """
        db = self._db
        if db is None:
            return False
        with self._lock:
            rows = db.execute(
                "SELECT label,stable FROM objects WHERE area_id=?", (area_id,)
            ).fetchall()
            cues = {
                cue
                for label, _stable in rows
                if (cue := _sleeping_cue(str(label))) is not None
            }
            stable_tent = any(
                bool(stable) and _sleeping_cue(str(label)) == "tent"
                for label, stable in rows
            )
            if not stable_tent and len(cues) < 2:
                return False
            other_total = int(
                db.execute(
                    """
                    SELECT COALESCE(SUM(votes),0) FROM area_votes
                    WHERE area_id=? AND area_type!='sleeping_area'
                    """,
                    (area_id,),
                ).fetchone()[0]
            )
            current_row = db.execute(
                """
                SELECT votes FROM area_votes
                WHERE area_id=? AND area_type='sleeping_area'
                """,
                (area_id,),
            ).fetchone()
            current = int(current_row[0]) if current_row is not None else 0
            target = other_total + 4
            if current >= target:
                return False
            db.execute(
                """
                INSERT INTO area_votes(area_id,area_type,votes) VALUES(?,?,?)
                ON CONFLICT(area_id,area_type) DO UPDATE SET votes=excluded.votes
                """,
                (area_id, "sleeping_area", target),
            )
            db.commit()
        logger.info(
            "diverse sleeping furniture corrected area evidence",
            area_id=area_id,
            cues=sorted(cues),
            sleeping_votes=target,
        )
        return True

    def _area_merge_radius(self) -> float:
        """Resolve the merge radius, defaulting to 2.0 * area_radius_m."""
        configured = getattr(self.config, "area_merge_radius_m", None)
        if configured is not None and configured > 0:
            return float(configured)
        return 2.0 * float(self.config.area_radius_m)

    def _resolve_area(self, area_id: str) -> None:
        db = self._db
        if db is None:
            return
        merged = False
        tag_spec: tuple[str, float, float, str] | None = None
        area_type = "unknown"
        with self._lock:
            votes = db.execute(
                """
                SELECT area_type,votes FROM area_votes
                WHERE area_id=? ORDER BY votes DESC,area_type
                """,
                (area_id,),
            ).fetchall()
            if not votes:
                return
            total = sum(int(row[1]) for row in votes)
            area_type, best = str(votes[0][0]), int(votes[0][1])
            confidence = best / max(1, total)
            area = self._area_row(area_id)
            if area is None:
                return
            auto_tag = area["auto_tag"]
            runner_up = int(votes[1][1]) if len(votes) > 1 else 0
            correction = (
                auto_tag is not None
                and area_type != area["area_type"]
                and area_type != "unknown"
                and best - runner_up >= 4
                and confidence >= 0.5
            )
            qualifies = auto_tag is None and (
                best >= 2 and area_type != "unknown" and confidence >= 0.5
            )
            old_auto_tag: str | None = None
            if correction:
                # Earlier code made the first automatic label immutable. That
                # left a tent room permanently named "workspace" even after
                # much stronger sleeping evidence arrived.
                old_auto_tag = str(auto_tag)
                auto_tag = None
                qualifies = True
            if qualifies:
                # Suppress duplicate tagging: if a same-type area is already
                # tagged within the merge radius, this is the same place, so
                # fold into it rather than mint workspace_2, workspace_3, ...
                neighbor = (
                    None
                    if correction
                    else self._nearest_tagged_same_type_locked(
                        area_id,
                        float(area["center_x"]),
                        float(area["center_y"]),
                        area_type,
                    )
                )
                if neighbor is not None:
                    db.execute(
                        "UPDATE areas SET area_type=?,confidence=? WHERE area_id=?",
                        (area_type, confidence, area_id),
                    )
                    db.commit()
                    self._merge_pair_locked(area_id, neighbor)
                    merged = True
                else:
                    number = (
                        db.execute(
                            "SELECT COUNT(*) FROM areas WHERE area_type=?", (area_type,)
                        ).fetchone()[0]
                        + 1
                    )
                    auto_tag = (
                        area_type
                        if area_type == "sleeping_area" and number == 1
                        else f"{area_type}_{number}"
                    )
            if not merged:
                db.execute(
                    """
                    UPDATE areas SET area_type=?,confidence=?,auto_tag=?
                    WHERE area_id=?
                    """,
                    (area_type, confidence, auto_tag, area_id),
                )
                db.commit()
                if auto_tag and not area["auto_tag"]:
                    tag_spec = (
                        str(auto_tag),
                        float(area["center_x"]),
                        float(area["center_y"]),
                        area_type,
                    )
                elif correction and auto_tag:
                    tag_spec = (
                        str(auto_tag),
                        float(area["center_x"]),
                        float(area["center_y"]),
                        area_type,
                    )
        if old_auto_tag is not None:
            self._forget_spatial_tag(old_auto_tag)
        if tag_spec is not None:
            name, cx, cy, resolved_type = tag_spec
            self._tag_location(
                name, cx, cy, 0.0, source=f"self-classified {resolved_type}"
            )
        # Collapse any adjacent same-type areas the scan just produced while
        # crossing one large room. Only meaningful once a type was assigned.
        if area_type != "unknown":
            try:
                self._merge_adjacent_areas()
            except Exception:
                logger.exception("adjacent-area merge pass failed")

    def _nearest_tagged_same_type_locked(
        self, area_id: str, x: float, y: float, area_type: str
    ) -> str | None:
        """Return an already-tagged same-type area within the merge radius.

        Caller must hold ``self._lock``.
        """
        db = self._db
        if db is None:
            return None
        radius = self._area_merge_radius()
        rows = db.execute(
            """
            SELECT area_id,center_x,center_y FROM areas
            WHERE area_type=? AND auto_tag IS NOT NULL AND area_id!=?
            """,
            (area_type, area_id),
        ).fetchall()
        nearest: str | None = None
        best = radius
        for candidate_id, cx, cy in rows:
            distance = math.hypot(x - cx, y - cy)
            if distance <= best:
                best = distance
                nearest = str(candidate_id)
        return nearest

    def _merge_pair_locked(self, area_id_a: str, area_id_b: str) -> str | None:
        """Fold one area into another. Caller must hold ``self._lock``.

        Keeps the area with more samples (tie: the older first_seen), combines
        the centers weighted by samples, sums samples, sums per-type votes into
        the survivor, re-points objects, and deletes the loser's rows. If both
        carry an auto_tag the survivor keeps its own; otherwise the survivor
        inherits whichever tag exists so the merged room stays on the map.
        """
        db = self._db
        if db is None or area_id_a == area_id_b:
            return None
        a = self._area_row(area_id_a)
        b = self._area_row(area_id_b)
        if a is None or b is None:
            return None
        a_samples, b_samples = int(a["samples"]), int(b["samples"])
        if b_samples > a_samples or (
            b_samples == a_samples and float(b["first_seen"]) < float(a["first_seen"])
        ):
            survivor, loser = b, a
        else:
            survivor, loser = a, b
        s_samples, l_samples = int(survivor["samples"]), int(loser["samples"])
        total = s_samples + l_samples
        denominator = max(1, total)
        center_x = (
            survivor["center_x"] * s_samples + loser["center_x"] * l_samples
        ) / denominator
        center_y = (
            survivor["center_y"] * s_samples + loser["center_y"] * l_samples
        ) / denominator
        auto_tag = survivor["auto_tag"] or loser["auto_tag"]
        first_seen = min(float(survivor["first_seen"]), float(loser["first_seen"]))
        last_seen = max(float(survivor["last_seen"]), float(loser["last_seen"]))
        survivor_id, loser_id = str(survivor["area_id"]), str(loser["area_id"])
        db.execute(
            """
            UPDATE areas SET center_x=?,center_y=?,samples=?,auto_tag=?,
              first_seen=?,last_seen=? WHERE area_id=?
            """,
            (center_x, center_y, total, auto_tag, first_seen, last_seen, survivor_id),
        )
        for vote_type, votes in db.execute(
            "SELECT area_type,votes FROM area_votes WHERE area_id=?", (loser_id,)
        ).fetchall():
            db.execute(
                """
                INSERT INTO area_votes(area_id,area_type,votes) VALUES(?,?,?)
                ON CONFLICT(area_id,area_type) DO UPDATE SET
                  votes=area_votes.votes+excluded.votes
                """,
                (survivor_id, str(vote_type), int(votes)),
            )
        db.execute("DELETE FROM area_votes WHERE area_id=?", (loser_id,))
        db.execute(
            "UPDATE objects SET area_id=? WHERE area_id=?", (survivor_id, loser_id)
        )
        db.execute("DELETE FROM areas WHERE area_id=?", (loser_id,))
        db.commit()
        loser_tag = loser["auto_tag"]
        if loser_tag and loser_tag != auto_tag:
            self._forget_spatial_tag(str(loser_tag))
        return survivor_id

    def _merge_adjacent_areas(self) -> int:
        """Collapse adjacent same-type areas until none remain, returns merges.

        Walking across one 10-15 m room used to create a chain of new areas
        every area_radius_m, each self-tagged independently as workspace_N. Any
        two areas of the same resolved type (never "unknown") whose centers are
        within the merge radius are the same place: fold the smaller into the
        larger. Re-scanning after each merge lets a whole chain collapse, since
        combining centers can bring a third area inside the radius.
        """
        db = self._db
        if db is None:
            return 0
        radius = self._area_merge_radius()
        merges = 0
        with self._lock:
            while True:
                rows = db.execute(
                    """
                    SELECT area_id,center_x,center_y,area_type FROM areas
                    WHERE area_type != 'unknown'
                    """
                ).fetchall()
                pair: tuple[str, str] | None = None
                for i in range(len(rows)):
                    for j in range(i + 1, len(rows)):
                        left, right = rows[i], rows[j]
                        if left[3] != right[3]:
                            continue
                        if math.hypot(left[1] - right[1], left[2] - right[2]) <= radius:
                            pair = (str(left[0]), str(right[0]))
                            break
                    if pair is not None:
                        break
                if pair is None:
                    break
                if self._merge_pair_locked(pair[0], pair[1]) is None:
                    break
                merges += 1
        return merges

    def _forget_spatial_tag(self, name: str) -> None:
        """Note that a merged-away tag stays searchable in spatial memory.

        The spatial-memory tag store exposes no delete path across the module
        boundary: SpatialTagSpec proxies only tag_location and query_by_text,
        and neither SpatialMemory nor SpatialVectorDB (its Chroma
        location_collection is add/query only) defines a remove method, so
        there is nothing safe to call from here. The sqlite areas table, which
        drives the map dots, no longer lists the merged name, so the only
        residue is a stale, still-searchable Chroma entry. Log it and move on;
        a merge must never fail on tag-store cleanup.
        """
        try:
            logger.info(
                "merged-away area tag remains searchable in spatial memory; "
                "no delete API is exposed to remove it",
                name=name,
            )
        except Exception:
            logger.exception("failed to log stale spatial tag", name=name)

    def _upsert_object(
        self,
        label: str,
        x: float,
        y: float,
        z: float,
        area_id: str,
        now: float,
    ) -> None:
        db = self._db
        if db is None:
            return
        became_stable_id: str | None = None
        with self._lock:
            rows = db.execute(
                """
                SELECT object_id,center_x,center_y,center_z,evidence,stable
                FROM objects WHERE label=?
                """,
                (label,),
            ).fetchall()
            nearest = min(
                rows,
                key=lambda row: math.sqrt(
                    (x - row[1]) ** 2 + (y - row[2]) ** 2 + (z - row[3]) ** 2
                ),
                default=None,
            )
            if (
                nearest is not None
                and math.sqrt(
                    (x - nearest[1]) ** 2
                    + (y - nearest[2]) ** 2
                    + (z - nearest[3]) ** 2
                )
                < 0.75
            ):
                object_id, ox, oy, oz, evidence, prev_stable = nearest
                n = int(evidence) + 1
                new_stable = int(n >= self.config.object_min_evidence)
                db.execute(
                    """
                    UPDATE objects SET center_x=?,center_y=?,center_z=?,
                      evidence=?,stable=?,area_id=?,last_seen=?
                    WHERE object_id=?
                    """,
                    (
                        (ox * evidence + x) / n,
                        (oy * evidence + y) / n,
                        (oz * evidence + z) / n,
                        n,
                        new_stable,
                        area_id,
                        now,
                        object_id,
                    ),
                )
                self._session_object_ids.add(str(object_id))
                if not int(prev_stable) and new_stable:
                    became_stable_id = str(object_id)
            else:
                object_id = f"obj_{uuid.uuid4().hex[:10]}"
                db.execute(
                    """
                    INSERT INTO objects
                    (object_id,label,center_x,center_y,center_z,evidence,stable,area_id,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        object_id,
                        label,
                        x,
                        y,
                        z,
                        1,
                        0,
                        area_id,
                        now,
                        now,
                    ),
                )
                self._session_object_ids.add(object_id)
            db.commit()
        # A tent is a decisive sleeping-area signal. The instant a tent object
        # first becomes stable (its stable flag flips 0->1), cast one heavy vote
        # so it beats routine per-scan VLM/object-hint votes. Gating on the
        # transition makes this fire exactly once per object, never on the
        # re-observations that follow.
        if became_stable_id is not None and "tent" in label.lower():
            self._vote_area(area_id, "sleeping_area", 8)
            logger.info(
                "tent observed, area voted sleeping_area",
                area_id=area_id,
                object_id=became_stable_id,
            )

    def _tag_location(
        self, name: str, x: float, y: float, z: float, *, source: str
    ) -> bool:
        with self._lock:
            odom = self._latest_odom
        if odom is not None:
            euler = odom.orientation.euler
            rotation = (float(euler.x), float(euler.y), float(euler.z))
        else:
            rotation = (0.0, 0.0, 0.0)
        location = RobotLocation(
            name=name,
            position=(x, y, z),
            rotation=rotation,
            metadata={"source": source, "automatic": True},
        )
        try:
            tagged = bool(self._spatial_memory.tag_location(location))
        except Exception:
            logger.exception("automatic location tag failed", name=name)
            return False
        if tagged:
            self._event(
                "location_tagged",
                f"Tagged {name} at ({x:.2f}, {y:.2f}) from {source}",
                severity="info",
                name=name,
                source=source,
            )
        return tagged

    def _watchdog_loop(self) -> None:
        ticks = 0
        while not self._stop_event.wait(1.0):
            try:
                self._check_experience_health()
            except Exception:
                logger.exception("experience watchdog failed")
            ticks += 1
            if ticks % 3 == 0:
                try:
                    self._publish_map_annotations()
                except Exception:
                    logger.exception("map annotation publish failed")
            # The world->map transform is cached for a few seconds, so a 5 s
            # backfill cadence costs one RPC at most and never per-row.
            if ticks % 5 == 0:
                try:
                    self._backfill_map_coords()
                except Exception:
                    logger.exception("map coordinate backfill failed")

    def _world_to_map(self) -> tuple[float, float, float] | None:
        """Return the cached world->map 2D transform, or None while unlocked.

        Applying the returned ``(x, y, yaw)`` maps SESSION WORLD coordinates
        into the persistent MAP frame (see NightwatchRelocalization). The result
        is cached briefly so per-row backfill and lookups never RPC the reloc
        worker once per row.
        """
        now = time.monotonic()
        with self._lock:
            if now < self._world_to_map_cache_until:
                return self._world_to_map_cache
        result: tuple[float, float, float] | None = None
        try:
            payload = self._reloc.world_to_map_2d()
        except Exception:
            logger.exception("world->map transform query failed")
            payload = None
        if payload:
            try:
                result = (
                    float(payload["x"]),
                    float(payload["y"]),
                    float(payload["yaw"]),
                )
            except (KeyError, TypeError, ValueError):
                logger.exception("world->map transform payload malformed")
                result = None
        with self._lock:
            self._world_to_map_cache = result
            self._world_to_map_cache_until = now + _WORLD_TO_MAP_CACHE_S
        return result

    def _backfill_map_coords(self) -> None:
        """Stamp persistent MAP-frame coords onto this session's rows.

        Once relocalization locks, every area/object observed this session gets
        its current-frame center projected into the map frame, so a later boot
        can read the tag back at the right place. Rows are refreshed whenever
        their world center has moved (running-average drift, merges), keeping
        the two representations consistent. Prior-session rows are left
        untouched: their centers are in a stale world frame.
        """
        transform = self._world_to_map()
        if transform is None:
            return
        db = self._db
        if db is None:
            return
        tx, ty, yaw = transform
        cos_yaw = math.cos(yaw)
        sin_yaw = math.sin(yaw)

        def project(cx: float, cy: float) -> tuple[float, float]:
            return (
                cos_yaw * cx - sin_yaw * cy + tx,
                sin_yaw * cx + cos_yaw * cy + ty,
            )

        with self._lock:
            for area_id, cx, cy, mx, my in db.execute(
                "SELECT area_id,center_x,center_y,map_x,map_y FROM areas"
            ).fetchall():
                if str(area_id) not in self._session_area_ids:
                    continue
                new_mx, new_my = project(float(cx), float(cy))
                if (
                    mx is None
                    or my is None
                    or abs(new_mx - float(mx)) > 1e-6
                    or abs(new_my - float(my)) > 1e-6
                ):
                    db.execute(
                        "UPDATE areas SET map_x=?,map_y=? WHERE area_id=?",
                        (new_mx, new_my, area_id),
                    )
            for object_id, cx, cy, mx, my in db.execute(
                "SELECT object_id,center_x,center_y,map_x,map_y FROM objects"
            ).fetchall():
                if str(object_id) not in self._session_object_ids:
                    continue
                new_mx, new_my = project(float(cx), float(cy))
                if (
                    mx is None
                    or my is None
                    or abs(new_mx - float(mx)) > 1e-6
                    or abs(new_my - float(my)) > 1e-6
                ):
                    db.execute(
                        "UPDATE objects SET map_x=?,map_y=? WHERE object_id=?",
                        (new_mx, new_my, object_id),
                    )
            db.commit()

    def _publish_map_annotations(self) -> None:
        """Publish the semantic map layer: tags, anchors, objects, trail."""
        db = self._db
        if db is None:
            return
        with self._lock:
            area_rows = db.execute(
                """
                SELECT auto_tag, area_type, center_x, center_y
                FROM areas WHERE auto_tag IS NOT NULL
                """
            ).fetchall()
            marker_rows = db.execute(
                "SELECT marker_id, name, center_x, center_y, center_z FROM markers"
            ).fetchall()
            object_rows = db.execute(
                """
                SELECT object_id, label, center_x, center_y, center_z
                FROM objects WHERE stable != 0
                """
            ).fetchall()
            history = list(self._odom_history)
        bedroom = self.bedroom_destination()

        markers: list[Marker] = []
        for auto_tag, area_type, x, y in area_rows:
            markers.append(
                Marker(
                    entity_id=f"area:{auto_tag}",
                    label=str(auto_tag),
                    entity_type="location",
                    x=float(x),
                    y=float(y),
                    z=0.3,
                )
            )
        for marker_id, name, x, y, z in marker_rows:
            markers.append(
                Marker(
                    entity_id=f"marker:{marker_id}",
                    label=str(name or f"tag_{marker_id}"),
                    entity_type="location",
                    x=float(x),
                    y=float(y),
                    z=float(z) + 0.3,
                )
            )
        for object_id, label, x, y, z in object_rows:
            markers.append(
                Marker(
                    entity_id=f"object:{object_id}",
                    label=str(label)[:40],
                    entity_type="object",
                    x=float(x),
                    y=float(y),
                    z=float(z) + 0.3,
                )
            )
        if bedroom is not None:
            markers.append(
                Marker(
                    entity_id="destination:bedroom",
                    label="Bedroom",
                    entity_type="location",
                    x=float(bedroom["x"]),
                    y=float(bedroom["y"]),
                    z=float(bedroom.get("z", 0.0)) + 0.45,
                )
            )
        if markers:
            self.tags.publish(EntityMarkers(markers=markers))

        if len(history) >= 2:
            poses = [
                PoseStamped(position=Vector3(float(x), float(y), 0.05))
                for _ts, x, y in history
            ]
            self.trail.publish(NavPath(frame_id="world", poses=poses))

    @staticmethod
    def _path_metrics(points: list[tuple[float, float, float]]) -> tuple[float, float]:
        if len(points) < 2:
            return 0.0, 0.0
        path = sum(
            math.hypot(b[1] - a[1], b[2] - a[2])
            for a, b in zip(points, points[1:], strict=False)
        )
        displacement = math.hypot(
            points[-1][1] - points[0][1], points[-1][2] - points[0][2]
        )
        return path, displacement

    def _check_experience_health(self) -> None:
        now_mono = time.monotonic()
        now_wall = time.time()
        with self._lock:
            activity = self._latest_activity
            history = list(self._odom_history)
            motion_expected_since = getattr(
                self, "_motion_expected_since", None
            )
            odom = self._latest_odom
            camera_wall = self._last_camera_wall
        if camera_wall and now_wall - camera_wall > 1.5:
            self._record_failure(
                "camera_stale",
                f"Latest camera frame is {now_wall - camera_wall:.1f}s old",
                "Keep latest-only transport and inspect robot LAN/WebRTC health.",
            )

        # EXPRESS means joint/body motion (wave, stretch, hips), not base
        # translation, so it is excluded from _TRANSLATIONAL_BEHAVIORS and
        # returns early here. Treating it as navigation "stuck" poisoned the
        # explorer's persistent failure map at every friendly gesture. FOLLOW
        # and the other translational behaviors do pass this guard, but the
        # stuck/coverage_loop records below withhold spatial explorer feedback
        # unless the dog is actually goal-driven (EXPLORE or PATROL): standing
        # still at follow distance is correct, not a navigation failure.
        if (
            activity is None
            or not activity.moving_expected
            or activity.behavior not in _TRANSLATIONAL_BEHAVIORS
            or odom is None
            or motion_expected_since is None
        ):
            return
        continuous_motion_s = now_mono - motion_expected_since
        stuck_points = [
            item
            for item in history
            if item[0] >= motion_expected_since
            and now_mono - item[0] <= self.config.stuck_window_s
        ]
        stuck_path, stuck_displacement = self._path_metrics(stuck_points)
        if (
            continuous_motion_s >= self.config.stuck_window_s
            and len(stuck_points) >= 3
            and stuck_displacement < self.config.stuck_displacement_m
            and stuck_path < 0.35
        ):
            self._record_failure(
                "stuck",
                f"Motion expected but moved only {stuck_displacement:.2f}m "
                f"in {self.config.stuck_window_s:.0f}s",
                "Penalize this region, rescan obstacles, and select another frontier.",
                x=float(odom.position.x),
                y=float(odom.position.y),
                spatial=activity.behavior
                in (BehaviorKind.EXPLORE, BehaviorKind.PATROL),
            )

        loop_points = [
            item
            for item in history
            if item[0] >= motion_expected_since
            and now_mono - item[0] <= self.config.loop_window_s
        ]
        loop_path, loop_displacement = self._path_metrics(loop_points)
        if (
            continuous_motion_s >= self.config.loop_window_s
            and loop_path >= self.config.loop_path_m
            and loop_displacement <= self.config.loop_displacement_m
        ):
            self._record_failure(
                "coverage_loop",
                f"Travelled {loop_path:.1f}m but ended only "
                f"{loop_displacement:.1f}m from the starting region",
                "Preserve visitation history and strongly prefer a novel frontier.",
                x=float(odom.position.x),
                y=float(odom.position.y),
                spatial=activity.behavior
                in (BehaviorKind.EXPLORE, BehaviorKind.PATROL),
            )

    def _record_failure(
        self,
        category: str,
        detail: str,
        recommendation: str,
        *,
        x: float | None = None,
        y: float | None = None,
        spatial: bool = True,
    ) -> None:
        now = time.monotonic()
        last = self._failure_last.get(category, 0.0)
        if now - last < self.config.failure_cooldown_s:
            return
        self._failure_last[category] = now
        with self._lock:
            odom = self._latest_odom
        if x is None and odom is not None:
            x = float(odom.position.x)
        if y is None and odom is not None:
            y = float(odom.position.y)
        self._event(
            category,
            detail,
            severity="warning",
            recommendation=recommendation,
            x=x,
            y=y,
        )
        self._maybe_record_keyframe(force=True, event=category)
        # Sensor/ML health is worth recording, but it says nothing about the
        # traversability of the robot's current coordinates. Previously a
        # briefly stale startup camera created a fake obstacle/failure zone.
        # ``spatial`` gates this further: only goal-driven navigation (EXPLORE,
        # PATROL) may plant a blacklist zone. A dog standing still at follow
        # distance is behaving correctly and must not poison the failure map.
        if (
            spatial
            and category in _SPATIAL_NAVIGATION_FAILURES
            and x is not None
            and y is not None
        ):
            try:
                self._explore.report_navigation_failure(x, y, category)
            except Exception:
                logger.exception("could not send failure feedback to explorer")
            try:
                self._patrol_feedback.report_patrol_failure(x, y, category)
            except Exception:
                logger.exception("could not send failure feedback to patrol")

    def _event(
        self, category: str, detail: str, *, severity: str, **fields: Any
    ) -> None:
        if self._experience_events is None:
            return
        with self._lock:
            odom = self._latest_odom
        payload = {
            "category": category,
            "detail": detail,
            "severity": severity,
            "timestamp": time.time(),
            **fields,
        }
        self._experience_events.append(
            json.dumps(payload, sort_keys=True),
            ts=payload["timestamp"],
            pose=Pose(odom) if odom is not None else None,
            tags={"category": category, "severity": severity},
        )
        logger.info("nightwatch experience", **payload)

    @skill
    def world_status(self) -> dict[str, Any]:
        """Return persistent semantic-map and experience-memory health."""
        db = self._db
        if db is None:
            return {"enabled": False}
        with self._lock:
            areas = int(db.execute("SELECT COUNT(*) FROM areas").fetchone()[0])
            objects = int(
                db.execute("SELECT COUNT(*) FROM objects WHERE stable=1").fetchone()[0]
            )
            markers = int(db.execute("SELECT COUNT(*) FROM markers").fetchone()[0])
            keyframes = (
                self._experience_images.count()
                if self._experience_images is not None
                else 0
            )
        try:
            coverage = self._explore.coverage_status()
        except Exception:
            coverage = {}
        bedroom = self.bedroom_status()
        return {
            "enabled": True,
            "areas": areas,
            "stable_objects": objects,
            "markers": markers,
            "experience_keyframes": keyframes,
            "semantic_detector": (
                "ready"
                if self._detector is not None
                else ("degraded" if self._detector_error else "pending")
            ),
            "semantic_detector_error": self._detector_error,
            "area_vlm": (
                "disabled"
                if not self.config.area_vlm_enabled
                else ("degraded" if self._area_vlm_error else "ready")
            ),
            "area_vlm_error": self._area_vlm_error,
            "bedroom": bedroom,
            **coverage,
        }

    @skill
    def list_semantic_areas(self) -> list[dict[str, Any]]:
        """List spaces the dog inferred, including confidence and auto-tag.

        Each row carries ``persistent``: True when the area has map-frame
        coordinates and so survives across sessions.
        """
        db = self._db
        if db is None:
            return []
        with self._lock:
            rows = db.execute(
                """
                SELECT area_id,center_x,center_y,area_type,confidence,auto_tag,
                       samples,last_seen,map_x,map_y
                FROM areas ORDER BY last_seen DESC
                """
            ).fetchall()
        keys = (
            "area_id",
            "center_x",
            "center_y",
            "area_type",
            "confidence",
            "auto_tag",
            "samples",
            "last_seen",
        )
        areas: list[dict[str, Any]] = []
        for row in rows:
            record = dict(zip(keys, row[:8], strict=True))
            record["persistent"] = row[8] is not None and row[9] is not None
            areas.append(record)
        return areas

    @rpc
    def nearest_area(self, area_type: str, x: float, y: float) -> dict | None:
        """Nearest area of ``area_type`` to (x, y) in the CURRENT world frame.

        Candidates are (a) areas observed THIS session, whose stored centers are
        already in the current session world frame, and (b) prior-session areas
        that carry persistent map_x/map_y, reprojected map->world with the
        INVERSE of the locked world->map transform. Prior-session rows are only
        considered while that transform is available; legacy prior-session rows
        with NULL map coords are skipped. Returns
        ``{"area_id", "x", "y", "area_type", "source"}`` (source is "session" or
        "persistent"), or None when nothing of that type is usable.
        """
        db = self._db
        if db is None:
            return None
        transform = self._world_to_map()
        if transform is not None:
            tx, ty, yaw = transform
            cos_yaw = math.cos(yaw)
            sin_yaw = math.sin(yaw)
        with self._lock:
            rows = db.execute(
                """
                SELECT area_id,center_x,center_y,map_x,map_y,area_type
                FROM areas WHERE area_type=?
                """,
                (area_type,),
            ).fetchall()
            session_ids = set(self._session_area_ids)
        best: tuple[float, dict[str, Any]] | None = None
        for area_id, cx, cy, mx, my, row_type in rows:
            if str(area_id) in session_ids:
                wx, wy = float(cx), float(cy)
                source = "session"
            elif mx is not None and my is not None and transform is not None:
                # Prior session: undo world->map (map = R@world + t) to recover
                # this session's world coords, world = R^T @ (map - t).
                dx = float(mx) - tx
                dy = float(my) - ty
                wx = cos_yaw * dx + sin_yaw * dy
                wy = -sin_yaw * dx + cos_yaw * dy
                source = "persistent"
            else:
                # Legacy row (no map coords) or transform not yet locked.
                continue
            distance = math.hypot(x - wx, y - wy)
            if best is None or distance < best[0]:
                best = (
                    distance,
                    {
                        "area_id": str(area_id),
                        "x": wx,
                        "y": wy,
                        "area_type": str(row_type),
                        "source": source,
                    },
                )
        return best[1] if best is not None else None

    @rpc
    def bedroom_destination(
        self, _x: float = 0.0, _y: float = 0.0
    ) -> dict[str, Any] | None:
        """Return the unique Bedroom in the current WORLD frame."""
        db = self._db
        if db is None:
            return None
        with self._lock:
            row = db.execute(
                """
                SELECT map_x,map_y,map_z,world_x,world_y,world_z,
                       source_session,source,updated_ts
                FROM operator_destinations WHERE name='bedroom'
                """
            ).fetchone()
        if row is None:
            return None
        mx, my, mz, wx, wy, wz, source_session, source, updated_ts = row
        current_session = str(source_session) == getattr(self, "_session_id", "")
        transform = self._world_to_map()
        if current_session:
            current_x, current_y = float(wx), float(wy)
        elif transform is not None:
            tx, ty, yaw = transform
            cos_yaw = math.cos(yaw)
            sin_yaw = math.sin(yaw)
            dx = float(mx) - tx
            dy = float(my) - ty
            current_x = cos_yaw * dx + sin_yaw * dy
            current_y = -sin_yaw * dx + cos_yaw * dy
        else:
            return None
        return {
            "area_id": "bedroom",
            "name": "bedroom",
            "area_type": "sleeping_area",
            "x": current_x,
            "y": current_y,
            "z": float(wz if current_session else mz),
            "map_x": float(mx),
            "map_y": float(my),
            "map_z": float(mz),
            "source": str(source),
            "updated_ts": float(updated_ts),
            "persistent": True,
        }

    @skill
    def bedroom_status(self) -> dict[str, Any] | None:
        """Return Bedroom metadata even when it cannot yet be reprojected."""
        db = self._db
        if db is None:
            return None
        with self._lock:
            row = db.execute(
                """
                SELECT map_x,map_y,map_z,source,updated_ts
                FROM operator_destinations WHERE name='bedroom'
                """
            ).fetchone()
        if row is None:
            return None
        current = self.bedroom_destination()
        return {
            "name": "bedroom",
            "map_x": float(row[0]),
            "map_y": float(row[1]),
            "map_z": float(row[2]),
            "source": str(row[3]),
            "updated_ts": float(row[4]),
            "available": current is not None,
            **(
                {
                    "world_x": float(current["x"]),
                    "world_y": float(current["y"]),
                    "world_z": float(current["z"]),
                }
                if current is not None
                else {}
            ),
        }

    def _set_bedroom_world(
        self, world_x: float, world_y: float, world_z: float, source: str
    ) -> str:
        values = (float(world_x), float(world_y), float(world_z))
        if not all(math.isfinite(value) for value in values):
            return "Cannot set Bedroom: coordinates must be finite."
        db = self._db
        if db is None:
            return "Cannot set Bedroom: world model is unavailable."
        transform = self._world_to_map()
        if transform is None:
            # During the first mapping session WORLD is the map being created.
            # Persisting the identical coordinates lets a later relocalized
            # session recover this point in the saved MAP frame.
            map_x, map_y = values[0], values[1]
        else:
            tx, ty, yaw = transform
            map_x = math.cos(yaw) * values[0] - math.sin(yaw) * values[1] + tx
            map_y = math.sin(yaw) * values[0] + math.cos(yaw) * values[1] + ty
        now = time.time()
        with self._lock:
            db.execute(
                """
                INSERT INTO operator_destinations(
                    name,map_x,map_y,map_z,world_x,world_y,world_z,
                    source_session,source,updated_ts
                ) VALUES('bedroom',?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    map_x=excluded.map_x,
                    map_y=excluded.map_y,
                    map_z=excluded.map_z,
                    world_x=excluded.world_x,
                    world_y=excluded.world_y,
                    world_z=excluded.world_z,
                    source_session=excluded.source_session,
                    source=excluded.source,
                    updated_ts=excluded.updated_ts
                """,
                (
                    map_x,
                    map_y,
                    values[2],
                    values[0],
                    values[1],
                    values[2],
                    self._session_id,
                    source,
                    now,
                ),
            )
            db.commit()
        self._event(
            "bedroom_updated",
            "Operator updated the unique Bedroom destination",
            severity="info",
            map_x=map_x,
            map_y=map_y,
            source=source,
        )
        return (
            f"Bedroom updated at world ({values[0]:.2f}, {values[1]:.2f}) "
            f"and map ({map_x:.2f}, {map_y:.2f})."
        )

    @skill
    def set_bedroom_at(
        self, world_x: float, world_y: float, world_z: float = 0.0
    ) -> str:
        """Replace the one Bedroom with an arbitrary map-view position."""
        return self._set_bedroom_world(
            world_x, world_y, world_z, "operator_map_click"
        )

    @skill
    def set_bedroom_here(self) -> str:
        """Replace the one Bedroom with the robot's current position."""
        with self._lock:
            odom = self._latest_odom
        if odom is None:
            return "Cannot set Bedroom: robot pose is unavailable."
        return self._set_bedroom_world(
            float(odom.position.x),
            float(odom.position.y),
            float(odom.position.z),
            "operator_robot_pose",
        )

    @skill
    def list_remembered_objects(
        self, label: str = "", stable_only: bool = True
    ) -> list[dict[str, Any]]:
        """List persistent 3D objects, optionally filtered by label."""
        db = self._db
        if db is None:
            return []
        clauses = ["stable=1"] if stable_only else []
        args: list[Any] = []
        if label.strip():
            clauses.append("label LIKE ?")
            args.append(f"%{label.strip().lower()}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = db.execute(
                """
                SELECT object_id,label,center_x,center_y,center_z,evidence,
                       stable,area_id,last_seen
                FROM objects
                """
                + where
                + " ORDER BY last_seen DESC LIMIT 200",
                args,
            ).fetchall()
        keys = (
            "object_id",
            "label",
            "center_x",
            "center_y",
            "center_z",
            "evidence",
            "stable",
            "area_id",
            "last_seen",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    @skill
    def search_visual_memory(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search DimensionalOS CLIP memory for a previously seen scene."""
        try:
            return self._spatial_memory.query_by_text(
                query.strip(), max(1, min(int(limit), 20))
            )
        except Exception:
            logger.exception("visual-memory search failed", query=query)
            return []

    @skill
    def analyze_recent_failures(self, window_minutes: float = 30.0) -> dict[str, Any]:
        """Summarize recent failures and the adaptive correction applied."""
        if self._experience_events is None:
            return {"failures": 0, "categories": {}, "recommendations": []}
        cutoff = time.time() - max(1.0, float(window_minutes)) * 60.0
        observations = self._experience_events.after(cutoff).to_list()
        events: list[dict[str, Any]] = []
        for obs in observations:
            try:
                event = json.loads(obs.data)
            except Exception:
                continue
            if event.get("severity") == "warning":
                events.append(event)
        counts = Counter(str(event.get("category")) for event in events)
        recommendations = list(
            dict.fromkeys(
                str(event["recommendation"])
                for event in events
                if event.get("recommendation")
            )
        )
        return {
            "window_minutes": float(window_minutes),
            "failures": len(events),
            "categories": dict(counts),
            "recommendations": recommendations,
            "latest": events[-10:],
        }

    @skill
    def tag_area_here(self, name: str) -> str:
        """Manually add or override a semantic location at the current pose."""
        clean = name.strip()
        if not clean:
            return "Location name cannot be empty."
        with self._lock:
            odom = self._latest_odom
        if odom is None:
            return "No odometry is available yet."
        x = float(odom.position.x)
        y = float(odom.position.y)
        area_id = self._ensure_area(x, y, time.time())
        normalized = clean.lower().replace(" ", "_")
        area_type = next(
            (candidate for candidate in _AREA_TYPES if candidate in normalized),
            "unknown",
        )
        db = self._db
        if db is not None:
            with self._lock:
                db.execute(
                    """
                    UPDATE areas SET area_type=?,confidence=1.0,auto_tag=?,
                      last_seen=? WHERE area_id=?
                    """,
                    (area_type, clean, time.time(), area_id),
                )
                db.commit()
        tagged = self._tag_location(
            clean,
            x,
            y,
            float(odom.position.z),
            source="manual world-model tag",
        )
        return f"Tagged {clean}." if tagged else f"Could not tag {clean}."
