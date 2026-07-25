from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from common.metrics import FatigueThresholds

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_YOLO_MODEL = (
    PROJECT_ROOT
    / "02_yolov8face_mediapipe"
    / "models"
    / "yolov8n-face-lindevs.pt"
)
DEFAULT_LANDMARK_MODEL = (
    PROJECT_ROOT
    / "02_yolov8face_mediapipe"
    / "models"
    / "face_landmarker.task"
)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value, got {value!r}")


def _optional_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value).expanduser().resolve() if value else None


def _apply_threshold_config(
    thresholds: FatigueThresholds, config_path: Path | None
) -> FatigueThresholds:
    if config_path is None:
        return thresholds

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    parameters = payload.get("best_parameters")
    if not isinstance(parameters, dict):
        raise ValueError(f"No best_parameters object in {config_path}")

    mapping = {
        "ear_closed": "ear_closed",
        "mar_yawn": "mar_yawn",
        "head_down_delta": "head_down_pitch_degrees",
        "head_yaw_delta": "head_yaw_away_degrees",
        "gaze_horizontal_delta": "gaze_horizontal_away",
        "gaze_vertical_delta": "gaze_vertical_away",
    }
    overrides = {
        target_name: float(parameters[source_name])
        for source_name, target_name in mapping.items()
        if source_name in parameters
    }
    return replace(thresholds, **overrides)


@dataclass(frozen=True)
class AnalyzerSettings:
    yolo_model: Path
    landmark_model: Path
    device: str
    image_size: int
    detection_confidence: float
    max_faces: int
    minimum_face_size: int
    face_padding: float
    calibration_frames: int
    maximum_missing_seconds: float
    warmup: bool
    thresholds: FatigueThresholds

    @classmethod
    def from_env(cls) -> "AnalyzerSettings":
        thresholds = FatigueThresholds(
            ear_closed=_env_float("FATIGUE_EAR_THRESHOLD", 0.20),
            mar_yawn=_env_float("FATIGUE_MAR_THRESHOLD", 0.35),
            eye_alarm_seconds=_env_float("FATIGUE_EYE_ALARM_SECONDS", 1.20),
            yawn_alarm_seconds=_env_float("FATIGUE_YAWN_ALARM_SECONDS", 1.50),
            perclos_window_seconds=_env_float("FATIGUE_PERCLOS_WINDOW", 30.0),
            perclos_alarm=_env_float("FATIGUE_PERCLOS_THRESHOLD", 0.28),
            head_yaw_away_degrees=_env_float(
                "FATIGUE_HEAD_YAW_THRESHOLD", 25.0
            ),
            head_down_pitch_degrees=_env_float(
                "FATIGUE_HEAD_DOWN_THRESHOLD", 18.0
            ),
            gaze_horizontal_away=_env_float(
                "FATIGUE_GAZE_HORIZONTAL_THRESHOLD", 0.45
            ),
            gaze_vertical_away=_env_float(
                "FATIGUE_GAZE_VERTICAL_THRESHOLD", 0.45
            ),
            attention_alarm_seconds=_env_float(
                "FATIGUE_ATTENTION_ALARM_SECONDS", 2.0
            ),
            head_down_alarm_seconds=_env_float(
                "FATIGUE_HEAD_DOWN_ALARM_SECONDS", 1.5
            ),
            nod_pitch_degrees=_env_float("FATIGUE_NOD_ANGLE", 12.0),
            nod_alarm_count=_env_int("FATIGUE_NOD_ALARM_COUNT", 2),
        )
        thresholds = _apply_threshold_config(
            thresholds, _optional_path("FATIGUE_THRESHOLD_CONFIG")
        )
        return cls(
            yolo_model=Path(
                os.getenv("FATIGUE_YOLO_MODEL", str(DEFAULT_YOLO_MODEL))
            ).expanduser().resolve(),
            landmark_model=Path(
                os.getenv(
                    "FATIGUE_LANDMARK_MODEL", str(DEFAULT_LANDMARK_MODEL)
                )
            ).expanduser().resolve(),
            device=os.getenv("FATIGUE_DEVICE", "auto"),
            image_size=_env_int("FATIGUE_IMAGE_SIZE", 640),
            detection_confidence=_env_float(
                "FATIGUE_DETECTION_CONFIDENCE", 0.35
            ),
            # During robot observations the selected visitor is centered and
            # should dominate the frame. Capping sequential MediaPipe work
            # prevents a crowded booth background from multiplying latency.
            max_faces=_env_int("FATIGUE_MAX_FACES", 6),
            minimum_face_size=_env_int("FATIGUE_MINIMUM_FACE_SIZE", 20),
            face_padding=_env_float("FATIGUE_FACE_PADDING", 0.15),
            calibration_frames=_env_int("FATIGUE_CALIBRATION_FRAMES", 6),
            maximum_missing_seconds=_env_float(
                "FATIGUE_MAXIMUM_MISSING_SECONDS", 1.5
            ),
            warmup=_env_bool("FATIGUE_WARMUP", True),
            thresholds=thresholds,
        )


@dataclass(frozen=True)
class ServiceSettings:
    analyzer: AnalyzerSettings
    max_frame_bytes: int
    max_frame_pixels: int
    annotated_jpeg_quality: int

    @classmethod
    def from_env(cls) -> "ServiceSettings":
        return cls(
            analyzer=AnalyzerSettings.from_env(),
            max_frame_bytes=_env_int(
                "FATIGUE_MAX_FRAME_BYTES", 8 * 1024 * 1024
            ),
            max_frame_pixels=_env_int(
                "FATIGUE_MAX_FRAME_PIXELS", 3840 * 2160
            ),
            annotated_jpeg_quality=_env_int(
                "FATIGUE_ANNOTATED_JPEG_QUALITY", 85
            ),
        )
