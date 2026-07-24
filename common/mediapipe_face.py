from __future__ import annotations

from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np

from .assets import download_file

FACE_LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/latest/face_landmarker.task"
)


def ensure_face_landmarker_model(path: Path) -> Path:
    return download_file(FACE_LANDMARKER_URL, path)


class FaceLandmarkDetector:
    """Small context-managed wrapper around MediaPipe Face Landmarker."""

    def __init__(
        self,
        model_path: Path,
        *,
        minimum_detection_confidence: float = 0.5,
        minimum_presence_confidence: float = 0.5,
    ) -> None:
        model_path = ensure_face_landmarker_model(model_path)
        options = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
            running_mode=mp.tasks.vision.RunningMode.IMAGE,
            num_faces=1,
            min_face_detection_confidence=minimum_detection_confidence,
            min_face_presence_confidence=minimum_presence_confidence,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
        )
        self._landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)

    def detect(self, bgr_image: np.ndarray) -> np.ndarray | None:
        if bgr_image.size == 0:
            return None
        rgb = np.ascontiguousarray(cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB))
        result = self._landmarker.detect(
            mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        )
        if not result.face_landmarks:
            return None
        return np.asarray(
            [[point.x, point.y, point.z] for point in result.face_landmarks[0]],
            dtype=np.float32,
        )

    def close(self) -> None:
        self._landmarker.close()

    def __enter__(self) -> "FaceLandmarkDetector":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

