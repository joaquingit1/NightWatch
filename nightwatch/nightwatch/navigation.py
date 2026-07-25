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
import contextlib
import fcntl
import heapq
import json
import math
import os
import random
import threading
import time
from typing import Any, Protocol
import uuid

import numpy as np
from reactivex.disposable import Disposable
from scipy.ndimage import binary_dilation, distance_transform_edt, label

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.core.core import rpc
from dimos.core.stream import In
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import CostValues, OccupancyGrid
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
_KEEP_OUT_PATH = os.getenv(
    "NIGHTWATCH_KEEP_OUT_PATH", "assets/output/maps/nightwatch_keepout.json"
)
_OPERATOR_ZONE_PATH = os.getenv(
    "NIGHTWATCH_ZONE_STATE_PATH", "assets/output/maps/nightwatch_zones.json"
)
_ODOM_EPOCH_PATH = os.getenv(
    "NIGHTWATCH_ODOM_EPOCH_PATH", "assets/output/maps/nightwatch_odom_epoch.json"
)
_ODOM_EPOCH_CONTINUITY_M = 4.0
# A robot power cycle resets odometry to the boot pose (position ~0, yaw ~0).
# Positional proximity alone cannot distinguish "stack restarted while the dog
# stood still" from "dog died near its original boot spot and was rebooted",
# so heading continuity is required as well whenever both sides recorded it.
# The tolerance is generous because the previous session can die up to
# _ODOM_EPOCH_SAVE_S after its last save while the dog was turning.
_ODOM_EPOCH_YAW_TOLERANCE_RAD = 0.7
_ODOM_EPOCH_MAX_AGE_S = 12.0 * 60.0 * 60.0
_ODOM_EPOCH_SAVE_S = 2.0
# The world->map transform is stable once ICP locks, so poll the reloc worker at
# most this often instead of once per frontier. Mirrors world_model.py.
_WORLD_TO_MAP_CACHE_S = 5.0

# The local planner declares arrival within its goal tolerance (0.2 m) without
# commanding any motion. An egress endpoint inside that band therefore "arrives"
# in place while the robot is still in violation, and patrol re-picks the same
# goal forever (observed live 2026-07-25: shrinking outward hops stalled the dog
# 0.13 m before the circle boundary). Every egress goal must clear the violated
# boundary by more than the arrival tolerance and command a real step.
_EGRESS_GOAL_CLEARANCE_M = 0.45
_MIN_EGRESS_STEP_M = 0.6
# Session failure exclusions heal after this long. Permanent exclusions from
# transient transport stalls poisoned the patrol goal pool for the whole run.
_FAILURE_EXCLUSION_TTL_S = 180.0


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


def _load_operator_zones(
    path: str | None,
    transform: tuple[float, float, float] | None,
    current_epoch: str | None = None,
) -> list[dict[str, Any]]:
    """Load operator polygons projected into the current WORLD frame.

    A zone whose only usable geometry is a stored WORLD snapshot is enforced
    only when that snapshot provably belongs to this session's odometry frame
    (its ``world_epoch`` stamp matches ``current_epoch``). A snapshot from a
    previous session's WORLD frame would place the polygon at the wrong
    physical spot after a restart, so it is refused instead of guessed
    (AGENTS invariant 10). Unstamped legacy entries keep their historical
    behavior. MAP-frame polygons projected through a live transform never
    depend on the snapshot and are unaffected.
    """
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    zones: list[dict[str, Any]] = []
    for item in payload.get("zones", []) if isinstance(payload, dict) else []:
        try:
            if not bool(item.get("active", True)):
                continue
            kind = str(item["kind"])
            if kind not in {"keep_in", "keep_out"}:
                continue
            map_points = [
                (float(point[0]), float(point[1])) for point in item["points"]
            ]
            fallback_world_points = [
                (float(point[0]), float(point[1]))
                for point in item.get("world_points", [])
            ]
            points_frame = str(item.get("points_frame", "")).lower()
            legacy_world_drawing = (
                not points_frame
                and len(map_points) == len(fallback_world_points)
                and all(
                    math.isclose(map_x, world_x, abs_tol=1e-9)
                    and math.isclose(map_y, world_y, abs_tol=1e-9)
                    for (map_x, map_y), (world_x, world_y) in zip(
                        map_points, fallback_world_points, strict=True
                    )
                )
            )
            uses_world_snapshot = (
                points_frame == "world"
                or legacy_world_drawing
                or transform is None
            )
            world_stamp = item.get("world_epoch")
            world_stamp = str(world_stamp) if world_stamp else None
            if (
                uses_world_snapshot
                and world_stamp is not None
                and world_stamp != current_epoch
            ):
                # The snapshot was taken in another session's WORLD frame;
                # skip the zone rather than enforce it at the wrong spot.
                continue
            world_points = (
                fallback_world_points
                if points_frame == "world" or legacy_world_drawing
                else (
                    [_map_xy_to_world(x, y, transform) for x, y in map_points]
                    if transform is not None
                    else fallback_world_points
                )
            )
            if len(world_points) >= 3:
                zones.append(
                    {
                        "id": str(item.get("id", "")),
                        "kind": kind,
                        "points": world_points,
                    }
                )
        except (KeyError, TypeError, ValueError):
            continue
    return zones


def _map_xy_to_world(
    map_x: float,
    map_y: float,
    transform: tuple[float, float, float],
) -> tuple[float, float]:
    """Inverse of map_xy = R(yaw) @ world_xy + translation."""
    tx, ty, yaw = transform
    dx = map_x - tx
    dy = map_y - ty
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    return (
        cos_yaw * dx + sin_yaw * dy,
        -sin_yaw * dx + cos_yaw * dy,
    )


def _point_in_polygon(
    x: float,
    y: float,
    polygon: list[tuple[float, float]],
) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _point_obeys_operator_zones(
    x: float,
    y: float,
    zones: list[dict[str, Any]],
) -> bool:
    keep_in = [zone["points"] for zone in zones if zone["kind"] == "keep_in"]
    if keep_in and not any(_point_in_polygon(x, y, polygon) for polygon in keep_in):
        return False
    return not any(
        _point_in_polygon(x, y, zone["points"])
        for zone in zones
        if zone["kind"] == "keep_out"
    )


def _points_obey_operator_zones(
    xs: np.ndarray,
    ys: np.ndarray,
    zones: list[dict[str, Any]],
) -> np.ndarray:
    """Vectorized endpoint filter used before patrol candidate sampling.

    Sampling the whole venue and checking the small focus polygon afterwards
    frequently produced zero valid candidates even though thousands of safe
    cells existed inside the polygon. Filtering first makes the candidate
    budget describe the allowed area rather than whichever room is largest.
    """

    allowed = np.ones(xs.shape, dtype=bool)
    keep_in_seen = False
    keep_in_allowed = np.zeros(xs.shape, dtype=bool)
    for zone in zones:
        polygon = zone["points"]
        inside = np.zeros(xs.shape, dtype=bool)
        previous = polygon[-1]
        for current in polygon:
            x1, y1 = previous
            x2, y2 = current
            crossing = (y1 > ys) != (y2 > ys)
            denominator = y2 - y1
            if abs(denominator) > 1e-12:
                crossing_x = (x2 - x1) * (ys - y1) / denominator + x1
                inside ^= crossing & (xs < crossing_x)
            previous = current
        if zone["kind"] == "keep_in":
            keep_in_seen = True
            keep_in_allowed |= inside
        else:
            allowed &= ~inside
    if keep_in_seen:
        allowed &= keep_in_allowed
    return allowed


def _operator_zone_routing_costmap(
    costmap: OccupancyGrid,
    zones: list[dict[str, Any]],
    start: tuple[float, float],
) -> OccupancyGrid:
    """Return an A* costmap whose forbidden operator-zone cells are occupied.

    Endpoint and post-plan checks are not sufficient for a concave focus area:
    unconstrained A* repeatedly chooses the shorter path across its boundary,
    so a legal endpoint is rejected even when a contained detour exists. Apply
    the same limits while searching so A* can discover that detour.

    If the robot starts outside the allowed area, retain the unmasked costmap;
    the existing route validator deliberately permits an immediate escape from
    a newly drawn zone. Once it is inside, subsequent calls use the hard mask.
    """
    if not zones or not _point_obeys_operator_zones(start[0], start[1], zones):
        return costmap

    rows, cols = np.indices(costmap.grid.shape)
    xs = float(costmap.origin.position.x) + cols.astype(np.float64) * float(
        costmap.resolution
    )
    ys = float(costmap.origin.position.y) + rows.astype(np.float64) * float(
        costmap.resolution
    )
    allowed = _points_obey_operator_zones(xs, ys, zones)
    if np.all(allowed):
        return costmap

    constrained = OccupancyGrid(
        grid=costmap.grid.copy(),
        resolution=float(costmap.resolution),
        origin=costmap.origin,
        frame_id=costmap.frame_id,
        ts=costmap.ts,
    )
    constrained.grid[~allowed] = CostValues.OCCUPIED
    return constrained


def _segment_obeys_operator_zones(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    zones: list[dict[str, Any]],
    *,
    step_m: float = 0.1,
) -> bool:
    """Sample a route so it cannot cut across a polygon between endpoints."""
    distance = math.hypot(end_x - start_x, end_y - start_y)
    steps = max(1, math.ceil(distance / step_m))
    has_reached_allowed_space = _point_obeys_operator_zones(start_x, start_y, zones)
    for index in range(1, steps + 1):
        fraction = index / steps
        x = start_x + (end_x - start_x) * fraction
        y = start_y + (end_y - start_y) * fraction
        allowed = _point_obeys_operator_zones(x, y, zones)
        if allowed:
            has_reached_allowed_space = True
        elif has_reached_allowed_space:
            # Permit escape when a newly-drawn exclusion contains the robot,
            # then forbid entry or re-entry once it reaches allowed space.
            return False
    return True


def _path_obeys_operator_zones(
    path: Any,
    zones: list[dict[str, Any]],
    *,
    step_m: float = 0.05,
) -> bool:
    """Validate a routed path while permitting only bounded zone egress/ingress.

    A robot outside a newly selected focus area must be allowed to travel
    inward, and a robot already inside a newly drawn keep-out must be allowed
    to leave. Once the path reaches allowed space it may never leave or
    re-enter an exclusion. The endpoint must be allowed. This mirrors the
    downstream global planner's policy so patrol cannot reject the exact
    recovery route that the motion planner is designed to execute.
    """
    if not zones or not getattr(path, "poses", None):
        return not zones

    samples: list[tuple[float, float]] = []
    previous_x = float(path.poses[0].position.x)
    previous_y = float(path.poses[0].position.y)
    samples.append((previous_x, previous_y))
    for pose in path.poses[1:]:
        x = float(pose.position.x)
        y = float(pose.position.y)
        distance = math.hypot(x - previous_x, y - previous_y)
        steps = max(1, math.ceil(distance / step_m))
        samples.extend(
            (
                previous_x + (x - previous_x) * index / steps,
                previous_y + (y - previous_y) * index / steps,
            )
            for index in range(1, steps + 1)
        )
        previous_x, previous_y = x, y

    keep_in = [zone["points"] for zone in zones if zone["kind"] == "keep_in"]
    keep_out = [zone["points"] for zone in zones if zone["kind"] == "keep_out"]
    start_x, start_y = samples[0]
    reached_keep_in = not keep_in or any(
        _point_in_polygon(start_x, start_y, polygon) for polygon in keep_in
    )
    exited_keep_out = [
        not _point_in_polygon(start_x, start_y, polygon) for polygon in keep_out
    ]

    for x, y in samples[1:]:
        if keep_in:
            inside_keep_in = any(
                _point_in_polygon(x, y, polygon) for polygon in keep_in
            )
            if inside_keep_in:
                reached_keep_in = True
            elif reached_keep_in:
                return False
        for index, polygon in enumerate(keep_out):
            inside_keep_out = _point_in_polygon(x, y, polygon)
            if not inside_keep_out:
                exited_keep_out[index] = True
            elif exited_keep_out[index]:
                return False

    return reached_keep_in and all(exited_keep_out)


def _shortest_zone_egress_goal(
    costmap: OccupancyGrid,
    start: tuple[float, float],
    goal_mask: np.ndarray,
    operator_zones: list[dict[str, Any]],
    exclusions: list[tuple[float, float, float]],
    *,
    unknown_penalty: float,
    require_full_egress: bool = True,
) -> tuple[tuple[float, float] | None, int]:
    """Find the shortest legal endpoint in one bounded grid search.

    Recovery previously ran one complete A* for every candidate endpoint. A
    normal 400x400 costmap can contain more than 1,000 candidates, leaving the
    patrol worker CPU-bound for minutes while the robot stands still. This is
    the equivalent multi-goal search: cumulative map cost is minimized first,
    then route length, exactly like ``min_cost_astar``.

    The search state also records irreversible zone transitions. A robot may
    enter a focus area or leave a keep-out that already contains it, but may
    never reverse either transition. Keep-outs that do not contain the start
    are ordinary hard obstacles. Therefore the selected endpoint is reachable
    under the same rules used by the downstream global planner; no N-times-A*
    validation cascade is needed.
    """
    if goal_mask.shape != costmap.grid.shape or not np.any(goal_mask):
        return None, 0

    start_grid = costmap.world_to_grid(start)
    start_col, start_row = int(start_grid.x), int(start_grid.y)
    if not (0 <= start_row < costmap.height and 0 <= start_col < costmap.width):
        return None, 0

    rows, cols = np.indices(costmap.grid.shape)
    xs = float(costmap.origin.position.x) + cols.astype(np.float64) * float(
        costmap.resolution
    )
    ys = float(costmap.origin.position.y) + rows.astype(np.float64) * float(
        costmap.resolution
    )

    keep_in_mask: np.ndarray | None = None
    region_masks: list[np.ndarray] = []
    for zone in operator_zones:
        inside = _points_obey_operator_zones(
            xs,
            ys,
            [{"kind": "keep_in", "points": zone["points"]}],
        )
        if zone["kind"] == "keep_in":
            if keep_in_mask is None:
                keep_in_mask = inside
            else:
                keep_in_mask |= inside
        else:
            # For a single synthetic keep-in, ``inside`` is the polygon mask.
            region_masks.append(inside)
    for cx, cy, radius in exclusions:
        region_masks.append((xs - cx) ** 2 + (ys - cy) ** 2 <= radius**2)

    # Regions that do not contain the robot never need dynamic state: entering
    # any of them is forbidden. Only zones being escaped need an "exited" bit.
    static_forbidden = np.zeros(costmap.grid.shape, dtype=bool)
    containing_masks: list[np.ndarray] = []
    for region_mask in region_masks:
        if bool(region_mask[start_row, start_col]):
            containing_masks.append(region_mask)
        else:
            static_forbidden |= region_mask
    if len(containing_masks) > 62:
        # This would require more state bits than the compact representation.
        # Refuse an unsafe route rather than silently weakening a keep-out.
        logger.error(
            "too many overlapping keep-outs for bounded egress",
            containing_zones=len(containing_masks),
        )
        return None, 0

    inside_bits = np.zeros(costmap.grid.shape, dtype=np.uint64)
    for index, region_mask in enumerate(containing_masks):
        inside_bits[region_mask] |= np.uint64(1 << index)
    all_exited = (1 << len(containing_masks)) - 1
    focus_bit = 1 << len(containing_masks)
    focus_required = keep_in_mask is not None
    start_state = 0
    if not focus_required or bool(keep_in_mask[start_row, start_col]):
        start_state |= focus_bit

    # State is sparse because egress normally ends near the start. Each score
    # is lexicographic (risk cost, grid distance), matching stock A*.
    start_key = (start_state, start_row, start_col)
    scores: dict[tuple[int, int, int], tuple[float, float]] = {start_key: (0.0, 0.0)}
    queue: list[tuple[float, float, int, int, int]] = [
        (0.0, 0.0, start_state, start_row, start_col)
    ]
    movements = (
        (0, 1, 1.0),
        (1, 0, 1.0),
        (0, -1, 1.0),
        (-1, 0, 1.0),
        (1, 1, 1.42),
        (1, -1, 1.42),
        (-1, 1, 1.42),
        (-1, -1, 1.42),
    )
    expansions = 0

    while queue:
        cost, distance, state, row, col = heapq.heappop(queue)
        key = (state, row, col)
        if scores.get(key) != (cost, distance):
            continue
        expansions += 1

        reached_focus = bool(state & focus_bit)
        exited_bits = state & all_exited
        egress_complete = exited_bits == all_exited
        if (
            goal_mask[row, col]
            and reached_focus
            and (egress_complete or not require_full_egress)
        ):
            world = costmap.grid_to_world((col, row))
            return (float(world.x), float(world.y)), expansions

        for delta_row, delta_col, step_distance in movements:
            next_row = row + delta_row
            next_col = col + delta_col
            if not (0 <= next_row < costmap.height and 0 <= next_col < costmap.width):
                continue
            if static_forbidden[next_row, next_col]:
                continue

            value = int(costmap.grid[next_row, next_col])
            if value >= int(CostValues.OCCUPIED):
                continue
            if value == int(CostValues.UNKNOWN):
                cell_cost = float(CostValues.OCCUPIED) * unknown_penalty
                if cell_cost >= float(CostValues.OCCUPIED):
                    continue
            elif value == int(CostValues.FREE):
                cell_cost = 0.0
            else:
                cell_cost = float(value)

            next_state = state
            if focus_required:
                inside_focus = bool(keep_in_mask[next_row, next_col])
                if reached_focus and not inside_focus:
                    continue
                if inside_focus:
                    next_state |= focus_bit

            neighbor_inside = int(inside_bits[next_row, next_col])
            if exited_bits & neighbor_inside:
                continue
            next_state |= all_exited ^ neighbor_inside

            next_cost = cost + cell_cost
            next_distance = distance + step_distance
            next_key = (next_state, next_row, next_col)
            next_score = (next_cost, next_distance)
            if next_score >= scores.get(next_key, (math.inf, math.inf)):
                continue
            scores[next_key] = next_score
            heapq.heappush(
                queue,
                (
                    next_cost,
                    next_distance,
                    next_state,
                    next_row,
                    next_col,
                ),
            )

    return None, expansions


def _wrap_angle(angle: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    wrapped = math.fmod(angle + math.pi, 2.0 * math.pi)
    if wrapped <= 0.0:
        wrapped += 2.0 * math.pi
    return wrapped - math.pi


def _load_odom_epoch(path: str = _ODOM_EPOCH_PATH) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        epoch_id = str(data["epoch_id"])
        last_pose = data["last_pose"]
        raw_yaw = data.get("last_yaw")
        return {
            "epoch_id": epoch_id,
            "updated_at": float(data["updated_at"]),
            "last_pose": (float(last_pose[0]), float(last_pose[1])),
            "last_yaw": float(raw_yaw) if raw_yaw is not None else None,
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
    yaw: float | None = None,
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
            "last_yaw": float(yaw) if yaw is not None else None,
        }
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except Exception:
        logger.exception("odometry epoch save failed", path=path)


def _resolve_odom_epoch(
    pose: tuple[float, float],
    *,
    yaw: float | None = None,
    path: str = _ODOM_EPOCH_PATH,
    now: float | None = None,
) -> str:
    """Reuse the WORLD frame only when odometry is spatially continuous.

    Heading continuity is required in addition to position whenever both the
    saved epoch and the caller provide a yaw: a power-cycled Go2 restarts
    odometry at position ~(0, 0) with yaw ~0, so a dog that died near its
    boot spot would otherwise falsely "continue" the previous WORLD frame and
    every restored transform/zone would be offset by the boot-pose delta.
    """
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
    if (
        continuous
        and yaw is not None
        and saved is not None
        and saved.get("last_yaw") is not None
        and abs(_wrap_angle(yaw - float(saved["last_yaw"])))
        > _ODOM_EPOCH_YAW_TOLERANCE_RAD
    ):
        continuous = False
    epoch_id = str(saved["epoch_id"]) if continuous else uuid.uuid4().hex
    _save_odom_epoch(epoch_id, pose, yaw=yaw, path=path, now=current_time)
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


def _yaw_from_odometry(msg: Any) -> float | None:
    """Best-effort planar heading from an odometry message, or None."""
    try:
        orientation = getattr(msg, "orientation", None)
        if orientation is None:
            return None
        euler = orientation.to_euler()
        yaw = float(euler.z)
    except Exception:
        return None
    return yaw if math.isfinite(yaw) else None


@contextlib.contextmanager
def _locked_zone_file(path: str):  # type: ignore[no-untyped-def]
    """Cross-process advisory lock around zone-file read-modify-write.

    The booth server (server/app/services/zone_store.py) and this navigation
    worker both rewrite the shared zone JSON. Each side writes atomically, but
    two concurrent read-modify-write cycles still lose one side's update (a
    zone saved by the operator exactly while a patrol tick anchors polygons
    silently disappears). Both sides therefore flock the same sidecar file.
    """
    lock_path = f"{path}.lock"
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _anchor_world_operator_zones(
    path: str | None,
    transform: tuple[float, float, float],
    current_epoch: str | None = None,
) -> int:
    """Anchor WORLD polygons and refresh the planner's current-WORLD fallback.

    Frame bookkeeping rules:

    - A WORLD drawing is anchored to the MAP frame only when its
      ``world_epoch`` stamp proves it was drawn in this session's odometry
      frame (or when it is a legacy unstamped entry). A stale-stamped drawing
      is left untouched: anchoring it with this session's transform would
      translate the polygon by the inter-session odometry delta.
    - Anchoring preserves the original drawing (``drawn_world_points`` +
      ``anchor_transform``). If the session's world->map alignment is later
      corrected (live verification overturning a restored alignment, operator
      initial pose), the MAP polygon is re-derived from the original drawing
      instead of keeping coordinates minted under the wrong transform.
    - Every refreshed ``world_points`` snapshot is stamped with the epoch it
      belongs to, so the frame-blind A* planner can validate it.
    """
    if not path:
        return 0
    try:
        with _locked_zone_file(path):
            return _anchor_world_operator_zones_locked(
                path, transform, current_epoch
            )
    except OSError:
        logger.exception("operator zone lock failed", path=path)
        return 0


def _anchor_world_operator_zones_locked(
    path: str,
    transform: tuple[float, float, float],
    current_epoch: str | None,
) -> int:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict) or not isinstance(payload.get("zones"), list):
        return 0

    anchored = 0
    changed = False
    for item in payload["zones"]:
        if not isinstance(item, dict):
            continue
        points = item.get("points", [])
        world_points = item.get("world_points", points)
        frame = str(item.get("points_frame", "")).lower()
        legacy_world = (
            not frame
            and isinstance(points, list)
            and isinstance(world_points, list)
            and points == world_points
        )
        world_stamp = item.get("world_epoch")
        world_stamp = str(world_stamp) if world_stamp else None
        if frame != "world" and not legacy_world:
            clean_world = None
        else:
            if world_stamp is not None and world_stamp != current_epoch:
                # Drawn in another session's WORLD frame: leave it exactly as
                # stored (flagged by its stale stamp) for the operator to
                # redraw; never reinterpret it under this session's transform.
                continue
            try:
                clean_world = [
                    (float(point[0]), float(point[1])) for point in world_points
                ]
            except (TypeError, ValueError, IndexError):
                continue
            if len(clean_world) < 3:
                continue
            item["points"] = [
                list(_world_to_map_point(x, y, transform)) for x, y in clean_world
            ]
            item["points_frame"] = "map"
            item["anchored_at"] = time.time()
            item["drawn_world_points"] = [[x, y] for x, y in clean_world]
            item["drawn_world_epoch"] = current_epoch
            item["anchor_transform"] = [
                float(transform[0]),
                float(transform[1]),
                float(transform[2]),
            ]
            anchored += 1
            changed = True

        if clean_world is None and _reanchor_map_zone_from_drawing(
            item, transform, current_epoch
        ):
            anchored += 1
            changed = True

        try:
            clean_map = [(float(point[0]), float(point[1])) for point in item["points"]]
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        projected_world = [
            list(_map_xy_to_world(x, y, transform)) for x, y in clean_map
        ]
        current_world = item.get("world_points")
        try:
            same_world = (
                isinstance(current_world, list)
                and len(current_world) == len(projected_world)
                and all(
                    isinstance(current, list)
                    and len(current) >= 2
                    and math.isclose(float(current[0]), projected[0], abs_tol=1e-9)
                    and math.isclose(float(current[1]), projected[1], abs_tol=1e-9)
                    for current, projected in zip(
                        current_world, projected_world, strict=True
                    )
                )
            )
        except (TypeError, ValueError):
            same_world = False
        if not same_world:
            item["world_points"] = projected_world
            changed = True
        if item.get("world_epoch") != current_epoch:
            # The snapshot now corresponds to this session's WORLD frame.
            item["world_epoch"] = current_epoch
            changed = True

    if not changed:
        return 0
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        logger.exception("operator zone anchoring failed", path=path)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        return 0
    logger.info(
        "operator zones synchronized to map alignment",
        anchored=anchored,
        path=path,
    )
    return anchored


def _reanchor_map_zone_from_drawing(
    item: dict[str, Any],
    transform: tuple[float, float, float],
    current_epoch: str | None,
) -> bool:
    """Re-derive an anchored MAP polygon after an in-session transform fix.

    Only zones whose original drawing provably belongs to this session's
    odometry frame are re-anchored; anything else keeps its MAP coordinates.
    """
    drawn = item.get("drawn_world_points")
    drawn_epoch = item.get("drawn_world_epoch")
    previous_raw = item.get("anchor_transform")
    if (
        not isinstance(drawn, list)
        or not drawn_epoch
        or current_epoch is None
        or str(drawn_epoch) != current_epoch
        or not isinstance(previous_raw, list)
        or len(previous_raw) != 3
    ):
        return False
    try:
        previous = tuple(float(value) for value in previous_raw)
        drawn_clean = [(float(point[0]), float(point[1])) for point in drawn]
    except (TypeError, ValueError, IndexError):
        return False
    if len(drawn_clean) < 3 or all(
        math.isclose(previous[index], transform[index], abs_tol=1e-9)
        for index in range(3)
    ):
        return False
    item["points"] = [
        list(_world_to_map_point(x, y, transform)) for x, y in drawn_clean
    ]
    item["anchor_transform"] = [
        float(transform[0]),
        float(transform[1]),
        float(transform[2]),
    ]
    item["anchored_at"] = time.time()
    logger.warning(
        "operator zone re-anchored after alignment correction",
        zone=str(item.get("id", ""))[:8],
    )
    return True


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
    # The accumulated map can briefly expose no safe frontier. Recheck once
    # after the next lidar update, then yield to coverage patrol instead of
    # standing still for the stock 20-second retry window.
    _max_consecutive_frontier_failures = 2
    _frontier_retry_s = 0.5
    # Persistent keep-out file (MAP frame). Stored as a class attribute because
    # the stock WavefrontConfig is dimos read-only and cannot take new fields.
    _keep_out_path = _KEEP_OUT_PATH
    # Set on fully initialized production instances. Keeping the class default
    # empty prevents object.__new__ test/simulation shells from silently reading
    # whichever user's venue file happens to exist under the process cwd.
    _operator_zone_path: str | None = None

    # Optional: keep-out zones are dormant until relocalization locks, so a stack
    # without reloc still explores. Auto-wires by type to NightwatchRelocalization.
    _reloc: RelocSpec | None
    navigation_obstacle: In[PoseStamped]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._operator_zone_path = _OPERATOR_ZONE_PATH
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
        if self.navigation_obstacle.transport is not None:
            self.register_disposable(
                Disposable(
                    self.navigation_obstacle.subscribe(
                        self._on_repeated_navigation_obstacle
                    )
                )
            )
        self._invalidation_stop.clear()
        self._invalidation_thread = threading.Thread(
            target=self._goal_invalidation_loop,
            name="frontier-goal-invalidation",
            daemon=True,
        )
        self._invalidation_thread.start()

    def _on_repeated_navigation_obstacle(self, pose: PoseStamped) -> None:
        """Blacklist the contact boundary, not the far-side frontier goal."""
        self.report_navigation_failure(
            float(pose.position.x),
            float(pose.position.y),
            "repeated obstacle replans at route boundary",
        )

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
        # A generation that has observed both stop signals can no longer
        # publish another goal. Give it a short chance to unwind. If an
        # unrelated transport/tool-stream teardown wedges its finalizer, detach
        # only that dead generation so autonomous exploration can restart.
        # `_exploration_loop` compares its captured stop_event with the current
        # one before mutating state, so the orphan cannot retire a successor.
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.0)
            if thread.is_alive():
                with self._exploration_lock:
                    if (
                        self.exploration_thread is thread
                        and not self.exploration_active
                    ):
                        self.exploration_thread = None
                logger.warning(
                    "Detached stopped exploration generation that did not unwind"
                )
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
            yaw = _yaw_from_odometry(msg)
            now = time.time()
            if self._odom_epoch_id is None:
                self._odom_epoch_id = _resolve_odom_epoch(pose, yaw=yaw)
                self._odom_epoch_last_saved_at = now
            elif now - self._odom_epoch_last_saved_at >= _ODOM_EPOCH_SAVE_S:
                _save_odom_epoch(self._odom_epoch_id, pose, yaw=yaw, now=now)
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
            # Repeated recovery callbacks used to append identical circles at
            # the same pose. Besides bloating state, overlapping copies can
            # make the bounded egress search conclude that no legal exit
            # exists. One physical hazard needs one persistent circle.
            duplicate = next(
                (
                    item
                    for item in self._keep_outs
                    if math.hypot(
                        float(item["x"]) - float(map_x),
                        float(item["y"]) - float(map_y),
                    )
                    <= 0.25
                    and abs(float(item["radius_m"]) - float(radius_m)) <= 0.25
                ),
                None,
            )
            if duplicate is not None:
                logger.info(
                    "duplicate keep-out ignored",
                    map_x=round(float(map_x), 2),
                    map_y=round(float(map_y), 2),
                    radius_m=round(float(radius_m), 2),
                )
                return
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
    def mark_keep_out_at(self, x: float, y: float, radius_m: float = 1.5) -> str:
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

    def detect_frontiers(
        self, robot_pose: Vector3, costmap: OccupancyGrid
    ) -> list[Vector3]:
        """Extract reachable frontier components with array operations.

        The stock implementation performs a Python object/BFS walk over every
        free *and unknown* cell in the full accumulated map. On the saved venue
        map that took 20-27 seconds after every obstacle recovery or gesture,
        leaving the dog stationary while its status still said ``explore``.

        A frontier is simply an UNKNOWN cell adjacent to reachable FREE space
        and not adjacent to an occupied cell. Connected-component labelling
        computes the same useful boundary in native SciPy code and also avoids
        proposing frontiers around disconnected free islands.
        """
        grid = np.asarray(costmap.grid)
        if grid.ndim != 2 or grid.size == 0:
            return []

        grid_pose = costmap.world_to_grid(robot_pose)
        start_x = int(grid_pose.x)
        start_y = int(grid_pose.y)
        height, width = grid.shape
        free_mask = grid == CostValues.FREE
        if not np.any(free_mask):
            return []

        # Normally odometry lies in a FREE cell. If inflation puts it just
        # outside one, select the nearest free cell in a small local window;
        # never run the stock unbounded Python BFS through the unknown map.
        if not (
            0 <= start_x < width
            and 0 <= start_y < height
            and free_mask[start_y, start_x]
        ):
            found: tuple[int, int] | None = None
            for radius in (4, 8, 16, 32, 64):
                x0, x1 = max(0, start_x - radius), min(width, start_x + radius + 1)
                y0, y1 = max(0, start_y - radius), min(height, start_y + radius + 1)
                ys, xs = np.nonzero(free_mask[y0:y1, x0:x1])
                if xs.size:
                    world_xs = xs + x0
                    world_ys = ys + y0
                    nearest = int(
                        np.argmin((world_xs - start_x) ** 2 + (world_ys - start_y) ** 2)
                    )
                    found = (int(world_xs[nearest]), int(world_ys[nearest]))
                    break
            if found is None:
                return []
            start_x, start_y = found

        connectivity = np.ones((3, 3), dtype=bool)
        free_labels, _free_count = label(free_mask, structure=connectivity)
        reachable_label = int(free_labels[start_y, start_x])
        if reachable_label == 0:
            return []
        reachable_free = free_labels == reachable_label

        unknown_mask = grid == CostValues.UNKNOWN
        occupied_mask = grid > self.config.occupancy_threshold
        frontier_mask = (
            unknown_mask
            & binary_dilation(reachable_free, structure=connectivity)
            & ~binary_dilation(occupied_mask, structure=connectivity)
        )
        frontier_labels, component_count = label(frontier_mask, structure=connectivity)
        if component_count == 0:
            return []

        ys, xs = np.nonzero(frontier_mask)
        point_labels = frontier_labels[ys, xs]
        component_sizes = np.bincount(point_labels, minlength=component_count + 1)
        min_cells = max(1, int(self.config.min_frontier_perimeter / costmap.resolution))
        component_ids = np.flatnonzero(component_sizes >= min_cells)
        component_ids = component_ids[component_ids != 0]
        if component_ids.size == 0:
            return []

        x_sums = np.bincount(point_labels, weights=xs, minlength=component_count + 1)
        y_sums = np.bincount(point_labels, weights=ys, minlength=component_count + 1)
        frontiers: list[Vector3] = []
        frontier_sizes: list[int] = []
        for component_id in component_ids:
            size = int(component_sizes[component_id])
            centroid = costmap.grid_to_world(
                Vector3(
                    float(x_sums[component_id] / size),
                    float(y_sums[component_id] / size),
                    0.0,
                )
            )
            frontiers.append(centroid)
            frontier_sizes.append(size)

        return self._rank_frontiers(frontiers, frontier_sizes, robot_pose, costmap)

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
        operator_zone = 0
        failure_route = 0
        origin_excluded = 0
        tight_band = 0
        visited = 0
        failed = 0
        resolved_fragments = 0
        # Permanent map-frame keep-out circles, projected once per call.
        # Dormant until relocalization locks (no transform, no circles).
        keep_out_circles = self._keep_out_world_circles()
        operator_zone_path = getattr(self, "_operator_zone_path", None)
        transform = _cached_world_to_map(self)
        session_epoch = getattr(self, "_odom_epoch_id", None)
        if transform is not None:
            _anchor_world_operator_zones(
                operator_zone_path, transform, session_epoch
            )
        operator_zones = _load_operator_zones(
            operator_zone_path, transform, session_epoch
        )
        failure_circles = [
            (x, y, self._failure_radius_m) for x, y, _ts, _reason in failures_snapshot
        ]
        session_origin = getattr(self, "_session_origin", None)
        origin_guard_armed = bool(getattr(self, "_origin_exited", False))
        for index, frontier in enumerate(frontier_centroids):
            if operator_zones and (
                not _point_obeys_operator_zones(frontier.x, frontier.y, operator_zones)
                or not _segment_obeys_operator_zones(
                    robot_pose.x,
                    robot_pose.y,
                    frontier.x,
                    frontier.y,
                    operator_zones,
                )
            ):
                operator_zone += 1
                continue
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
            operator_zone=operator_zone,
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
    # Weighted map-wide sampling favours corridor centrelines, but on a merged
    # premap it can miss the robot's small currently connected pocket forever.
    # Always test a small deterministic set of the nearest legal endpoints as
    # an escape hatch; A* and every zone/keep-out route check remain unchanged.
    _local_candidates_to_consider = 48
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
        self._operator_zones: list[dict[str, Any]] = []
        # (x, y, radius, created_monotonic); read via _active_failure_zones().
        self._session_failure_zones: list[tuple[float, float, float, float]] = []

    def set_keep_out_circles(self, circles: list[tuple[float, float, float]]) -> None:
        """Replace persistent WORLD-frame exclusions used by patrol."""
        with self._lock:
            self._persistent_keep_outs = list(circles)

    def set_operator_zones(self, zones: list[dict[str, Any]]) -> None:
        """Replace booth-drawn WORLD polygons used by coverage patrol."""
        with self._lock:
            self._operator_zones = list(zones)

    def report_failure_zone(self, x: float, y: float) -> None:
        """Exclude a navigation failure around this spot for a bounded time.

        These used to last "for the rest of this run", but a transient
        transport stall (stale lidar, degraded WebRTC) writes several of them
        around the robot in under a minute, and once the transport recovers
        the goal pool stays permanently poisoned: the dog can walk again yet
        finds no eligible patrol goal forever (observed live 2026-07-25,
        endless "No patrol goal available"). Exploration's equivalent failure
        memory already expires; patrol now matches it.
        """
        zone = (
            float(x),
            float(y),
            self._failure_exclusion_radius_m,
            time.monotonic(),
        )
        with self._lock:
            if any(
                math.hypot(zone[0] - cx, zone[1] - cy) < radius
                for cx, cy, radius in self._active_failure_zones()
            ):
                return
            self._session_failure_zones.append(zone)

    def _active_failure_zones(self) -> list[tuple[float, float, float]]:
        """Unexpired failure exclusions as (x, y, radius). Caller holds lock."""
        # getattr: ranking tests build routers with object.__new__.
        zones = getattr(self, "_session_failure_zones", None)
        if not zones:
            return []
        now = time.monotonic()
        kept = [zone for zone in zones if now - zone[3] < _FAILURE_EXCLUSION_TTL_S]
        if len(kept) != len(zones):
            self._session_failure_zones = kept
        return [(zone[0], zone[1], zone[2]) for zone in kept]

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
                math.ceil(self._clearance_radius_m / msg.resolution),
            )
            # Euclidean clearance is faithful to the circular Go2 footprint.
            # The stock square structuring element rejected diagonal/narrow
            # passages even when the planner's circular footprint fit.
            self._safe_mask = distance_transform_edt(free) >= clearance_cells
            if not np.any(self._safe_mask):
                # Never invent unsafe space. This fallback merely retains free
                # cells with 10 cm clearance so the downstream A* safety
                # planner can make the final, stricter decision.
                fallback_cells = max(1, math.ceil(0.10 / msg.resolution))
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
            failure_zones = self._active_failure_zones()
            exclusions = keep_outs + failure_zones
            operator_zones = list(getattr(self, "_operator_zones", []))

        if pose is None:
            return None
        start = (pose.position.x, pose.position.y)
        routing_costmap = _operator_zone_routing_costmap(
            costmap,
            operator_zones,
            (float(start[0]), float(start[1])),
        )
        containing_exclusions = [
            circle
            for circle in exclusions
            if math.hypot(start[0] - circle[0], start[1] - circle[1]) <= circle[2]
        ]
        egress_search_stats: dict[str, dict[str, int]] = {}

        def shortest_reachable_goal(
            indices: np.ndarray,
            search_label: str = "patrol",
            *,
            unknown_penalty: float = 1.0,
            require_full_egress: bool = True,
        ) -> tuple[float, float] | None:
            """Return the shortest legal endpoint with one multi-goal search."""
            if len(indices) == 0:
                egress_search_stats[search_label] = {
                    "candidates": 0,
                    "expansions": 0,
                }
                return None
            goal_mask = np.zeros(routing_costmap.grid.shape, dtype=bool)
            goal_mask[indices[:, 0], indices[:, 1]] = True
            started_at = time.monotonic()
            goal, expansions = _shortest_zone_egress_goal(
                routing_costmap,
                (float(start[0]), float(start[1])),
                goal_mask,
                operator_zones,
                exclusions,
                unknown_penalty=unknown_penalty,
                require_full_egress=require_full_egress,
            )
            egress_search_stats[search_label] = {
                "candidates": len(indices),
                "expansions": expansions,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            }
            return goal

        if containing_exclusions:
            # Keep-out egress is a separate navigation state, not a patrol
            # preference. First find the shortest A* route that exits every
            # containing exclusion while remaining inside the operator's
            # allowed area. This prevents normal farthest-goal scoring from
            # sending a booted/moved robot deeper through a hazard region.
            egress_indices = np.argwhere(safe_mask)
            egress_xs = float(occupancy_grid.origin.position.x) + egress_indices[
                :, 1
            ].astype(np.float64) * float(occupancy_grid.resolution)
            egress_ys = float(occupancy_grid.origin.position.y) + egress_indices[
                :, 0
            ].astype(np.float64) * float(occupancy_grid.resolution)
            outside_all = np.ones(len(egress_indices), dtype=bool)
            if operator_zones:
                outside_all &= _points_obey_operator_zones(
                    egress_xs,
                    egress_ys,
                    operator_zones,
                )
            for cx, cy, radius in exclusions:
                # Clear the boundary by more than the local planner's arrival
                # tolerance, or the dog can "arrive" at a boundary cell while
                # geometrically still inside the circle it is escaping.
                outside_all &= (egress_xs - cx) ** 2 + (egress_ys - cy) ** 2 > (
                    radius + _EGRESS_GOAL_CLEARANCE_M
                ) ** 2
            complete_exit = shortest_reachable_goal(
                egress_indices[outside_all],
                "complete",
            )
            egress_mode = "known_safe"
            if complete_exit is None:
                # A focus boundary can leave the robot's freshly observed
                # free-space island disconnected from the rest of the allowed
                # area (observed live with only 10 connected cells at boot).
                # The downstream planner already traverses unknown cells at
                # penalty 0.8 and its live local planner stops for real lidar
                # obstacles. Use the same policy only for bounded keep-out
                # egress, after proving that no all-known exit exists. The
                # operator-zone mask and route validator still constrain every
                # waypoint, and all endpoints remain known-safe cells.
                complete_exit = shortest_reachable_goal(
                    egress_indices[outside_all],
                    "complete_unknown",
                    unknown_penalty=0.8,
                )
                egress_mode = "unknown_safe"
            if complete_exit is None:
                # A newly observed obstacle can remove the 21 cm-clear
                # endpoint used on the preceding leg while still leaving
                # plenty of observed, A*-traversable floor out of the
                # exclusion. Do not turn that clearance mismatch into a
                # permanent standstill. The global planner remains the final
                # authority: it validates the full robot footprint and snaps
                # this observed-free endpoint to a contained safe point.
                narrow_exit_mask = (occupancy_grid.grid == CostValues.FREE) & (
                    routing_costmap.grid < CostValues.OCCUPIED
                )
                narrow_indices = np.argwhere(narrow_exit_mask)
                narrow_xs = float(occupancy_grid.origin.position.x) + narrow_indices[
                    :, 1
                ].astype(np.float64) * float(occupancy_grid.resolution)
                narrow_ys = float(occupancy_grid.origin.position.y) + narrow_indices[
                    :, 0
                ].astype(np.float64) * float(occupancy_grid.resolution)
                narrow_allowed = np.ones(len(narrow_indices), dtype=bool)
                if operator_zones:
                    narrow_allowed &= _points_obey_operator_zones(
                        narrow_xs,
                        narrow_ys,
                        operator_zones,
                    )
                for cx, cy, radius in exclusions:
                    narrow_allowed &= (narrow_xs - cx) ** 2 + (
                        narrow_ys - cy
                    ) ** 2 > radius**2
                complete_exit = shortest_reachable_goal(
                    narrow_indices[narrow_allowed],
                    "complete_narrow",
                    unknown_penalty=0.8,
                )
                egress_mode = "observed_narrow"
            if complete_exit is not None:
                logger.info(
                    "patrol selected shortest A* keep-out exit",
                    goal=complete_exit,
                    containing_zones=len(containing_exclusions),
                    egress_mode=egress_mode,
                )
                return point_to_pose_stamped(complete_exit)

            # A rolling costmap can initially reveal no safe cell beyond a
            # large restored circle. Until a full exit exists, choose the
            # shortest A* step that is monotonically farther from every circle
            # containing the robot and never enters any other circle.
            base_outward = np.ones(len(egress_indices), dtype=bool)
            if operator_zones:
                base_outward &= _points_obey_operator_zones(
                    egress_xs,
                    egress_ys,
                    operator_zones,
                )
            # Tier 1: real outward steps only. A hop shorter than the local
            # planner's arrival tolerance produces a no-motion "arrival" and an
            # identical re-pick next tick (the shrinking-hop standstill seen
            # live). Cap the requirement at a full margined exit so a robot
            # already hugging the boundary is sent fully outside.
            outward = base_outward.copy()
            for cx, cy, radius in exclusions:
                candidate_distance_sq = (egress_xs - cx) ** 2 + (egress_ys - cy) ** 2
                start_distance = math.hypot(start[0] - cx, start[1] - cy)
                if start_distance <= radius:
                    required_distance = min(
                        radius + _EGRESS_GOAL_CLEARANCE_M,
                        start_distance + _MIN_EGRESS_STEP_M,
                    )
                    outward &= candidate_distance_sq > required_distance**2
                else:
                    outward &= candidate_distance_sq > radius**2
            intermediate_exit = shortest_reachable_goal(
                egress_indices[outward],
                "intermediate",
                require_full_egress=False,
            )
            if intermediate_exit is None:
                # Tier 2 (degenerate pockets only, e.g. a two-cell safe island
                # at boot): any outward progress beats a standstill, exactly
                # the pre-existing behavior. Mapping expands the pocket and the
                # next selection graduates back to real steps.
                inch_outward = base_outward.copy()
                for cx, cy, radius in exclusions:
                    candidate_distance_sq = (egress_xs - cx) ** 2 + (
                        egress_ys - cy
                    ) ** 2
                    start_distance = math.hypot(start[0] - cx, start[1] - cy)
                    if start_distance <= radius:
                        inch_outward &= (
                            candidate_distance_sq > (start_distance + 1e-6) ** 2
                        )
                    else:
                        inch_outward &= candidate_distance_sq > radius**2
                intermediate_exit = shortest_reachable_goal(
                    egress_indices[inch_outward],
                    "intermediate_inch",
                    require_full_egress=False,
                )
            if intermediate_exit is not None:
                logger.info(
                    "patrol selected shortest bounded A* egress step",
                    goal=intermediate_exit,
                    containing_zones=len(containing_exclusions),
                )
                return point_to_pose_stamped(intermediate_exit)
            now = time.monotonic()
            if now - getattr(self, "_last_egress_diagnostic_at", 0.0) >= 5.0:
                self._last_egress_diagnostic_at = now
                logger.warning(
                    "patrol keep-out egress unavailable",
                    start=(round(float(start[0]), 3), round(float(start[1]), 3)),
                    safe_cells=int(len(egress_indices)),
                    containing_zones=len(containing_exclusions),
                    exclusions=len(exclusions),
                    focus_zones=len(operator_zones),
                    complete=egress_search_stats.get("complete"),
                    intermediate=egress_search_stats.get("intermediate"),
                )
            return None

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
        # Apply hard endpoint constraints before drawing the 64 candidates.
        # With a small focus area, sampling the entire venue first made every
        # draw land outside the polygon and patrol incorrectly reported that no
        # goal existed. Route-level checks below remain authoritative.
        if operator_zones or exclusions:
            candidate_xs = float(occupancy_grid.origin.position.x) + safe_indices[
                :, 1
            ].astype(np.float64) * float(occupancy_grid.resolution)
            candidate_ys = float(occupancy_grid.origin.position.y) + safe_indices[
                :, 0
            ].astype(np.float64) * float(occupancy_grid.resolution)
            base_eligible = np.ones(len(safe_indices), dtype=bool)
            if operator_zones:
                base_eligible &= _points_obey_operator_zones(
                    candidate_xs,
                    candidate_ys,
                    operator_zones,
                )

            def exclusion_eligible(minimum_egress_m: float) -> np.ndarray:
                eligible = base_eligible.copy()
                for cx, cy, radius in exclusions:
                    candidate_distance_sq = (candidate_xs - cx) ** 2 + (
                        candidate_ys - cy
                    ) ** 2
                    start_distance = math.hypot(start[0] - cx, start[1] - cy)
                    if start_distance <= radius:
                        # A restored glass/trap circle can contain the boot
                        # pose or a robot moved by hand. Only allow endpoints
                        # farther from its centre; route validation below still
                        # forbids re-entry after the robot has escaped.
                        required_distance = min(
                            radius,
                            start_distance + minimum_egress_m,
                        )
                        eligible &= (
                            candidate_distance_sq
                            >= max(0.0, required_distance - 1e-6) ** 2
                        )
                    else:
                        eligible &= candidate_distance_sq > radius**2
                return eligible

            eligible = exclusion_eligible(max(0.20, float(occupancy_grid.resolution)))
            if not np.any(eligible) and any(
                math.hypot(start[0] - cx, start[1] - cy) <= radius
                for cx, cy, radius in exclusions
            ):
                # At startup the rolling map may expose less than 20 cm of
                # cleared floor around the robot. Requiring a 20 cm first hop
                # then creates a permanent boot-inside-keep-out deadlock:
                # movement is needed to reveal the space needed for movement.
                # Fall back to one grid-cell of strictly outward progress.
                eligible = exclusion_eligible(
                    max(0.01, float(occupancy_grid.resolution))
                )
                if np.any(eligible):
                    logger.info(
                        "patrol using bounded keep-out egress step",
                        candidates=int(np.count_nonzero(eligible)),
                        resolution_m=float(occupancy_grid.resolution),
                    )
            safe_indices = safe_indices[eligible]
        if len(safe_indices) == 0:
            return None
        n_candidates = min(self._candidates_to_consider, len(safe_indices))
        weights = sampling_weights[safe_indices[:, 0], safe_indices[:, 1]]
        weight_sum = weights.sum()
        probs = weights / weight_sum if weight_sum > 0 else None
        chosen = safe_indices[
            np.random.choice(
                len(safe_indices), size=n_candidates, replace=False, p=probs
            )
        ]
        local_count = min(self._local_candidates_to_consider, len(safe_indices))
        if local_count:
            local_dx = (
                float(occupancy_grid.origin.position.x)
                + safe_indices[:, 1].astype(np.float64)
                * float(occupancy_grid.resolution)
                - float(start[0])
            )
            local_dy = (
                float(occupancy_grid.origin.position.y)
                + safe_indices[:, 0].astype(np.float64)
                * float(occupancy_grid.resolution)
                - float(start[1])
            )
            local_distance_sq = local_dx * local_dx + local_dy * local_dy
            local_selection = np.argpartition(
                local_distance_sq,
                local_count - 1,
            )[:local_count]
            chosen = np.unique(
                np.vstack((chosen, safe_indices[local_selection])),
                axis=0,
            )

        reachable: list[tuple[int, tuple[float, float]]] = []
        for enforce_recent_spacing in (True, False):
            for row, col in chosen:
                world = occupancy_grid.grid_to_world((int(col), int(row), 0))
                candidate = (world.x, world.y)
                if (
                    enforce_recent_spacing
                    and recent_goals
                    and any(
                        math.hypot(candidate[0] - gx, candidate[1] - gy)
                        < self._recent_goal_exclusion_radius_m
                        for gx, gy in recent_goals
                    )
                ):
                    continue
                path = min_cost_astar(
                    routing_costmap,
                    candidate,
                    start,
                    unknown_penalty=1.0,
                    use_cpp=True,
                )
                if path is None:
                    continue
                if exclusions and self._path_reenters_exclusion(path, exclusions):
                    continue
                if operator_zones and not _path_obeys_operator_zones(
                    path,
                    operator_zones,
                ):
                    continue
                new_cells = self._count_new_coverage(
                    path, visited, occupancy_grid, safe_mask
                )
                reachable.append((new_cells, candidate))
            if reachable or not recent_goals:
                break
            logger.info("focus area exhausted recent-goal spacing; relaxing spacing")

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
    _operator_zone_path = _OPERATOR_ZONE_PATH
    _reloc: RelocSpec | None
    navigation_obstacle: In[PoseStamped]

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

    async def handle_navigation_obstacle(self, pose: PoseStamped) -> None:
        self.report_patrol_failure(
            float(pose.position.x),
            float(pose.position.y),
            "repeated obstacle replans at route boundary",
        )

    async def handle_goal_reached(self, msg: Any) -> None:
        """Advance immediately when the planner finally accepts or rejects a leg.

        Replanning cancellations use ``but_will_try_again=True`` and do not
        publish on this stream. A false message therefore means the planner
        has definitively rejected/cancelled this patrol leg; waiting for the
        15-second liveness watchdog leaves the dog visibly idle.
        """

        self._goal_reached_event.set()
        if not bool(msg.data):
            logger.info("patrol leg rejected; selecting another goal immediately")

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
        zone_setter = getattr(self._router, "set_operator_zones", None)
        if zone_setter is not None:
            session_epoch = (
                str(epoch["epoch_id"])
                if (epoch := _load_odom_epoch()) is not None
                else None
            )
            if transform is not None:
                _anchor_world_operator_zones(
                    self._operator_zone_path, transform, session_epoch
                )
            zone_setter(
                _load_operator_zones(
                    self._operator_zone_path, transform, session_epoch
                )
            )

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
        # Keep the accepted patrol goal while the local planner waits for a
        # moving obstacle, then let A* route around it if it persists. Disabling
        # replanning here made the first obstacle notification cancel the leg;
        # patrol then selected the same geometric approach again and visibly
        # alternated between standing still and retrying it.
        self._planner_spec.set_replanning_enabled(True)
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
