from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.contracts import CaptureRecord, FeatureVector, fatigue_frame_to_dict

router = APIRouter(prefix="/api", tags=["api"])


class AdoptRequest(BaseModel):
    name_alias: str
    person_id: str | None = None
    consent: bool = True


class OutcomeRequest(BaseModel):
    nap_id: str | None = None
    person_id: str | None = None
    outcome: str = "better"


class CaptureRequest(BaseModel):
    session_id: str
    person_id: str
    ts_start: float
    fps: float
    features: list[dict[str, Any]] = Field(default_factory=list)
    kss: int = Field(ge=1, le=9, default=5)
    hours_awake: float = 0.0
    glasses: bool = False
    lighting: str = "normal"
    consent_raw_video: bool = False


@router.get("/score")
async def get_score(request: Request) -> dict[str, Any]:
    frame = request.app.state.score_source.latest()
    return fatigue_frame_to_dict(frame)


@router.get("/plan")
async def get_plan(request: Request) -> dict[str, Any]:
    pending = request.app.state.intake_db.list_pending_escort()
    return request.app.state.ledger.build_plan(pending)


@router.get("/ledger")
async def get_ledger(request: Request) -> dict[str, Any]:
    return request.app.state.ledger.get_ledger()


@router.get("/leaderboard")
async def get_leaderboard(request: Request) -> dict[str, Any]:
    return request.app.state.ledger.get_leaderboard()


@router.post("/adopt")
async def post_adopt(request: Request, body: AdoptRequest) -> dict[str, Any]:
    return request.app.state.ledger.record_adopt(body.model_dump())


@router.post("/capture")
async def post_capture(request: Request, body: CaptureRequest) -> dict[str, Any]:
    features = [
        FeatureVector(
            ts=f["ts"],
            ear_l=f.get("ear_l", 0.0),
            ear_r=f.get("ear_r", 0.0),
            ear_corrected=f.get("ear_corrected", 0.0),
            eye_cnn_p=f.get("eye_cnn_p", -1.0),
            mar=f.get("mar", 0.0),
            head_pitch=f.get("head_pitch", 0.0),
            head_yaw=f.get("head_yaw", 0.0),
            head_roll=f.get("head_roll", 0.0),
            pitch_vel=f.get("pitch_vel", 0.0),
            slump_deg=f.get("slump_deg", -1.0),
            movement=f.get("movement", 0.0),
            quality=f.get("quality", 1.0),
        )
        for f in body.features
    ]
    record = CaptureRecord(
        session_id=body.session_id,
        person_id=body.person_id,
        ts_start=body.ts_start,
        fps=body.fps,
        features=features,
        kss=body.kss,
        hours_awake=body.hours_awake,
        glasses=body.glasses,
        lighting=body.lighting,
        consent_raw_video=body.consent_raw_video,
    )
    return request.app.state.ledger.record_capture(record)


@router.post("/outcome")
async def post_outcome(request: Request, body: OutcomeRequest) -> dict[str, Any]:
    return request.app.state.ledger.record_outcome(body.model_dump())
