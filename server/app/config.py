from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class Settings:
    demo_mode: str
    camera_source: str
    scorer: str
    cors_origins: list[str]
    scorer_backend: str
    fatigue_ws_url: str
    robot_camera_url: str
    robot_status_url: str
    robot_bridge_enabled: bool
    robot_assessment_path: str
    robot_assessment_window_seconds: float
    tick_hz: float = 2.0


def load_settings() -> Settings:
    cors_raw = os.getenv("CORS_ORIGINS", "http://localhost:3000")
    return Settings(
        demo_mode=os.getenv("DEMO_MODE", "stub"),
        camera_source=os.getenv("CAMERA_SOURCE", "webcam"),
        scorer=os.getenv("SCORER", "thresholds"),
        cors_origins=[origin.strip() for origin in cors_raw.split(",") if origin.strip()],
        # stub | live -- "live" streams webcam frames to fatigue_fastapi_service
        scorer_backend=os.getenv("SCORER_BACKEND", "stub"),
        fatigue_ws_url=os.getenv(
            "FATIGUE_WS_URL", "ws://127.0.0.1:8001/v1/streams/detect"
        ),
        robot_camera_url=os.getenv(
            "ROBOT_CAMERA_URL", "http://localhost:5555/video_feed/camera"
        ),
        robot_status_url=os.getenv(
            "ROBOT_STATUS_URL", "http://localhost:5555/operator/status"
        ),
        robot_bridge_enabled=_env_bool("ROBOT_BRIDGE_ENABLED", False),
        robot_assessment_path=os.getenv(
            "ROBOT_ASSESSMENT_PATH", "data/assessments.jsonl"
        ),
        robot_assessment_window_seconds=float(
            os.getenv("ROBOT_ASSESSMENT_WINDOW_SECONDS", "5.0")
        ),
    )
