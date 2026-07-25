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
    insta360_mjpeg_url: str
    insta360_serial: str | None
    rtmp_hls_url: str
    robot_assessment_url: str
    robot_mcp_url: str
    robot_bridge_enabled: bool
    robot_assessment_path: str
    robot_assessment_window_seconds: float
    robot_intervene_score: float
    robot_escort_score: float
    robot_min_confidence: float
    robot_min_quality: float
    robot_min_observation_seconds: float
    robot_consecutive_windows: int
    robot_person_cooldown_seconds: float
    robot_intake_timeout_seconds: float
    intake_db_path: str
    intake_operator_key: str | None
    lidar_bridge_ws_url: str
    tick_hz: float = 2.0


def load_settings() -> Settings:
    cors_raw = os.getenv(
        "CORS_ORIGINS",
        ",".join(
            (
                "http://localhost:3000",
                "http://127.0.0.1:3000",
                "http://localhost:5555",
                "http://127.0.0.1:5555",
            )
        ),
    )
    return Settings(
        demo_mode=os.getenv("DEMO_MODE", "live"),
        camera_source=os.getenv("CAMERA_SOURCE", "webcam"),
        scorer=os.getenv("SCORER", "thresholds"),
        cors_origins=[origin.strip() for origin in cors_raw.split(",") if origin.strip()],
        # stub | live -- "live" streams webcam frames to fatigue_fastapi_service
        scorer_backend=os.getenv("SCORER_BACKEND", "live"),
        fatigue_ws_url=os.getenv(
            "FATIGUE_WS_URL", "ws://127.0.0.1:8001/v1/streams/detect"
        ),
        robot_camera_url=os.getenv(
            "ROBOT_CAMERA_URL", "http://localhost:5555/video_feed/camera"
        ),
        robot_status_url=os.getenv(
            "ROBOT_STATUS_URL", "http://localhost:5555/operator/status"
        ),
        insta360_mjpeg_url=os.getenv(
            "INSTA360_MJPEG_URL", "http://127.0.0.1:5556/video"
        ),
        insta360_serial=os.getenv("INSTA360_SERIAL") or None,
        rtmp_hls_url=os.getenv(
            "RTMP_HLS_URL", "http://localhost:8080/hls/stream.m3u8"
        ),
        robot_assessment_url=os.getenv(
            "ROBOT_ASSESSMENT_URL", "http://localhost:5555/operator/assessment"
        ),
        robot_mcp_url=os.getenv("ROBOT_MCP_URL", "http://localhost:9990/mcp"),
        robot_bridge_enabled=_env_bool("ROBOT_BRIDGE_ENABLED", False),
        robot_assessment_path=os.getenv(
            "ROBOT_ASSESSMENT_PATH", "data/assessments.jsonl"
        ),
        robot_assessment_window_seconds=float(
            os.getenv("ROBOT_ASSESSMENT_WINDOW_SECONDS", "5.0")
        ),
        robot_intervene_score=float(os.getenv("ROBOT_INTERVENE_SCORE", "0.65")),
        robot_escort_score=float(os.getenv("ROBOT_ESCORT_SCORE", "0.75")),
        robot_min_confidence=float(os.getenv("ROBOT_MIN_CONFIDENCE", "0.55")),
        robot_min_quality=float(os.getenv("ROBOT_MIN_QUALITY", "0.55")),
        robot_min_observation_seconds=float(
            os.getenv("ROBOT_MIN_OBSERVATION_SECONDS", "8.0")
        ),
        robot_consecutive_windows=max(
            1, int(os.getenv("ROBOT_CONSECUTIVE_WINDOWS", "2"))
        ),
        robot_person_cooldown_seconds=float(
            os.getenv("ROBOT_PERSON_COOLDOWN_SECONDS", "180.0")
        ),
        robot_intake_timeout_seconds=float(
            os.getenv("ROBOT_INTAKE_TIMEOUT_SECONDS", "180.0")
        ),
        intake_db_path=os.getenv("INTAKE_DB_PATH", "data/nightwatch.db"),
        intake_operator_key=os.getenv("INTAKE_OPERATOR_KEY") or None,
        lidar_bridge_ws_url=os.getenv(
            "LIDAR_BRIDGE_WS_URL", "ws://127.0.0.1:8010/ws/map"
        ),
    )
