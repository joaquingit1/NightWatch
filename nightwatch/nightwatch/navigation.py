"""Coverage-aware exploration and patrol for Nightwatch.

The stock frontier explorer treats a small change in the current costmap as
proof that a floor is mapped. That is not valid for the Go2 rolling-window
sensor: clearing a temporary obstacle can make known-cell count shrink, and a
single viewpoint can contain no useful frontier while another viewpoint does.

The stock coverage patrol also clears all visitation history every time a
short follow or gesture preempts it. In a cluttered floor its square erosion
can remove every candidate cell. Together those behaviors produce the very
visible "walk locally, turn, forget, repeat" failure.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import threading
import time
from typing import Any, Protocol
import uuid

import numpy as np
from scipy.ndimage import distance_transform_edt

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.core.core import rpc
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import OccupancyGrid
from dimos.navigation.frontier_exploration.wavefront_frontier_goal_selector import (
    WavefrontFrontierExplorer as _StockWavefrontFrontierExplorer,
)
from dimos.navigation.patrolling.module import (
    PatrollingModule as _StockPatrollingModule,
)
from dimos.navigation.patrolling.routers.base_patrol_router import BasePatrolRouter
from dimos.navigation.patrolling.routers.coverage_patrol_router import (
    CoveragePatrolRouter,
)
from dimos.navigation.patrolling.utilities import point_to_pose_stamped
from dimos.navigation.replanning_a_star.min_cost_astar import min_cost_astar
from dimos.spec.utils import Spec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

# Persistent keep-out zones live in the MAP frame (the premap frame that
# survives restarts; the relocalization worker owns the world<->map transform).
# A zone that keeps wedging the dog (a glass window it walks into, a chair trap)
# is recorded once and avoided forever, across boots.
_KEEP_OUT_PATH = "assets/output/maps/nightwatch_keepout.json"
_ODOM_EPOCH_PATH = "assets/output/maps/nightwatch_odom_epoch.json"
_ODOM_EPOCH_CONTINUITY_M = 4.0
_ODOM_EPOCH_MAX_AGE_S = 12.0 * 60.0 * 60.0
_ODOM_EPOCH_SAVE_S = 2.0
# The world->map transform is stable once ICP locks, so poll the reloc worker at
# most this often instead of once per frontier. Mirrors world_model.py.
_WORLD_TO_MAP_CACHE_S = 5.0


class RelocSpec(Spec, Protocol):
    # Structural bind to NightwatchRelocalization: the locked session WORLD ->
    # persistent MAP transform projected to 2D, or None while unlocked. Mirrors
    # curiosity.py / world_model.py.
    def world_to_map_2d(self) -> dict | None: ...  # type: ignore[type-arg]


def _load_keep_out_circles(path: str) -> list[dict[str, Any]]:
    """Read persistent keep-out circles (MAP frame) from ``path``.

    Missing file is normal (none marked yet); malformed rows are skipped so one
    bad entry never disables the whole guard.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except Exception:
        logger.exception("keep-out load failed", path=path)
        return []
    circles: list[dict[str, Any]] = []
    for item in data if isinstance(data, list) else []:
        try:
            circles.append(
                {
                    "x": float(item["x"]),
                    "y": float(item["y"]),
                    "radius_m": float(item["radius_m"]),
                    "created": float(item.get("created", 0.0)),
                    **(
                        {
                            "world_x": float(item["world_x"]),
                            "world_y": float(item["world_y"]),
                        }
                        if "world_x" in item and "world_y" in item
                        else {}
                    ),
                    **(
                        {"odom_epoch": str(item["odom_epoch"])}
                        if item.get("odom_epoch")
                        else {}
                    ),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    if circles:
        logger.info("keep-out zones loaded", count=len(circles), path=path)
    return circles


def _save_keep_out_circles(path: str, circles: list[dict[str, Any]]) -> None:
    """Atomically persist keep-out circles (tmp write + rename)."""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(circles, handle)
        os.replace(tmp, path)
    except Exception:
        logger.exception("keep-out save failed", path=path)


def _load_odom_epoch(path: str = _ODOM_EPOCH_PATH) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        epoch_id = str(data["epoch_id"])
        last_pose = data["last_pose"]
        return {
            "epoch_id": epoch_id,
            "updated_at": float(data["updated_at"]),
            "last_pose": (float(last_pose[0]), float(last_pose[1])),
        }
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None
    except Exception:
        logger.exception("odometry epoch load failed", path=path)
        return None


def _save_odom_epoch(
    epoch_id: str,
    pose: tuple[float, float],
    *,
    path: str = _ODOM_EPOCH_PATH,
    now: float | None = None,
) -> None:
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        payload = {
            "epoch_id": str(epoch_id),
            "updated_at": float(time.time() if now is None else now),
            "last_pose": [float(pose[0]), float(pose[1])],
        }
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except Exception:
        logger.exception("odometry epoch save failed", path=path)


def _resolve_odom_epoch(
    pose: tuple[float, float],
    *,
    path: str = _ODOM_EPOCH_PATH,
    now: float | None = None,
) -> str:
    """Reuse the WORLD frame only when odometry is spatially continuous."""
    current_time = time.time() if now is None else float(now)
    saved = _load_odom_epoch(path)
    continuous = bool(
        saved is not None
        and 0.0 <= current_time - saved["updated_at"] <= _ODOM_EPOCH_MAX_AGE_S
        and math.hypot(
            pose[0] - saved["last_pose"][0],
            pose[1] - saved["last_pose"][1],
        )
        <= _ODOM_EPOCH_CONTINUITY_M
    )
    epoch_id = str(saved["epoch_id"]) if continuous else uuid.uuid4().hex
    _save_odom_epoch(epoch_id, pose, path=path, now=current_time)
    logger.info(
        "odometry epoch resolved",
        epoch=epoch_id[:8],
        continued=continuous,
        x=round(pose[0], 2),
        y=round(pose[1], 2),
    )
    return epoch_id


def _query_world_to_map(reloc: RelocSpec | None) -> tuple[float, float, float] | None:
    """One reloc RPC to (x, y, yaw), or None while unlocked/unbound."""
    if reloc is None:
        return None
    try:
        payload = reloc.world_to_map_2d()
    except Exception:
        logger.exception("world->map transform query failed")
        return None
    if not payload:
        return None
    try:
        return (float(payload["x"]), float(payload["y"]), float(payload["yaw"]))
    except (KeyError, TypeError, ValueError):
        logger.exception("world->map transform payload malformed")
        return None


def _cached_world_to_map(host: Any) -> tuple[float, float, float] | None:
    """Cached world->map transform for a host that carries the cache attrs.

    Best-effort cache (a race just costs one extra RPC), read from ``_reloc``
    lazily because specs are injected after __init__.
    """
    now = time.monotonic()
    if now < getattr(host, "_world_to_map_cache_until", 0.0):
        return getattr(host, "_world_to_map_cache", None)
    result = _query_world_to_map(getattr(host, "_reloc", None))
    host._world_to_map_cache = result
    host._world_to_map_cache_until = now + _WORLD_TO_MAP_CACHE_S
    return result


def _world_to_map_point(
    wx: float, wy: float, transform: tuple[float, float, float]
) -> tuple[float, float]:
    """Project a WORLD point into the MAP frame: map = R @ world + t."""
    tx, ty, yaw = transform
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    return (cos_yaw * wx - sin_yaw * wy + tx, sin_yaw * wx + cos_yaw * wy + ty)


def _keep_out_circles_to_world(
    circles: list[dict[str, Any]], transform: tuple[float, float, float]
) -> list[tuple[float, float, float]]:
    """Project MAP-frame keep-out circles into WORLD: world = R^T @ (map - t)."""
    tx, ty, yaw = transform
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    world: list[tuple[float, float, float]] = []
    for circle in circles:
        dx = circle["x"] - tx
        dy = circle["y"] - ty
        wx = cos_yaw * dx + sin_yaw * dy
        wy = -sin_yaw * dx + cos_yaw * dy
        world.append((wx, wy, circle["radius_m"]))
    return world


def _point_in_keep_out(
    x: float, y: float, world_circles: list[tuple[float, float, float]]
) -> bool:
    return any(math.hypot(x - cx, y - cy) <= radius for cx, cy, radius in world_circles)


def _segment_crosses_keep_out(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    world_circles: list[tuple[float, float, float]],
    *,
    margin_m: float = 0.2,
) -> bool:
    """Return whether a direct route enters a keep-out circle.

    A robot already inside a circle may leave it. This matters when a keep-out
    is marked at the current pose or restored while the dog is beside glass.
    From outside, however, a frontier on the far side of the window is rejected
    even when its endpoint itself lies outside the circle.
    """
    dx = end_x - start_x
    dy = end_y - start_y
    length_sq = dx * dx + dy * dy
    for cx, cy, radius in world_circles:
        guarded_radius = radius + margin_m
        start_inside = math.hypot(start_x - cx, start_y - cy) <= guarded_radius
        end_inside = math.hypot(end_x - cx, end_y - cy) <= guarded_radius
        if end_inside:
            return True
        if start_inside:
            continue
        if length_sq <= 1e-9:
            continue
        projection = ((cx - start_x) * dx + (cy - start_y) * dy) / length_sq
        projection = min(1.0, max(0.0, projection))
        closest_x = start_x + projection * dx
        closest_y = start_y + projection * dy
        if math.hypot(closest_x - cx, closest_y - cy) <= guarded_radius:
            return True
    return False


def _goal_returns_toward_origin(
    goal_x: float,
    goal_y: float,
    robot_x: float,
    robot_y: float,
    origin: tuple[float, float] | None,
    *,
    armed: bool,
    radius_m: float,
    hysteresis_m: float = 0.25,
) -> bool:
    """Reject a goal inside the boot guard only when it moves inward.

    The boot guard stops the dog returning to the glass/window where a session
    began. A blanket circle also rejects every outward frontier until the dog
    is already four metres away, which can make leaving a room impossible.
    """
    if not armed or origin is None:
        return False
    goal_distance = math.hypot(goal_x - origin[0], goal_y - origin[1])
    if goal_distance >= radius_m:
        return False
    robot_distance = math.hypot(robot_x - origin[0], robot_y - origin[1])
    return goal_distance + hysteresis_m < robot_distance


def _prelock_world_keep_outs(
    circles: list[dict[str, Any]],
    pose: tuple[float, float] | None,
    odom_epoch: str | None = None,
) -> list[tuple[float, float, float]]:
    """Reuse WORLD coordinates only for a proven same-odometry restart."""
    if pose is None:
        return []
    result: list[tuple[float, float, float]] = []
    for circle in circles:
        if "world_x" not in circle or "world_y" not in circle:
            continue
        wx = float(circle["world_x"])
        wy = float(circle["world_y"])
        radius = float(circle["radius_m"])
        same_epoch = bool(
            odom_epoch
            and circle.get("odom_epoch")
            and str(circle["odom_epoch"]) == odom_epoch
        )
        # Legacy rows have no epoch identifier. Retain the old nearby-only
        # validation for those, while epoch-tagged exclusions stay active
        # after the dog has walked to another room.
        if same_epoch or math.hypot(pose[0] - wx, pose[1] - wy) <= max(
            3.0, radius + 1.0
        ):
            result.append((wx, wy, radius))
    return result


class WavefrontFrontierExplorer(_StockWavefrontFrontierExplorer):
    """Frontier selection with persistent novelty and temporary failure zones."""

    # Skim tuning: a visited goal blocks a 1.5 m region and worthwhile
    # frontiers are at least 1.5 m away, so the scout takes one coarse pass
    # per area instead of grinding out every sub-meter fragment.
    _repeat_radius_m = 1.5
    _failure_radius_m = 1.25
    _failure_ttl_s = 180.0
    # Recent visits stop the dog re-attempting a frontier it just left, but a
    # frontier still open minutes later deserves another try: the first attempt
    # clearly did not resolve it. Permanent visits let a handful of goals
    # blanket a small venue and seal off the doorway to unexplored terrain, so
    # visits expire on the same schedule as failure zones.
    # Four minutes was shorter than a single venue lap. Rooms therefore became
    # "novel" again while they were still visibly the same rooms, producing
    # repeated circles. Keep attempts for a full operating session; a doorway
    # may still reopen early through the substantial-unknown rule below.
    _visit_ttl_s = 30.0 * 60.0
    # Starvation relief: when every raw frontier is blacklisted (visited or
    # failed) but real map edges still exist, idling forever is the sealed-in
    # failure. After this long with no selectable frontier, release the safest
    # blacklisted candidate so exploration can breathe.
    _starvation_release_s = 20.0
    # Retry a visited frontier only when a room-sized mass of UNKNOWN remains
    # behind it. Chair gaps, wall slivers, and scanned rooms must not be
    # re-released merely because every better frontier is exhausted.
    _starvation_reopen_unknown_score = 0.25
    # Strike-based session-permanent blacklist. Time-based visit/failure TTLs
    # cannot stop an unresolvable "honeypot" frontier: a glass window lidar sees
    # through (the unknown behind it never resolves) so the dog returns every
    # time the 240 s visit or 180 s failure TTL expires, and a chair cluster
    # that physically traps the robot keeps failing. Worse, the starvation valve
    # re-releases such a region (observed live: failures=5, score=-9.331). A
    # region that accrues _strike_limit strikes is struck out for the rest of
    # the session and never offered as a goal again. Strikes never expire: the
    # window never stops being a window.
    _strike_region_m = 1.5
    _strike_limit = 3
    # A full-leg timeout is weaker evidence than a hard planner failure, so it
    # must not become a physical keep-out. Two timeouts in the same region are
    # enough to retire only that frontier for the rest of this session.
    _timeout_strike_limit = 2
    # A frontier whose nearby unknown cells were actually resolved is complete
    # for this session. Keep low-information fragments around it suppressed,
    # but allow a doorway that later exposes a genuinely large unknown region.
    _resolved_goal_radius_m = 2.0
    _resolved_reopen_unknown_score = 0.18
    _min_frontier_travel_m = 1.5
    # Unknown-mass radius: how far around a frontier centroid we count UNKNOWN
    # cells to estimate how much unmapped area reaching it would open. A doorway
    # into a dark room scores near 1.0; a chair-gap pocket ringed by known space
    # scores near 0. This dominates the ranking so the dog heads for corridors
    # that open new rooms instead of zigzagging between dead-end chair gaps
    # (user-observed zigzag, July 24).
    _unknown_radius_m = 2.0
    # Two-tier clearance. Wall-shadow ribbons (slivers of unknown behind
    # furniture and dead-end walls) have near-zero centroid clearance and stay
    # discarded below the hard floor: approaching them never resolves the
    # unknown. Doorway frontiers sit mid-gap where post-inflation clearance is
    # typically 0.25-0.45 m, so a single hard threshold at 0.40 banned every
    # doorway and rooms were never entered (verified live July 24). The hard
    # floor is now the Go2 body half-width plus margin (physically impassable
    # below it); frontiers in the [hard floor, tight) band stay selectable but
    # are penalized so open-space frontiers win whenever both exist.
    _min_frontier_clearance_m = 0.22
    _tight_clearance_m = 0.40
    # While traveling, a goal whose surrounding unknown has already been
    # resolved by lidar is worthless; abandon it instead of walking the rest
    # of the way to a wall.
    _invalidate_after_s = 3.0
    _invalidate_check_s = 2.0
    _invalidate_radius_m = 0.9
    # The boot position is an egress point, not a destination. At the venue the
    # robot is powered on beside the glass wall, so treating the session origin
    # like any other frontier makes every fresh process rediscover the same bad
    # region. Goals inside this radius are rejected when they move back toward
    # the origin; outward goals remain valid so the guard cannot seal the dog
    # in.
    _origin_goal_exclusion_radius_m = 4.0
    _origin_guard_arm_distance_m = 2.0
    # Persistent keep-out file (MAP frame). Stored as a class attribute because
    # the stock WavefrontConfig is dimos read-only and cannot take new fields.
    _keep_out_path = _KEEP_OUT_PATH

    # Optional: keep-out zones are dormant until relocalization locks, so a stack
    # without reloc still explores. Auto-wires by type to NightwatchRelocalization.
    _reloc: RelocSpec | None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._goal_visits: list[tuple[float, float, float]] = []
        self._resolved_goal_visits: list[tuple[float, float]] = []
        self._failure_zones: list[tuple[float, float, float, str]] = []
        self._region_strikes: dict[tuple[int, int], int] = {}
        self._timeout_strikes: dict[tuple[int, int], int] = {}
        self._coverage_lock = threading.RLock()
        self._starved_since: float | None = None
        self._active_goal: Vector3 | None = None
        self._active_goal_at = 0.0
        self._invalidation_stop = threading.Event()
        self._invalidation_thread: threading.Thread | None = None
        # Persistent keep-out state (MAP frame) plus the world->map cache.
        self._keep_outs: list[dict[str, Any]] = []
        self._world_to_map_cache: tuple[float, float, float] | None = None
        self._world_to_map_cache_until = 0.0
        self._session_origin: tuple[float, float] | None = None
        self._origin_exited = False
        self._odom_epoch_id: str | None = None
        self._odom_epoch_last_saved_at = 0.0

    @rpc
    def start(self) -> None:
        super().start()
        with self._coverage_lock:
            self._keep_outs = _load_keep_out_circles(self._keep_out_path)
        self._invalidation_stop.clear()
        self._invalidation_thread = threading.Thread(
            target=self._goal_invalidation_loop,
            name="frontier-goal-invalidation",
            daemon=True,
        )
        self._invalidation_thread.start()

    @rpc
    def stop(self) -> None:
        self._invalidation_stop.set()
        thread = self._invalidation_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._invalidation_thread = None
        super().stop()

    @rpc
    def stop_exploration(self, reason: str = "STOPPED") -> bool:
        """Stop the frontier loop without issuing a zero-distance nav goal.

        The stock stop method publishes the latest odometry as a goal. On the
        real robot that races with ``cancel_goal``: the planner accepts the
        current-pose goal, instantly reports success, and stale frontier state
        is struck as if glass had defeated navigation. Stuck recovery then
        "escapes" to the exact same location. The stop event and navigation
        cancellation already provide the two required wakeups, so a synthetic
        goal is both redundant and harmful.
        """
        with self._exploration_lock:
            thread = self.exploration_thread
            was_running = self.exploration_active or bool(thread and thread.is_alive())
            if not was_running:
                return False
            self.exploration_active = False
            self._completion_reason = reason
            self._completed_at = time.time()
            self.no_gain_counter = 0
            stop_event = self.stop_event
            stop_event.set()

        # Clear the selected frontier before waking the wait. Any delayed
        # planner terminal message after cancellation is lifecycle noise, not
        # evidence that this frontier is an unresolvable obstacle.
        self._active_goal = None
        self.goal_reached_event.set()
        logger.info("Stopped autonomous frontier exploration", reason=reason)
        return True

    def _on_goal_reached(self, msg: Any) -> None:
        # Stock wakes the exploration loop only on success (msg.data True).
        # A planner give-up published as False then costs the full
        # goal_timeout of dead waiting. Any terminal signal should advance
        # the loop to the next frontier immediately.
        #
        # Arrival-without-resolution strike: a real frontier resolves when the
        # lidar sweeps it on arrival, so its unknown neighborhood collapses. A
        # goal that terminates with its unknown mass INTACT is physically
        # unresolvable (glass: lidar passes through, the "unknown" behind the
        # pane never clears). Striking those arrivals makes glass walls
        # self-blacklisting even though every trip "succeeds", which failure-
        # only strikes never caught. Verified against tonight's window crash.
        # getattr: some tests exercise this hook on bare instances.
        goal = getattr(self, "_active_goal", None)
        costmap = getattr(self, "latest_costmap", None)
        if goal is not None and costmap is not None:
            try:
                if self._unknown_cells_near(costmap, goal) > 0:
                    with self._coverage_lock:
                        # A terminal planner failure after its own obstacle
                        # replans is hard evidence: the endpoint is physically
                        # unreachable even though lidar still sees unknown
                        # space beyond it (the glass-window signature). Retire
                        # it immediately instead of requiring three returns.
                        strikes = (
                            self._strike_limit
                            if getattr(msg, "data", None) is False
                            else 1
                        )
                        for _ in range(strikes):
                            self._add_region_strike_locked(goal.x, goal.y)
                    logger.info(
                        "goal terminated with unknown unresolved; striking",
                        x=round(goal.x, 2),
                        y=round(goal.y, 2),
                        hard_failure=getattr(msg, "data", None) is False,
                    )
                else:
                    self._remember_resolved_goal(goal)
            except Exception:
                logger.exception("unresolved-arrival strike check failed")
            self._active_goal = None
        self.goal_reached_event.set()

    def _on_odometry(self, msg: Any) -> None:
        """Remember the boot position once, then retain normal stock behavior."""
        super()._on_odometry(msg)
        try:
            pose = (float(msg.position.x), float(msg.position.y))
            now = time.time()
            if self._odom_epoch_id is None:
                self._odom_epoch_id = _resolve_odom_epoch(pose)
                self._odom_epoch_last_saved_at = now
            elif now - self._odom_epoch_last_saved_at >= _ODOM_EPOCH_SAVE_S:
                _save_odom_epoch(self._odom_epoch_id, pose, now=now)
                self._odom_epoch_last_saved_at = now
        except Exception:
            logger.exception("odometry epoch update failed")
        if self._session_origin is None:
            self._session_origin = (
                float(msg.position.x),
                float(msg.position.y),
            )
            logger.info(
                "session egress origin captured",
                x=round(self._session_origin[0], 2),
                y=round(self._session_origin[1], 2),
                exclusion_radius_m=self._origin_goal_exclusion_radius_m,
            )
        elif (
            not self._origin_exited
            and math.hypot(
                float(msg.position.x) - self._session_origin[0],
                float(msg.position.y) - self._session_origin[1],
            )
            >= self._origin_guard_arm_distance_m
        ):
            self._origin_exited = True
            logger.info(
                "session egress complete; origin return guard armed",
                x=round(self._session_origin[0], 2),
                y=round(self._session_origin[1], 2),
                exclusion_radius_m=self._origin_goal_exclusion_radius_m,
            )

    def _unknown_cells_near(self, costmap: OccupancyGrid, goal: Vector3) -> int:
        grid_pos = costmap.world_to_grid(goal)
        gx, gy = int(grid_pos.x), int(grid_pos.y)
        radius = max(1, int(self._invalidate_radius_m / costmap.resolution))
        x0, x1 = max(0, gx - radius), min(costmap.width, gx + radius + 1)
        y0, y1 = max(0, gy - radius), min(costmap.height, gy + radius + 1)
        if x0 >= x1 or y0 >= y1:
            return 0
        window = costmap.grid[y0:y1, x0:x1]
        return int(np.count_nonzero(window == -1))

    def _build_unknown_mass(
        self, costmap: OccupancyGrid
    ) -> tuple[np.ndarray, int, float]:
        """Integral image of UNKNOWN cells for O(1) box sums per frontier.

        UNKNOWN is ``grid < 0`` (dimos ``CostValues.UNKNOWN == -1``, the same
        convention ``_unknown_cells_near`` uses). Returns the padded integral
        image, the box half-side in cells, and the full un-clipped box cell
        count that normalizes the fraction. A single cumsum-twice pass replaces
        a per-frontier scan of the whole grid.
        """
        unknown_mask = costmap.grid < 0
        integral = np.zeros((costmap.height + 1, costmap.width + 1), dtype=np.int64)
        integral[1:, 1:] = unknown_mask.astype(np.int64).cumsum(axis=0).cumsum(axis=1)
        half = max(1, int(self._unknown_radius_m / costmap.resolution))
        full_box_cells = float((2 * half + 1) ** 2)
        return integral, half, full_box_cells

    def _unknown_mass_score(
        self,
        integral: np.ndarray,
        half: int,
        full_box_cells: float,
        gx: int,
        gy: int,
        width: int,
        height: int,
    ) -> float:
        """Fraction of the box around ``(gx, gy)`` that is UNKNOWN.

        1.0 means the whole neighborhood is unmapped (a doorway into a dark
        region); near 0 means a pocket surrounded by known space (a chair gap).
        Normalized by the full box, so a centroid near the grid edge whose box
        is clipped scores lower, which is correct: less unknown is reachable.
        """
        x0 = max(0, gx - half)
        x1 = min(width - 1, gx + half)
        y0 = max(0, gy - half)
        y1 = min(height - 1, gy + half)
        if x0 > x1 or y0 > y1:
            return 0.0
        unknown_cells = int(
            integral[y1 + 1, x1 + 1]
            - integral[y0, x1 + 1]
            - integral[y1 + 1, x0]
            + integral[y0, x0]
        )
        return min(unknown_cells / full_box_cells, 1.0)

    def _goal_invalidation_loop(self) -> None:
        while not self._invalidation_stop.wait(self._invalidate_check_s):
            goal = self._active_goal
            costmap = self.latest_costmap
            if goal is None or costmap is None:
                continue
            if not self.exploration_active or self.goal_reached_event.is_set():
                continue
            if time.time() - self._active_goal_at < self._invalidate_after_s:
                continue
            try:
                world_circles = self._keep_out_world_circles()
                origin = getattr(self, "_session_origin", None)
                odom = getattr(self, "latest_odometry", None)
                returning_to_origin = bool(
                    odom is not None
                    and _goal_returns_toward_origin(
                        goal.x,
                        goal.y,
                        float(odom.position.x),
                        float(odom.position.y),
                        origin,
                        armed=bool(getattr(self, "_origin_exited", False)),
                        radius_m=self._origin_goal_exclusion_radius_m,
                    )
                )
                if returning_to_origin:
                    logger.info(
                        "origin return guard armed en route; abandoning goal",
                        x=round(goal.x, 2),
                        y=round(goal.y, 2),
                    )
                    self._active_goal = None
                    self.goal_reached_event.set()
                elif world_circles and _point_in_keep_out(
                    goal.x, goal.y, world_circles
                ):
                    logger.info(
                        "active goal now inside keep-out; abandoning",
                        x=round(goal.x, 2),
                        y=round(goal.y, 2),
                    )
                    self._active_goal = None
                    self.goal_reached_event.set()
                elif self._unknown_cells_near(costmap, goal) == 0:
                    logger.info(
                        "frontier resolved en route; abandoning goal early",
                        x=round(goal.x, 2),
                        y=round(goal.y, 2),
                    )
                    self._remember_resolved_goal(goal)
                    self._active_goal = None
                    self.goal_reached_event.set()
            except Exception:
                logger.exception("frontier goal invalidation check failed")

    @rpc
    def report_navigation_failure(
        self, x: float, y: float, reason: str = "navigation failure"
    ) -> bool:
        """Temporarily penalize a region that just caused a stuck/loop event."""
        now = time.time()
        with self._coverage_lock:
            self._prune_failures_locked(now)
            self._failure_zones.append((float(x), float(y), now, str(reason)))
            # Each spatial failure also strikes the region permanently: three
            # failures in one place mean it never resolves, not that the TTL
            # should keep resetting.
            self._add_region_strike_locked(float(x), float(y))
        logger.warning(
            "exploration feedback: temporary failure zone",
            x=round(float(x), 2),
            y=round(float(y), 2),
            reason=reason,
        )
        return True

    @rpc
    def coverage_status(self) -> dict[str, Any]:
        now = time.time()
        with self._coverage_lock:
            self._prune_failures_locked(now)
            return {
                "goal_attempts": len(self._goal_visits),
                "temporary_failure_zones": len(self._failure_zones),
                "unique_goal_regions": len(
                    {
                        (
                            round(x / self._repeat_radius_m),
                            round(y / self._repeat_radius_m),
                        )
                        for x, y, _ts in self._goal_visits
                    }
                ),
                "resolved_goal_regions": len(
                    getattr(self, "_resolved_goal_visits", [])
                ),
            }

    def _remember_resolved_goal(self, goal: Vector3) -> None:
        """Remember genuinely completed room coverage for the whole session."""
        with self._coverage_lock:
            resolved = getattr(self, "_resolved_goal_visits", None)
            if resolved is None:
                resolved = []
                self._resolved_goal_visits = resolved
            if not any(
                math.hypot(float(goal.x) - x, float(goal.y) - y)
                < self._resolved_goal_radius_m * 0.5
                for x, y in resolved
            ):
                resolved.append((float(goal.x), float(goal.y)))

    def _prune_failures_locked(self, now: float) -> None:
        failure_cutoff = now - self._failure_ttl_s
        self._failure_zones = [
            item for item in self._failure_zones if item[2] >= failure_cutoff
        ]
        # Visits expire too: a frontier still open past the visit TTL was not
        # resolved by the earlier attempt and must become selectable again.
        visit_cutoff = now - self._visit_ttl_s
        self._goal_visits = [
            item for item in self._goal_visits if item[2] >= visit_cutoff
        ]

    def _keep_out_world_circles(self) -> list[tuple[float, float, float]]:
        """Current keep-out circles projected into the session WORLD frame.

        Before relocalization locks, a saved WORLD fallback is accepted only
        when the robot is initially close enough to validate that this is the
        same odometry boot. Once validated, it stays active for the process;
        otherwise it would disappear as soon as the dog successfully walked
        away and permit a later route back to the glass.
        """
        with self._coverage_lock:
            # getattr: ranking tests build explorers with object.__new__.
            circles = list(getattr(self, "_keep_outs", []) or [])
            pending = list(getattr(self, "_pending_world_keep_outs", []) or [])
        transform = _cached_world_to_map(self)
        if pending and transform is not None:
            # The lock arrived: anchor provisional world circles permanently.
            with self._coverage_lock:
                for wx, wy, radius in pending:
                    map_x, map_y = _world_to_map_point(wx, wy, transform)
                    self._keep_outs.append(
                        {
                            "x": float(map_x),
                            "y": float(map_y),
                            "radius_m": float(radius),
                            "created": time.time(),
                            "world_x": float(wx),
                            "world_y": float(wy),
                        }
                    )
                self._pending_world_keep_outs = []
                circles = list(self._keep_outs)
                snapshot = list(self._keep_outs)
            _save_keep_out_circles(self._keep_out_path, snapshot)
            logger.info("pending keep-outs anchored to map frame", count=len(pending))
            pending = []
        world: list[tuple[float, float, float]] = list(pending)
        if circles and transform is not None:
            world.extend(_keep_out_circles_to_world(circles, transform))
        elif circles:
            validated = _prelock_world_keep_outs(
                circles,
                self._latest_robot_pose(),
                getattr(self, "_odom_epoch_id", None),
            )
            if validated:
                self._validated_prelock_world_keepouts = validated
            world.extend(list(getattr(self, "_validated_prelock_world_keepouts", [])))
        return world

    def _append_keep_out_map_circle(
        self,
        map_x: float,
        map_y: float,
        radius_m: float,
        *,
        world_x: float | None = None,
        world_y: float | None = None,
    ) -> None:
        """Persist one MAP-frame keep-out circle and apply it immediately."""
        circle = {
            "x": float(map_x),
            "y": float(map_y),
            "radius_m": float(radius_m),
            "created": time.time(),
            **(
                {"odom_epoch": self._odom_epoch_id}
                if getattr(self, "_odom_epoch_id", None)
                else {}
            ),
            **(
                {"world_x": float(world_x), "world_y": float(world_y)}
                if world_x is not None and world_y is not None
                else {}
            ),
        }
        with self._coverage_lock:
            self._keep_outs.append(circle)
            snapshot = list(self._keep_outs)
        _save_keep_out_circles(self._keep_out_path, snapshot)

    @skill
    def mark_keep_out_here(self, radius_m: float = 4.0) -> str:
        """Permanently forbid exploration around the robot's current spot.

        Records a MAP-frame circle that survives every restart. The planner
        can still drive through it; frontier selection and patrol never
        target it again. Requires relocalization lock, because a keep-out
        recorded in an unanchored frame would be garbage.
        """
        pose = self._latest_robot_pose()
        if pose is None:
            return "No robot pose yet; cannot mark a keep-out."
        transform = _cached_world_to_map(self)
        if transform is None:
            # Pre-lock: apply immediately in THIS session's world frame (it is
            # self-consistent within the session) and anchor it permanently
            # the moment relocalization locks. Without this the glass honeypot
            # recaptures the dog before it ever walks far enough to lock.
            with self._coverage_lock:
                pending = getattr(self, "_pending_world_keep_outs", None)
                if pending is None:
                    pending = []
                    self._pending_world_keep_outs = pending
                pending.append((float(pose[0]), float(pose[1]), float(radius_m)))
            logger.info(
                "keep-out marked provisionally (world frame, pre-lock)",
                x=round(pose[0], 2),
                y=round(pose[1], 2),
                radius_m=radius_m,
            )
            return (
                f"Keep-out active NOW at ({pose[0]:.2f}, {pose[1]:.2f}) "
                f"radius {radius_m:.1f} m; it becomes permanent once the "
                "saved map locks."
            )
        map_x, map_y = _world_to_map_point(pose[0], pose[1], transform)
        self._append_keep_out_map_circle(
            map_x,
            map_y,
            float(radius_m),
            world_x=pose[0],
            world_y=pose[1],
        )
        logger.info(
            "keep-out marked",
            map_x=round(map_x, 2),
            map_y=round(map_y, 2),
            radius_m=radius_m,
        )
        return (
            f"Keep-out recorded at map ({map_x:.2f}, {map_y:.2f}) "
            f"radius {radius_m:.1f} m; this zone is off limits forever."
        )

    @skill
    def mark_keep_out_at(
        self, x: float, y: float, radius_m: float = 1.5
    ) -> str:
        """Exclude a known hazardous endpoint reported by live navigation."""
        x = float(x)
        y = float(y)
        radius_m = min(4.0, max(0.5, float(radius_m)))
        transform = _cached_world_to_map(self)
        if transform is not None:
            map_x, map_y = _world_to_map_point(x, y, transform)
        else:
            # Until ICP locks, x/y are placeholders; world_x/world_y plus the
            # odometry epoch are the authoritative same-boot coordinates.
            map_x, map_y = x, y
        self._append_keep_out_map_circle(
            map_x,
            map_y,
            radius_m,
            world_x=x,
            world_y=y,
        )
        logger.warning(
            "hazard endpoint marked keep-out",
            x=round(x, 2),
            y=round(y, 2),
            radius_m=radius_m,
            epoch=(self._odom_epoch_id or "")[:8],
        )
        return (
            f"Hazard endpoint ({x:.2f}, {y:.2f}) excluded with radius "
            f"{radius_m:.1f} m for this odometry epoch and the persistent map."
        )

    def _latest_robot_pose(self) -> tuple[float, float] | None:
        """Latest odometry position, if the stream has produced one."""
        odom = getattr(self, "latest_odometry", None)
        if odom is None:
            return None
        try:
            return (float(odom.position.x), float(odom.position.y))
        except AttributeError:
            return None

    def _strike_region_key(self, x: float, y: float) -> tuple[int, int]:
        return (
            round(x / self._strike_region_m),
            round(y / self._strike_region_m),
        )

    def _add_region_strike_locked(self, x: float, y: float) -> None:
        """Add a session-permanent strike to the region of (x, y).

        Caller holds ``_coverage_lock``. Unlike visits and failure zones there
        is no TTL: a region that repeatedly fails or is repeatedly released by
        the starvation valve is an unresolvable honeypot and must stop being an
        exploration target for good. A struck-out region can still be traversed
        by the planner; only frontier selection ignores it.
        """
        strikes = getattr(self, "_region_strikes", None)
        if strikes is None:
            strikes = {}
            self._region_strikes = strikes
        key = self._strike_region_key(x, y)
        count = strikes.get(key, 0) + 1
        strikes[key] = count
        if count == self._strike_limit:
            region_x = key[0] * self._strike_region_m
            region_y = key[1] * self._strike_region_m
            logger.info(
                "region struck out for the session",
                x=round(region_x, 2),
                y=round(region_y, 2),
                strikes=count,
            )
            # Promote to a PERMANENT map-frame keep-out when the transform is
            # available: session strikes reset on every boot, which is how the
            # glass-window honeypot kept coming back. The RPC to the reloc
            # module is safe under this lock (no call path back into us).
            transform = _cached_world_to_map(self)
            if transform is not None:
                map_x, map_y = _world_to_map_point(region_x, region_y, transform)
                circle = {
                    "x": float(map_x),
                    "y": float(map_y),
                    "radius_m": float(self._strike_region_m),
                    "created": time.time(),
                    "world_x": float(region_x),
                    "world_y": float(region_y),
                    **(
                        {"odom_epoch": self._odom_epoch_id}
                        if getattr(self, "_odom_epoch_id", None)
                        else {}
                    ),
                }
                self._keep_outs.append(circle)
                _save_keep_out_circles(self._keep_out_path, list(self._keep_outs))
                logger.info(
                    "struck region promoted to permanent keep-out",
                    map_x=round(map_x, 2),
                    map_y=round(map_y, 2),
                )
            elif getattr(self, "_odom_epoch_id", None):
                # The saved-map alignment is deliberately conservative and
                # may remain unlocked. Persist the WORLD exclusion against a
                # proven odometry epoch so a stack restart cannot resurrect
                # the same window target.
                circle = {
                    "x": float(region_x),
                    "y": float(region_y),
                    "radius_m": float(self._strike_region_m),
                    "created": time.time(),
                    "world_x": float(region_x),
                    "world_y": float(region_y),
                    "odom_epoch": self._odom_epoch_id,
                }
                self._keep_outs.append(circle)
                _save_keep_out_circles(self._keep_out_path, list(self._keep_outs))
                logger.info(
                    "struck region persisted in odometry epoch",
                    x=round(region_x, 2),
                    y=round(region_y, 2),
                    epoch=self._odom_epoch_id[:8],
                )

    def _retire_timed_out_active_goal(self) -> None:
        """Penalize the prior goal when the stock wait expired.

        Goal callbacks and en-route resolution clear ``_active_goal``. If it is
        still present when the loop requests another goal, the full goal wait
        expired. Preserve that evidence beyond visit-TTL expiry so late-stage
        cleanup cannot circle the same unreachable edge forever.
        """
        goal = getattr(self, "_active_goal", None)
        if goal is None:
            return
        now = time.time()
        with self._coverage_lock:
            self._prune_failures_locked(now)
            self._failure_zones.append(
                (float(goal.x), float(goal.y), now, "goal_timeout")
            )
            key = self._strike_region_key(goal.x, goal.y)
            timeout_strikes = getattr(self, "_timeout_strikes", None)
            if timeout_strikes is None:
                timeout_strikes = {}
                self._timeout_strikes = timeout_strikes
            count = timeout_strikes.get(key, 0) + 1
            timeout_strikes[key] = count
        self._active_goal = None
        logger.warning(
            "frontier goal timed out; region penalized",
            x=round(goal.x, 2),
            y=round(goal.y, 2),
            timeout_strikes=count,
            retired=count >= self._timeout_strike_limit,
        )

    def mark_explored_goal(self, goal: Vector3) -> None:
        """Record one attempt once.

        The upstream implementation appends the same goal twice, doubling its
        history and making all coverage diagnostics misleading.
        """
        self.explored_goals.append(goal)
        with self._coverage_lock:
            self._goal_visits.append((float(goal.x), float(goal.y), time.time()))

    def _compute_comprehensive_frontier_score(
        self,
        frontier: Vector3,
        frontier_size: int,
        robot_pose: Vector3,
        costmap: OccupancyGrid,
    ) -> float:
        base = super()._compute_comprehensive_frontier_score(
            frontier, frontier_size, robot_pose, costmap
        )
        now = time.time()
        with self._coverage_lock:
            self._prune_failures_locked(now)
            visits = sum(
                math.hypot(frontier.x - x, frontier.y - y) < self._repeat_radius_m
                for x, y, _ts in self._goal_visits
            )
            failures = sum(
                math.hypot(frontier.x - x, frontier.y - y) < self._failure_radius_m
                for x, y, _ts, _reason in self._failure_zones
            )
            if self._goal_visits:
                novelty_m = min(
                    math.hypot(frontier.x - x, frontier.y - y)
                    for x, y, _ts in self._goal_visits
                )
            else:
                novelty_m = self.config.max_explored_distance

        # Novel regions dominate small differences in frontier perimeter.
        novelty_bonus = min(
            novelty_m / max(0.1, self.config.max_explored_distance), 1.0
        )
        return base + 0.8 * novelty_bonus - 0.45 * visits - 1.2 * failures

    def _rank_frontiers(
        self,
        frontier_centroids: list[Vector3],
        frontier_sizes: list[int],
        robot_pose: Vector3,
        costmap: OccupancyGrid,
    ) -> list[Vector3]:
        """Rank true map edges in one vectorized pass.

        Upstream searches a ``safe_distance`` square around every frontier.
        With 50-70 frontiers at 5 cm resolution that is millions of Python
        iterations, leaving the robot stationary for several seconds.  A
        single Euclidean distance transform gives the same obstacle-clearance
        information for the complete grid.

        Clean, unvisited frontiers form a single tier sorted by score, and
        score is dominated by the UNKNOWN mass a frontier would open, so a
        corridor across the room outranks a nearby chair-gap pocket.  Visited or
        failed frontiers are never re-selected outside the starvation override.
        """
        if not frontier_centroids:
            self._starved_since = None
            return []

        obstacle_mask = costmap.grid >= self.config.occupancy_threshold
        if np.any(obstacle_mask):
            clearance = distance_transform_edt(~obstacle_mask) * costmap.resolution
        else:
            clearance = np.full(
                costmap.grid.shape, self.config.safe_distance, dtype=np.float32
            )

        # Unknown-mass map: build the integral image of UNKNOWN cells once so
        # each frontier's neighborhood is scored with a single box lookup
        # instead of a per-frontier scan of the whole grid.
        unknown_integral, unknown_half, unknown_full_box = self._build_unknown_mass(
            costmap
        )

        now = time.time()
        with self._coverage_lock:
            self._prune_failures_locked(now)
            visits_snapshot = list(self._goal_visits)
            failures_snapshot = list(self._failure_zones)
            strikes_snapshot = dict(getattr(self, "_region_strikes", {}))
            timeout_strikes_snapshot = dict(getattr(self, "_timeout_strikes", {}))
            resolved_snapshot = list(getattr(self, "_resolved_goal_visits", []))

        max_frontier_size = max(
            1.0,
            self.config.min_frontier_perimeter / costmap.resolution * 10.0,
        )
        ranked: list[tuple[Vector3, float, bool, bool, float, int, int, float]] = []
        # Discard tallies so the next live log explains an empty ranking.
        low_clearance = 0
        struck = 0
        keep_out = 0
        keep_out_route = 0
        failure_route = 0
        origin_excluded = 0
        tight_band = 0
        visited = 0
        failed = 0
        resolved_fragments = 0
        # Permanent map-frame keep-out circles, projected once per call.
        # Dormant until relocalization locks (no transform, no circles).
        keep_out_circles = self._keep_out_world_circles()
        failure_circles = [
            (x, y, self._failure_radius_m)
            for x, y, _ts, _reason in failures_snapshot
        ]
        session_origin = getattr(self, "_session_origin", None)
        origin_guard_armed = bool(getattr(self, "_origin_exited", False))
        for index, frontier in enumerate(frontier_centroids):
            if _goal_returns_toward_origin(
                frontier.x,
                frontier.y,
                robot_pose.x,
                robot_pose.y,
                session_origin,
                armed=origin_guard_armed,
                radius_m=self._origin_goal_exclusion_radius_m,
            ):
                origin_excluded += 1
                continue
            if keep_out_circles and _point_in_keep_out(
                frontier.x, frontier.y, keep_out_circles
            ):
                # Forever-forbidden zone (glass honeypot, operator-marked).
                # Discarded before any scoring so it can never be selected,
                # released by starvation, or re-learned after a restart.
                keep_out += 1
                continue
            if keep_out_circles and _segment_crosses_keep_out(
                robot_pose.x,
                robot_pose.y,
                frontier.x,
                frontier.y,
                keep_out_circles,
            ):
                # Do not select a point beyond the glass merely because the
                # endpoint itself lies outside the exclusion circle.
                keep_out_route += 1
                continue
            if failure_circles and _segment_crosses_keep_out(
                robot_pose.x,
                robot_pose.y,
                frontier.x,
                frontier.y,
                failure_circles,
                margin_m=0.0,
            ):
                # A different endpoint beyond the same chair/glass/corridor
                # trap is not novel exploration. The old endpoint-only penalty
                # kept routing through the failed region and produced several
                # distinct-looking stalls along one obstacle boundary.
                failure_route += 1
                continue
            robot_distance = math.hypot(
                frontier.x - robot_pose.x, frontier.y - robot_pose.y
            )
            visit_distances = [
                math.hypot(frontier.x - x, frontier.y - y)
                for x, y, _ts in visits_snapshot
            ]
            novelty_m = (
                min(visit_distances)
                if visit_distances
                else self.config.max_explored_distance
            )
            visits = sum(
                distance < self._repeat_radius_m for distance in visit_distances
            )
            failures = sum(
                math.hypot(frontier.x - x, frontier.y - y) < self._failure_radius_m
                for x, y, _ts, _reason in failures_snapshot
            )

            grid_pos = costmap.world_to_grid(frontier)
            gx, gy = int(grid_pos.x), int(grid_pos.y)
            clearance_m = (
                float(clearance[gy, gx])
                if 0 <= gx < costmap.width and 0 <= gy < costmap.height
                else 0.0
            )
            if clearance_m < self._min_frontier_clearance_m:
                # Below the hard floor: physically impassable for the Go2 body.
                # Wall-shadow ribbons live here (near-zero centroid clearance)
                # and stay discarded outright.
                low_clearance += 1
                continue
            if (
                strikes_snapshot.get(self._strike_region_key(frontier.x, frontier.y), 0)
                >= self._strike_limit
                or timeout_strikes_snapshot.get(
                    self._strike_region_key(frontier.x, frontier.y), 0
                )
                >= self._timeout_strike_limit
            ):
                # Session-permanent strike-out: an unresolvable honeypot region
                # (glass window, chair trap) that repeatedly failed or was
                # repeatedly released by the starvation valve. Hard discard so
                # it never enters `ranked` and is therefore invisible to the
                # override as well. The planner can still traverse it; only
                # frontier selection ignores it. When every remaining frontier
                # is struck out, ranking returns empty and the explorer
                # converges to NO_FRONTIERS, which is the correct end state when
                # only junk frontiers remain on an otherwise-mapped floor.
                struck += 1
                continue
            tight = clearance_m < self._tight_clearance_m
            if tight:
                # Doorway-width gap: keep it so unexplored rooms are entered,
                # but zero its obstacle_score and dock the final score so an
                # open-space frontier is preferred whenever one exists.
                tight_band += 1
            if visits:
                visited += 1
            if failures:
                failed += 1
            obstacle_score = (
                0.0
                if tight
                else min(clearance_m / max(0.1, self.config.safe_distance), 1.0)
            )
            # Fraction of the neighborhood that is UNKNOWN: ~1.0 for a doorway
            # into a dark room, near 0 for a chair-gap pocket ringed by known
            # space. A box approximation of the disk is fine here.
            unknown_score = self._unknown_mass_score(
                unknown_integral,
                unknown_half,
                unknown_full_box,
                gx,
                gy,
                costmap.width,
                costmap.height,
            )
            resolved_near = any(
                math.hypot(frontier.x - x, frontier.y - y)
                < self._resolved_goal_radius_m
                for x, y in resolved_snapshot
            )
            if resolved_near and unknown_score < self._resolved_reopen_unknown_score:
                # The room around this point has already been observed and the
                # remaining unknown mass is only a small crevice. A doorway
                # with substantial unknown beyond it passes this threshold.
                resolved_fragments += 1
                continue
            frontier_size = frontier_sizes[index] if index < len(frontier_sizes) else 1
            info_score = min(float(frontier_size) / max_frontier_size, 1.0)
            novelty_score = min(
                novelty_m / max(0.1, self.config.max_explored_distance), 1.0
            )
            # Range: reward FAR frontiers so the dog keeps pushing outward to
            # find people instead of huddling near its start (user requirement,
            # July 24 evening). Unknown mass still dominates; range only flips
            # the tiebreak from near to far when two frontiers open similar area.
            range_anchor = session_origin or (robot_pose.x, robot_pose.y)
            outward_distance = math.hypot(
                frontier.x - range_anchor[0],
                frontier.y - range_anchor[1],
            )
            range_score = min(
                outward_distance / max(0.1, self.config.max_explored_distance),
                1.0,
            )
            momentum_score = self._compute_direction_momentum_score(
                frontier, robot_pose
            )
            # Unknown mass dominates: a corridor or doorway that opens a whole
            # unmapped region beats a chair-gap pocket surrounded by known
            # space, even when the pocket is closer (user-observed zigzag,
            # July 24). Perimeter, novelty, range, clearance, and momentum only
            # break ties between frontiers that open similar unknown area.
            score = (
                0.40 * unknown_score
                + 0.12 * info_score
                + 0.10 * novelty_score
                + 0.10 * range_score
                + 0.10 * obstacle_score
                + 0.08 * momentum_score
                - 0.75 * visits
                - 1.75 * failures
                - (0.15 if tight else 0.0)
            )
            far_enough = robot_distance >= self._min_frontier_travel_m
            clean = far_enough and visits == 0 and failures == 0
            # Near frontiers stay reachable at session start, but a frontier
            # that was already visited or failed is never worth a second trip:
            # falling back to it is exactly the visible circling behavior.
            # With no unvisited frontiers left, ranking returns empty, the
            # explorer converges to NO_FRONTIERS, and the map is declared
            # complete. Small residual holes are filled while wandering.
            usable = robot_distance >= 0.60 and visits == 0 and failures == 0
            ranked.append(
                (
                    frontier,
                    score,
                    clean,
                    usable,
                    robot_distance,
                    visits,
                    failures,
                    unknown_score,
                )
            )

        clean = [item for item in ranked if item[2]]
        usable_ranked = [item for item in ranked if item[3] and not item[2]]
        # One clean tier, sorted by score. Because unknown mass dominates the
        # score, a corridor 8 m away that opens a room outranks a chair gap
        # 1.5 m away: distance no longer forms a separate tier.
        clean.sort(key=lambda item: item[1], reverse=True)
        usable_ranked.sort(key=lambda item: item[1], reverse=True)
        selected_pool = clean + usable_ranked
        logger.info(
            "edge-first frontier ranking",
            raw=len(frontier_centroids),
            frontiers=len(ranked),
            low_clearance=low_clearance,
            struck=struck,
            keep_out=keep_out,
            keep_out_route=keep_out_route,
            failure_route=failure_route,
            origin_excluded=origin_excluded,
            tight=tight_band,
            visited=visited,
            failed=failed,
            resolved_fragments=resolved_fragments,
            clean=len(clean),
            usable_candidates=len(usable_ranked),
            vectorized=True,
        )
        if selected_pool:
            self._starved_since = None
            return [item[0] for item in selected_pool]

        if ranked:
            # Every surviving frontier is blacklisted but real map edges remain.
            # Time-box the starvation, then release the safest candidate so
            # exploration never seals itself in.
            starved_since = getattr(self, "_starved_since", None)
            if starved_since is None:
                self._starved_since = now
                return []
            if now - starved_since < self._starvation_release_s:
                return []
            # Release only a visits-only doorway with substantial unknown
            # space beyond it. Failure zones and low-information fragments are
            # evidence to stop, not an excuse to circle back.
            override = [
                item
                for item in ranked
                if item[4] >= 0.60
                and item[6] == 0
                and item[7] >= self._starvation_reopen_unknown_score
            ]
            if not override:
                self._starved_since = None
                return []
            override.sort(key=lambda item: (item[7], item[1]), reverse=True)
            best = override[0]
            self._starved_since = None
            # A release is another attempt at a region that already proved
            # unproductive. Strike it: three releases without progress means it
            # never resolves, and the next ranking will hard-discard it instead
            # of the valve re-releasing the same honeypot forever.
            with self._coverage_lock:
                self._add_region_strike_locked(best[0].x, best[0].y)
            logger.info(
                "frontier starvation override: releasing blacklisted frontier",
                x=round(best[0].x, 2),
                y=round(best[0].y, 2),
                visits=best[5],
                failures=best[6],
                unknown_score=round(best[7], 3),
                score=round(best[1], 3),
            )
            return [best[0]]

        # No frontier survived the clearance filter: true NO_FRONTIERS
        # convergence. The map-complete regression tests depend on this staying
        # an empty return with no override.
        self._starved_since = None
        return []

    def get_exploration_goal(
        self, robot_pose: Vector3, costmap: OccupancyGrid
    ) -> Vector3 | None:
        """Select from actual frontiers; do not use rolling information delta.

        Map completion is decided only by the explorer's sustained
        ``NO_FRONTIERS`` convergence. A transient or negative known-cell delta
        is diagnostic evidence, never a completion signal.
        """
        self._retire_timed_out_active_goal()
        frontiers = self.detect_frontiers(robot_pose, costmap)
        self.last_costmap = costmap  # retained for diagnostics only
        self.no_gain_counter = 0
        if not frontiers:
            self._active_goal = None
            return None
        selected = frontiers[0]
        self._update_exploration_direction(robot_pose, selected)
        self.mark_explored_goal(selected)
        self._active_goal = selected
        self._active_goal_at = time.time()
        return selected

    @rpc
    def exploration_status(self) -> dict[str, Any]:
        status = super().exploration_status()
        status.update(self.coverage_status())
        status["map_complete"] = status.get("completion_reason") == "NO_FRONTIERS"
        return status


class NightwatchCoveragePatrolRouter(CoveragePatrolRouter):
    """Coverage router that preserves history and keeps narrow safe corridors."""

    _occupancy_grid_min_update_interval_s = 2.0
    _candidates_to_consider = 64
    _origin_goal_exclusion_radius_m = 4.0
    _origin_guard_arm_distance_m = 2.0
    _recent_goal_exclusion_radius_m = 2.0
    _recent_goal_limit = 64
    _failure_exclusion_radius_m = 2.0

    def __init__(self, clearance_radius_m: float) -> None:
        super().__init__(clearance_radius_m)
        self._session_origin: tuple[float, float] | None = None
        self._origin_exited = False
        self._recent_goals: list[tuple[float, float]] = []
        self._persistent_keep_outs: list[tuple[float, float, float]] = []
        self._session_failure_zones: list[tuple[float, float, float]] = []

    def set_keep_out_circles(self, circles: list[tuple[float, float, float]]) -> None:
        """Replace persistent WORLD-frame exclusions used by patrol."""
        with self._lock:
            self._persistent_keep_outs = list(circles)

    def report_failure_zone(self, x: float, y: float) -> None:
        """Exclude a navigation failure immediately for the rest of this run."""
        zone = (float(x), float(y), self._failure_exclusion_radius_m)
        with self._lock:
            if any(
                math.hypot(zone[0] - cx, zone[1] - cy) < radius
                for cx, cy, radius in self._session_failure_zones
            ):
                return
            self._session_failure_zones.append(zone)

    @staticmethod
    def _path_reenters_exclusion(
        path: Any,
        circles: list[tuple[float, float, float]],
    ) -> bool:
        """Reject entry into a zone, while allowing a robot already inside to exit."""
        for cx, cy, radius in circles:
            outside_seen = False
            for pose in path.poses:
                inside = (
                    math.hypot(
                        float(pose.position.x) - cx,
                        float(pose.position.y) - cy,
                    )
                    <= radius
                )
                if not inside:
                    outside_seen = True
                elif outside_seen:
                    return True
        return False

    def handle_odom(self, msg: Any) -> None:
        super().handle_odom(msg)
        with self._lock:
            if self._session_origin is None:
                self._session_origin = (
                    float(msg.position.x),
                    float(msg.position.y),
                )
            elif (
                not self._origin_exited
                and math.hypot(
                    float(msg.position.x) - self._session_origin[0],
                    float(msg.position.y) - self._session_origin[1],
                )
                >= self._origin_guard_arm_distance_m
            ):
                self._origin_exited = True

    def handle_occupancy_grid(self, msg: OccupancyGrid) -> None:
        with self._lock:
            previous = self._occupancy_grid
            BasePatrolRouter.handle_occupancy_grid(self, msg)
            if self._occupancy_grid is previous:
                return

            from dimos.mapping.occupancy.gradient import gradient, voronoi_gradient

            self._costmap = gradient(msg, max_distance=1.5)
            free = msg.grid == 0
            clearance_cells = max(
                2,
                int(math.ceil(self._clearance_radius_m / msg.resolution)),
            )
            # Euclidean clearance is faithful to the circular Go2 footprint.
            # The stock square structuring element rejected diagonal/narrow
            # passages even when the planner's circular footprint fit.
            self._safe_mask = distance_transform_edt(free) >= clearance_cells
            if not np.any(self._safe_mask):
                # Never invent unsafe space. This fallback merely retains free
                # cells with 10 cm clearance so the downstream A* safety
                # planner can make the final, stricter decision.
                fallback_cells = max(1, int(math.ceil(0.10 / msg.resolution)))
                self._safe_mask = distance_transform_edt(free) >= fallback_cells

            voronoi = voronoi_gradient(msg, max_distance=1.5)
            self._sampling_weights = np.clip(
                100.0 - voronoi.grid.astype(np.float64), 0.0, 100.0
            )

    def next_goal(self) -> Any:
        """Prefer the FARTHEST reachable candidate so the wander pushes outward.

        The stock router picks the sampled candidate whose path covers the most
        new cells. Once the start area is mapped that still re-orbits it, because
        a short nearby path can out-score a long one. Nightwatch instead takes
        the farthest reachable candidate (new coverage only breaks ties), so the
        dog ranges across the whole floor to find people rather than circling
        home (user requirement, July 24 evening). The map-wide, corridor-weighted
        candidate sampling and the A* reachability check are unchanged.
        """
        with self._lock:
            if (
                self._occupancy_grid is None
                or self._visited is None
                or self._safe_mask is None
                or self._costmap is None
                or self._sampling_weights is None
            ):
                return None
            occupancy_grid = self._occupancy_grid
            costmap = self._costmap
            safe_mask = self._safe_mask
            sampling_weights = self._sampling_weights
            visited = self._visited.copy()
            pose = self._pose
            session_origin = getattr(self, "_session_origin", None)
            origin_guard_armed = bool(getattr(self, "_origin_exited", False))
            recent_goals = list(getattr(self, "_recent_goals", []))
            keep_outs = list(getattr(self, "_persistent_keep_outs", []))
            failure_zones = list(getattr(self, "_session_failure_zones", []))
            exclusions = keep_outs + failure_zones

        if pose is None:
            return None
        start = (pose.position.x, pose.position.y)

        unvisited_safe = safe_mask & ~visited
        if origin_guard_armed and session_origin is not None:
            origin_grid = occupancy_grid.world_to_grid(session_origin)
            yy, xx = np.ogrid[: occupancy_grid.height, : occupancy_grid.width]
            radius_cells = (
                self._origin_goal_exclusion_radius_m / occupancy_grid.resolution
            )
            outside_origin = (xx - float(origin_grid.x)) ** 2 + (
                yy - float(origin_grid.y)
            ) ** 2 >= radius_cells**2
            unvisited_safe &= outside_origin
        if not np.any(unvisited_safe):
            # Everything novel is covered. Keep roaming, but never reintroduce
            # the boot/window region merely because the novelty mask saturated.
            unvisited_safe = safe_mask.copy()
            if origin_guard_armed and session_origin is not None:
                unvisited_safe &= outside_origin
        if not np.any(unvisited_safe):
            return None

        safe_indices = np.argwhere(unvisited_safe)
        n_candidates = min(self._candidates_to_consider, len(safe_indices))
        weights = sampling_weights[safe_indices[:, 0], safe_indices[:, 1]]
        weight_sum = weights.sum()
        probs = weights / weight_sum if weight_sum > 0 else None
        chosen = safe_indices[
            np.random.choice(
                len(safe_indices), size=n_candidates, replace=False, p=probs
            )
        ]

        reachable: list[tuple[int, tuple[float, float]]] = []
        for row, col in chosen:
            world = occupancy_grid.grid_to_world((int(col), int(row), 0))
            candidate = (world.x, world.y)
            if exclusions and _point_in_keep_out(
                candidate[0], candidate[1], exclusions
            ):
                continue
            if any(
                math.hypot(candidate[0] - gx, candidate[1] - gy)
                < self._recent_goal_exclusion_radius_m
                for gx, gy in recent_goals
            ):
                continue
            path = min_cost_astar(
                costmap, candidate, start, unknown_penalty=1.0, use_cpp=True
            )
            if path is None:
                continue
            if exclusions and self._path_reenters_exclusion(path, exclusions):
                continue
            new_cells = self._count_new_coverage(
                path, visited, occupancy_grid, safe_mask
            )
            reachable.append((new_cells, candidate))

        best_point = self._select_spread_goal(reachable, start)
        if best_point is None:
            return None
        with self._lock:
            goals = getattr(self, "_recent_goals", None)
            if goals is None:
                goals = []
                self._recent_goals = goals
            goals.append(best_point)
            del goals[: -self._recent_goal_limit]
        return point_to_pose_stamped(best_point)

    def _select_spread_goal(
        self,
        reachable: list[tuple[int, tuple[float, float]]],
        start: tuple[float, float],
    ) -> tuple[float, float] | None:
        """Spread goals across rooms; preserve outward motion on the first leg."""
        best_point: tuple[float, float] | None = None
        best_key = (-1.0, -1, -1.0)
        origin = getattr(self, "_session_origin", None) or start
        recent = list(getattr(self, "_recent_goals", []))
        for new_cells, (x, y) in reachable:
            outward_distance = math.hypot(x - origin[0], y - origin[1])
            recent_novelty = (
                min(math.hypot(x - gx, y - gy) for gx, gy in recent)
                if recent
                else outward_distance
            )
            # Farthest-point sampling over recent goals avoids choosing several
            # points in whichever single room happens to be farthest from boot.
            # New path coverage breaks ties, then travel distance.
            travel_distance = math.hypot(x - start[0], y - start[1])
            key = (recent_novelty, new_cells, travel_distance)
            if key > best_key:
                best_key = key
                best_point = (x, y)
        return best_point


class PatrollingModule(_StockPatrollingModule):
    """Coverage patrol that does not forget the floor after every preemption."""

    # After reaching each patrol goal the dog pauses for a few seconds before
    # ambling to the next one. Navigation is IDLE during the dwell, which is
    # exactly when the curiosity supervisor schedules dog expressions, so the
    # mapped phase reads as a relaxed stroll instead of inch-by-inch coverage.
    _wander_dwell_range_s: tuple[float, float] = (0.5, 1.5)
    # A full-floor leg can legitimately take about a minute at the capped Go2
    # cruise speed. Twelve seconds cancelled healthy paths mid-stride and
    # immediately selected another far corner, producing visible circles.
    # Stuck recovery remains bounded by the supervisor's displacement windows.
    _patrol_goal_timeout_s = 75.0
    _keep_out_path = _KEEP_OUT_PATH
    _reloc: RelocSpec | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        clearance_radius_m = (
            self._global_config.robot_width * self._clearance_multiplier
        )
        self._router = NightwatchCoveragePatrolRouter(clearance_radius_m)
        self._no_goal_since: float | None = None
        self._goals_published = 0
        self._world_to_map_cache: tuple[float, float, float] | None = None
        self._world_to_map_cache_until = 0.0

    def _refresh_keep_outs(self) -> None:
        """Feed the explorer's persistent glass/trap exclusions into patrol."""
        circles = _load_keep_out_circles(self._keep_out_path)
        transform = _cached_world_to_map(self)
        world_circles = (
            _keep_out_circles_to_world(circles, transform)
            if circles and transform is not None
            else _prelock_world_keep_outs(
                circles,
                (
                    (
                        float(self._latest_pose.position.x),
                        float(self._latest_pose.position.y),
                    )
                    if getattr(self, "_latest_pose", None) is not None
                    else None
                ),
                (
                    str(epoch["epoch_id"])
                    if (epoch := _load_odom_epoch()) is not None
                    else None
                ),
            )
        )
        setter = getattr(self._router, "set_keep_out_circles", None)
        if setter is not None:
            setter(world_circles)

    @rpc
    def report_patrol_failure(self, x: float, y: float, reason: str = "stuck") -> bool:
        """Blacklist a patrol failure immediately instead of revisiting it."""
        self._router.report_failure_zone(float(x), float(y))
        logger.warning(
            "patrol feedback: session exclusion added",
            x=round(float(x), 2),
            y=round(float(y), 2),
            reason=reason,
        )
        return True

    @skill(uses=[CAP_MOVEMENT], lifecycle="background")
    async def start_patrol(self) -> str:
        """Start least-visited coverage patrol without erasing visit history."""
        self.start_tool("start_patrol")
        started = await self._ensure_patrol_task()
        if not started:
            return "Patrol is already running. Use `stop_patrol` to stop."
        return "Patrol started. Use `stop_patrol` to stop."

    @rpc
    async def ensure_patrolling(self) -> bool:
        """Start patrol directly for the supervisor, without MCP discovery."""
        return await self._ensure_patrol_task()

    @rpc
    async def stop_patrolling(self) -> bool:
        """Stop patrol directly for the supervisor, without MCP discovery."""
        was_active = self.is_patrolling()
        await self._stop_patrolling()
        return was_active

    async def _ensure_patrol_task(self) -> bool:
        if self._patrol_task is not None and not self._patrol_task.done():
            return False

        # Deliberately do not call router.reset(): short follows, expressions,
        # and operator commands must not make the dog forget where it patrolled.
        self._planner_spec.set_replanning_enabled(False)
        from dimos.navigation.patrolling.constants import EXTRA_CLEARANCE

        self._planner_spec.set_safe_goal_clearance(
            self._global_config.robot_rotation_diameter / 2 + EXTRA_CLEARANCE
        )
        self._no_goal_since = None
        self._patrol_task = asyncio.create_task(self._patrol_loop())
        return True

    async def _stop_patrolling(self) -> None:
        """Cancel patrol without publishing the current pose as another goal."""
        if self._patrol_task is not None and not self._patrol_task.done():
            self._patrol_task.cancel()
            try:
                await self._patrol_task
            except asyncio.CancelledError:
                pass
        self._patrol_task = None
        self._planner_spec.set_replanning_enabled(True)
        self._planner_spec.reset_safe_goal_clearance()
        self.stop_tool("start_patrol")
        # Upstream publishes `_latest_pose` here. The global planner treats it
        # as a new destination, performs final rotation, and leaves the robot
        # staring in the exact direction where patrol was cancelled.
        self._planner_spec.cancel_goal()

    async def _patrol_loop(self) -> None:
        while True:
            self._refresh_keep_outs()
            goal = self._router.next_goal()
            if goal is None:
                if self._no_goal_since is None:
                    self._no_goal_since = time.monotonic()
                logger.info("No patrol goal available, retrying in 2s")
                await asyncio.sleep(2.0)
                continue

            self._no_goal_since = None
            self._goals_published += 1
            self._goal_reached_event.clear()
            self.goal_request.publish(goal)
            try:
                await asyncio.wait_for(
                    self._goal_reached_event.wait(),
                    timeout=self._patrol_goal_timeout_s,
                )
            except TimeoutError:
                logger.warning(
                    "patrol goal timed out; selecting another novel goal",
                    timeout_s=self._patrol_goal_timeout_s,
                )
            dwell_min, dwell_max = self._wander_dwell_range_s
            await asyncio.sleep(random.uniform(dwell_min, dwell_max))

    @rpc
    def patrol_status(self) -> dict[str, Any]:
        return {
            "active": self.is_patrolling(),
            "saturation": float(self._router.get_saturation()),
            "goals_published": self._goals_published,
            "no_goal_for_s": (
                max(0.0, time.monotonic() - self._no_goal_since)
                if self._no_goal_since is not None
                else 0.0
            ),
        }
