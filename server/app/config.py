from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    demo_mode: str
    camera_source: str
    scorer: str
    cors_origins: list[str]
    scorer_backend: str
    fatigue_ws_url: str
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
    )
