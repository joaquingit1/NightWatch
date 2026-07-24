from __future__ import annotations

from dataclasses import dataclass
from statistics import median

import cv2
import numpy as np

from .metrics import pixel_landmarks

# Face Landmarker outputs 478 points. The final ten points describe both irises.
LEFT_IRIS = (468, 469, 470, 471, 472)
RIGHT_IRIS = (473, 474, 475, 476, 477)
LEFT_EYE_CORNERS = (33, 133)
RIGHT_EYE_CORNERS = (362, 263)
LEFT_UPPER_LID = (159, 158)
LEFT_LOWER_LID = (145, 153)
RIGHT_UPPER_LID = (386, 385)
RIGHT_LOWER_LID = (374, 380)

# Six 2D landmarks used by solvePnP: nose, chin, eye corners and mouth corners.
HEAD_POSE_LANDMARKS = (1, 152, 33, 263, 61, 291)
HEAD_MODEL_POINTS = np.asarray(
    [
        (0.0, 0.0, 0.0),
        (0.0, -330.0, -65.0),
        (-225.0, 170.0, -135.0),
        (225.0, 170.0, -135.0),
        (-150.0, -150.0, -125.0),
        (150.0, -150.0, -125.0),
    ],
    dtype=np.float64,
)


@dataclass(frozen=True)
class AttentionGeometry:
    pitch: float
    yaw: float
    roll: float
    gaze_horizontal: float | None
    gaze_vertical: float | None


def _mean_point(points: np.ndarray, indices: tuple[int, ...]) -> np.ndarray:
    return points[list(indices), :2].mean(axis=0)


def _axis_ratio(value: float, first: float, second: float) -> float | None:
    low = min(first, second)
    high = max(first, second)
    if high - low < 1e-6:
        return None
    return (value - low) / (high - low)


def estimate_gaze(points: np.ndarray) -> tuple[float | None, float | None]:
    """Estimate gaze in normalized eye coordinates.

    Both returned values are centered around zero. Approximately -1/+1 means
    the iris is near an edge of the visible eye region.
    """
    if len(points) < 478:
        return None, None

    horizontal_ratios: list[float] = []
    vertical_ratios: list[float] = []
    for iris, corners, upper_lid, lower_lid in (
        (LEFT_IRIS, LEFT_EYE_CORNERS, LEFT_UPPER_LID, LEFT_LOWER_LID),
        (RIGHT_IRIS, RIGHT_EYE_CORNERS, RIGHT_UPPER_LID, RIGHT_LOWER_LID),
    ):
        iris_center = _mean_point(points, iris)
        horizontal = _axis_ratio(
            float(iris_center[0]),
            float(points[corners[0], 0]),
            float(points[corners[1], 0]),
        )
        upper_y = float(_mean_point(points, upper_lid)[1])
        lower_y = float(_mean_point(points, lower_lid)[1])
        vertical = _axis_ratio(float(iris_center[1]), upper_y, lower_y)
        if horizontal is not None:
            horizontal_ratios.append(horizontal)
        if vertical is not None:
            vertical_ratios.append(vertical)

    horizontal_result = (
        2.0 * (float(np.mean(horizontal_ratios)) - 0.5)
        if horizontal_ratios
        else None
    )
    vertical_result = (
        2.0 * (float(np.mean(vertical_ratios)) - 0.5)
        if vertical_ratios
        else None
    )
    return horizontal_result, vertical_result


def estimate_head_pose(
    normalized_landmarks: np.ndarray, image_size: tuple[int, int]
) -> tuple[float, float, float] | None:
    """Estimate pitch, yaw and roll in degrees with a perspective-n-point fit."""
    width, height = image_size
    points = pixel_landmarks(normalized_landmarks, image_size)
    image_points = np.asarray(
        [points[index, :2] for index in HEAD_POSE_LANDMARKS],
        dtype=np.float64,
    )
    focal_length = float(max(width, height))
    camera_matrix = np.asarray(
        [
            (focal_length, 0.0, width / 2.0),
            (0.0, focal_length, height / 2.0),
            (0.0, 0.0, 1.0),
        ],
        dtype=np.float64,
    )
    distortion = np.zeros((4, 1), dtype=np.float64)
    success, rotation_vector, _ = cv2.solvePnP(
        HEAD_MODEL_POINTS,
        image_points,
        camera_matrix,
        distortion,
        flags=cv2.SOLVEPNP_ITERATIVE,
    )
    if not success:
        return None
    rotation_matrix, _ = cv2.Rodrigues(rotation_vector)
    angles = cv2.RQDecomp3x3(rotation_matrix)[0]
    pitch, yaw, roll = (float(angle) for angle in angles)
    return pitch, yaw, roll


def extract_attention_geometry(
    normalized_landmarks: np.ndarray, image_size: tuple[int, int]
) -> AttentionGeometry | None:
    pose = estimate_head_pose(normalized_landmarks, image_size)
    if pose is None:
        return None
    points = pixel_landmarks(normalized_landmarks, image_size)
    gaze_horizontal, gaze_vertical = estimate_gaze(points)
    return AttentionGeometry(
        pitch=pose[0],
        yaw=pose[1],
        roll=pose[2],
        gaze_horizontal=gaze_horizontal,
        gaze_vertical=gaze_vertical,
    )


class AttentionCalibrator:
    """Create a per-person neutral-pose baseline from the first usable frames."""

    def __init__(self, required_frames: int = 20) -> None:
        if required_frames < 1:
            raise ValueError("required_frames must be positive")
        self.required_frames = required_frames
        self._samples: list[AttentionGeometry] = []
        self._baseline: AttentionGeometry | None = None

    @property
    def ready(self) -> bool:
        return self._baseline is not None

    @property
    def progress(self) -> float:
        return min(1.0, len(self._samples) / self.required_frames)

    def update(self, geometry: AttentionGeometry) -> AttentionGeometry | None:
        if self._baseline is None:
            self._samples.append(geometry)
            if len(self._samples) >= self.required_frames:
                horizontal = [
                    item.gaze_horizontal
                    for item in self._samples
                    if item.gaze_horizontal is not None
                ]
                vertical = [
                    item.gaze_vertical
                    for item in self._samples
                    if item.gaze_vertical is not None
                ]
                self._baseline = AttentionGeometry(
                    pitch=median(item.pitch for item in self._samples),
                    yaw=median(item.yaw for item in self._samples),
                    roll=median(item.roll for item in self._samples),
                    gaze_horizontal=median(horizontal) if horizontal else None,
                    gaze_vertical=median(vertical) if vertical else None,
                )
            else:
                return None

        baseline = self._baseline
        assert baseline is not None

        def angle_delta(value: float, reference: float) -> float:
            return (value - reference + 180.0) % 360.0 - 180.0

        return AttentionGeometry(
            pitch=angle_delta(geometry.pitch, baseline.pitch),
            yaw=angle_delta(geometry.yaw, baseline.yaw),
            roll=angle_delta(geometry.roll, baseline.roll),
            gaze_horizontal=(
                geometry.gaze_horizontal - baseline.gaze_horizontal
                if geometry.gaze_horizontal is not None
                and baseline.gaze_horizontal is not None
                else None
            ),
            gaze_vertical=(
                geometry.gaze_vertical - baseline.gaze_vertical
                if geometry.gaze_vertical is not None
                and baseline.gaze_vertical is not None
                else None
            ),
        )

    def reset(self) -> None:
        self._samples.clear()
        self._baseline = None
