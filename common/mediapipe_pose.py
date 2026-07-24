from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np

from .assets import download_file


POSE_LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
)


def ensure_pose_landmarker_model(path: Path) -> Path:
    return download_file(POSE_LANDMARKER_URL, path)


@dataclass(frozen=True)
class PoseLandmarks:
    normalized: np.ndarray
    world: np.ndarray
    visibility: np.ndarray
    presence: np.ndarray


class PoseLandmarkDetector:
    """MediaPipe Pose Landmarker wrapper for a single video stream."""

    def __init__(
        self,
        model_path: Path,
        *,
        minimum_detection_confidence: float = 0.5,
        minimum_presence_confidence: float = 0.5,
        minimum_tracking_confidence: float = 0.5,
        maximum_poses: int = 1,
    ) -> None:
        model_path = ensure_pose_landmarker_model(model_path)
        options = mp.tasks.vision.PoseLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_poses=maximum_poses,
            min_pose_detection_confidence=minimum_detection_confidence,
            min_pose_presence_confidence=minimum_presence_confidence,
            min_tracking_confidence=minimum_tracking_confidence,
            output_segmentation_masks=False,
        )
        self._landmarker = (
            mp.tasks.vision.PoseLandmarker.create_from_options(options)
        )
        self._last_timestamp_ms = -1

    def detect(
        self, bgr_image: np.ndarray, timestamp_seconds: float
    ) -> PoseLandmarks | None:
        poses = self.detect_all(bgr_image, timestamp_seconds)
        return poses[0] if poses else None

    def detect_all(
        self, bgr_image: np.ndarray, timestamp_seconds: float
    ) -> list[PoseLandmarks]:
        if bgr_image.size == 0:
            return []
        rgb = np.ascontiguousarray(cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB))
        timestamp_ms = max(
            self._last_timestamp_ms + 1,
            int(round(timestamp_seconds * 1000.0)),
        )
        self._last_timestamp_ms = timestamp_ms
        result = self._landmarker.detect_for_video(
            mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
            timestamp_ms,
        )
        if not result.pose_landmarks:
            return []
        poses = []
        world_landmark_sets = result.pose_world_landmarks or []
        for pose_index, normalized_points in enumerate(result.pose_landmarks):
            world_points = (
                world_landmark_sets[pose_index]
                if pose_index < len(world_landmark_sets)
                else normalized_points
            )
            poses.append(
                PoseLandmarks(
                    normalized=np.asarray(
                        [
                            [point.x, point.y, point.z]
                            for point in normalized_points
                        ],
                        dtype=np.float32,
                    ),
                    world=np.asarray(
                        [
                            [point.x, point.y, point.z]
                            for point in world_points
                        ],
                        dtype=np.float32,
                    ),
                    visibility=np.asarray(
                        [point.visibility for point in normalized_points],
                        dtype=np.float32,
                    ),
                    presence=np.asarray(
                        [point.presence for point in normalized_points],
                        dtype=np.float32,
                    ),
                )
            )
        return poses

    def close(self) -> None:
        self._landmarker.close()

    def __enter__(self) -> "PoseLandmarkDetector":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
