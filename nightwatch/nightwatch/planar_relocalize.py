"""Fast, gravity-preserving global localization for indoor floor maps.

The upstream 3-D FPFH/RANSAC relocalizer is a poor startup primitive for this
robot: a mostly planar building contains many repeated floors, ceilings and
corridors, and one solve can occupy a CPU core for a minute.  This module uses
the information that is actually distinctive indoors -- the top-down layout
of elevated structure -- and never permits roll or pitch.

It is intentionally conservative.  A candidate is returned only when:

* enough of the live structure overlaps the saved map;
* saved structure in the same footprint agrees in the other direction; and
* the best spatially distinct hypothesis beats the runner-up.

An ambiguous result is not an error.  The robot can continue exploring on its
live map and retry after its bounded local cloud has accumulated a more unique
shape.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import cv2  # type: ignore[import-untyped]
import numpy as np
from scipy.optimize import minimize  # type: ignore[import-untyped]
from scipy.spatial import ConvexHull, cKDTree  # type: ignore[import-untyped]


@dataclass(frozen=True)
class PlanarRelocalization:
    transform: np.ndarray
    score: float
    overlap: float
    runner_up_score: float


def _points(cloud: Any) -> np.ndarray:
    values = np.asarray(cloud.points, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3:
        raise ValueError("point cloud must contain Nx3 points")
    return values[np.isfinite(values).all(axis=1)]


def _structure_xy(points: np.ndarray) -> tuple[np.ndarray, float]:
    """Return elevated structure and a robust floor-height estimate."""
    if len(points) < 500:
        return np.empty((0, 2), dtype=np.float64), 0.0
    floor_z = float(np.quantile(points[:, 2], 0.02))
    # The Go2 lidar is roughly 0.55 m above the floor.  Sampling another
    # 0.45-1.55 m above the estimated floor discards the rotationally symmetric
    # floor/ceiling and retains walls, furniture and door frames.
    low = floor_z + 0.70
    high = floor_z + 1.70
    mask = (points[:, 2] >= low) & (points[:, 2] <= high)
    xy = points[mask, :2]
    if len(xy) < 300:
        # Sparse maps sometimes have few returns at chest height.  Falling
        # back to the non-floor middle of the z distribution is still safer
        # than using the floor plane.
        z_low, z_high = np.quantile(points[:, 2], [0.35, 0.92])
        xy = points[
            (points[:, 2] >= z_low) & (points[:, 2] <= z_high),
            :2,
        ]
    return xy, floor_z


def _rotation(yaw: float) -> np.ndarray:
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    return np.array(
        [[cos_yaw, -sin_yaw], [sin_yaw, cos_yaw]],
        dtype=np.float64,
    )


def _inside_hull(points: np.ndarray, hull_points: np.ndarray) -> np.ndarray:
    if len(hull_points) < 3:
        return np.zeros(len(points), dtype=bool)
    hull = ConvexHull(hull_points)
    # scipy's hull equations are n.x + b <= 0 for points inside.
    return np.all(
        points @ hull.equations[:, :2].T + hull.equations[:, 2] <= 1e-7,
        axis=1,
    )


def _candidate_metrics(
    local_xy: np.ndarray,
    saved_xy: np.ndarray,
    saved_tree: cKDTree,
    yaw: float,
    tx: float,
    ty: float,
    raster_score: float,
) -> tuple[float, float, float, float, float]:
    transformed = local_xy @ _rotation(yaw).T + np.array([tx, ty])
    source_distance = saved_tree.query(transformed, k=1)[0]
    source_overlap = float(np.mean(source_distance < 0.25))

    in_footprint = _inside_hull(saved_xy, transformed)
    footprint_saved = saved_xy[in_footprint]
    if len(footprint_saved) < 100:
        return 0.0, source_overlap, 0.0, tx, ty
    target_distance = cKDTree(transformed).query(footprint_saved, k=1)[0]
    target_overlap = float(np.mean(target_distance < 0.25))
    symmetric = (
        2.0 * source_overlap * target_overlap / (source_overlap + target_overlap)
        if source_overlap + target_overlap
        else 0.0
    )
    # Raster correlation rewards matching topology; symmetric overlap prevents
    # a dense subset from masquerading as a complete room.
    combined = 0.68 * symmetric + 0.32 * max(0.0, raster_score)
    return combined, source_overlap, target_overlap, tx, ty


def relocalize_planar(
    saved_cloud: Any,
    live_cloud: Any,
    *,
    resolution: float = 0.10,
    yaw_step_deg: float = 4.0,
    min_overlap: float = 0.66,
    min_score: float = 0.54,
    min_margin: float = 0.012,
) -> PlanarRelocalization | None:
    """Find a confident WORLD->MAP transform, or return ``None``.

    The matcher is venue-independent: its search bounds are derived entirely
    from the saved cloud and it searches all headings.  Translation and yaw
    are refined against point-to-map distance after raster correlation.
    """
    saved_points = _points(saved_cloud)
    live_points = _points(live_cloud)
    saved_xy, saved_floor = _structure_xy(saved_points)
    live_xy, live_floor = _structure_xy(live_points)
    if len(saved_xy) < 500 or len(live_xy) < 500:
        return None

    map_min = saved_xy.min(axis=0) - 1.0
    map_max = saved_xy.max(axis=0) + 1.0
    shape_xy = np.ceil((map_max - map_min) / resolution).astype(int) + 1
    if np.any(shape_xy < 8) or np.any(shape_xy > 5000):
        return None

    saved_raster = np.zeros((shape_xy[1], shape_xy[0]), dtype=np.uint8)
    saved_cells = np.floor((saved_xy - map_min) / resolution).astype(int)
    saved_raster[saved_cells[:, 1], saved_cells[:, 0]] = 255
    kernel = np.ones((3, 3), dtype=np.uint8)
    saved_raster = cv2.dilate(saved_raster, kernel)

    center = live_xy.mean(axis=0)
    raw: list[tuple[float, float, float, float]] = []
    for yaw_deg in np.arange(-180.0, 180.0, yaw_step_deg):
        yaw = math.radians(float(yaw_deg))
        rotation = _rotation(yaw)
        rotated = (live_xy - center) @ rotation.T
        local_min = rotated.min(axis=0) - 0.30
        local_max = rotated.max(axis=0) + 0.30
        local_shape = (
            np.ceil((local_max - local_min) / resolution).astype(int) + 1
        )
        if (
            local_shape[0] >= shape_xy[0]
            or local_shape[1] >= shape_xy[1]
            or np.any(local_shape < 3)
        ):
            continue
        template = np.zeros((local_shape[1], local_shape[0]), dtype=np.uint8)
        cells = np.floor((rotated - local_min) / resolution).astype(int)
        template[cells[:, 1], cells[:, 0]] = 255
        template = cv2.dilate(template, kernel)
        correlation = cv2.matchTemplate(
            saved_raster,
            template,
            cv2.TM_CCOEFF_NORMED,
        )
        # Keep two translation peaks per heading; the global de-duplication
        # below makes repeated rooms visible to the ambiguity gate.
        for _ in range(2):
            _minimum, maximum, _min_at, max_at = cv2.minMaxLoc(correlation)
            translation = (
                map_min
                + np.asarray(max_at, dtype=np.float64) * resolution
                - local_min
                - rotation @ center
            )
            raw.append(
                (
                    float(maximum),
                    yaw,
                    float(translation[0]),
                    float(translation[1]),
                )
            )
            cv2.circle(correlation, max_at, max(3, round(1.5 / resolution)), -1, -1)

    if not raw:
        return None
    raw.sort(reverse=True)
    distinct: list[tuple[float, float, float, float]] = []
    for candidate in raw:
        _score, yaw, tx, ty = candidate
        if all(
            (
                abs(math.degrees(math.atan2(math.sin(yaw - other[1]), math.cos(yaw - other[1]))))
                > 8.0
                or math.hypot(tx - other[2], ty - other[3]) > 2.0
            )
            for other in distinct
        ):
            distinct.append(candidate)
        if len(distinct) >= 5:
            break

    saved_tree = cKDTree(saved_xy)
    # A spatially uniform sample bounds optimizer cost without biasing toward
    # whichever wall happens to be densest in the raw scan.
    local_for_fit = live_xy[:: max(1, len(live_xy) // 2500)]

    evaluated: list[tuple[float, float, float, float, float, float]] = []
    for raster_score, seed_yaw, seed_x, seed_y in distinct[:4]:
        def objective(pose: np.ndarray) -> float:
            transformed = (
                local_for_fit @ _rotation(float(pose[0])).T + pose[1:3]
            )
            distances = saved_tree.query(transformed, k=1)[0]
            cap = float(np.quantile(distances, 0.70))
            return float(np.mean(np.minimum(distances, cap) ** 2))

        refined = minimize(
            objective,
            np.array([seed_yaw, seed_x, seed_y], dtype=np.float64),
            method="Nelder-Mead",
            options={"maxiter": 80, "xatol": 3e-4, "fatol": 2e-7},
        )
        yaw, tx, ty = (float(value) for value in refined.x)
        combined, source_overlap, target_overlap, _tx, _ty = _candidate_metrics(
            live_xy,
            saved_xy,
            saved_tree,
            yaw,
            tx,
            ty,
            raster_score,
        )
        symmetric = (
            2.0
            * source_overlap
            * target_overlap
            / (source_overlap + target_overlap)
            if source_overlap + target_overlap
            else 0.0
        )
        evaluated.append(
            (combined, symmetric, raster_score, yaw, tx, ty)
        )

    evaluated.sort(reverse=True)
    best = evaluated[0]
    runner_up = evaluated[1][0] if len(evaluated) > 1 else 0.0
    if (
        best[0] < min_score
        or best[1] < min_overlap
        or best[0] - runner_up < min_margin
    ):
        return None

    transform = np.eye(4)
    transform[:2, :2] = _rotation(best[3])
    transform[:2, 3] = [best[4], best[5]]
    transform[2, 3] = saved_floor - live_floor
    return PlanarRelocalization(
        transform=transform,
        score=float(best[0]),
        overlap=float(best[1]),
        runner_up_score=float(runner_up),
    )
