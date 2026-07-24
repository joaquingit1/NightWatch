from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import load_settings
from app.routers import api, form, lidar, thoughts, video
from app.services.frame_source import create_frame_source
from app.services.intake_db import IntakeDatabase
from app.services.ledger_memory import LedgerMemory
from app.services.live_scorer import LiveScoreSource
from app.services.policy_events import LivePolicyEventSource, StubPolicyEventSource
from app.services.robot_bridge import RobotBridge
from app.services.scorer import StubScoreSource


async def _app_loop(app: FastAPI) -> None:
    settings = app.state.settings
    interval = 1.0 / settings.tick_hz
    last_event_ts = 0.0
    while True:
        app.state.score_source.tick()
        frame = app.state.score_source.latest()
        tracks = frame.people or ([frame] if frame.person_id else [])
        for track in tracks:
            if track.person_id and track.confidence >= 0.15:
                app.state.ledger.observe_score(track.person_id, track.score)
        app.state.policy_source.tick()
        events = app.state.policy_source.subscribe()
        for event in events:
            if event.ts > last_event_ts:
                app.state.ledger.record_event(event)
                last_event_ts = event.ts
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings

    if settings.scorer_backend == "live":
        # frame_source is created right after this, so the lambda's lookup
        # of app.state.frame_source only resolves once streaming actually starts.
        app.state.score_source = LiveScoreSource(
            ws_url=settings.fatigue_ws_url,
            get_frame=lambda: app.state.frame_source.get_pov_frame()
            if hasattr(app.state, "frame_source")
            else None,
        )
    else:
        app.state.score_source = StubScoreSource()

    app.state.frame_source = create_frame_source(
        settings.camera_source,
        score_provider=app.state.score_source.latest,
        robot_camera_url=settings.robot_camera_url,
        insta360_mjpeg_url=settings.insta360_mjpeg_url,
    )
    app.state.ledger = LedgerMemory()
    app.state.intake_db = IntakeDatabase(settings.intake_db_path)
    app.state.intake_db.init_schema()
    app.state.policy_source = (
        StubPolicyEventSource()
        if settings.demo_mode == "stub"
        else LivePolicyEventSource(
            get_frame=app.state.score_source.latest,
            get_pending_escorts=app.state.intake_db.list_pending_escort,
        )
    )
    app.state.robot_bridge = None

    robot_bridge: RobotBridge | None = None
    if settings.robot_bridge_enabled:
        robot_bridge = RobotBridge(
            assessment_path=settings.robot_assessment_path,
            status_url=settings.robot_status_url,
            assessment_url=settings.robot_assessment_url,
            mcp_url=settings.robot_mcp_url,
            window_seconds=settings.robot_assessment_window_seconds,
            get_latest_frame=app.state.score_source.latest,
            publish_event=app.state.policy_source.publish,
            intervene_score=settings.robot_intervene_score,
            escort_score=settings.robot_escort_score,
            min_confidence=settings.robot_min_confidence,
            min_quality=settings.robot_min_quality,
            min_observation_seconds=settings.robot_min_observation_seconds,
            consecutive_windows=settings.robot_consecutive_windows,
            person_cooldown_seconds=settings.robot_person_cooldown_seconds,
            update_intake_status=app.state.intake_db.update_status,
        )
        app.state.robot_bridge = robot_bridge
        await robot_bridge.start()

    if isinstance(app.state.score_source, LiveScoreSource):
        await app.state.score_source.start()

    task = asyncio.create_task(_app_loop(app))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if isinstance(app.state.score_source, LiveScoreSource):
            await app.state.score_source.stop()
        if robot_bridge is not None:
            await robot_bridge.stop()
        app.state.frame_source.close()


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Night Watch API", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(video.router)
    app.include_router(thoughts.router)
    app.include_router(api.router)
    app.include_router(form.router)
    app.include_router(lidar.router)
    return app


app = create_app()
