from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.contracts import CaptureRecord, FeatureVector, fatigue_frame_to_dict

router = APIRouter(prefix="/api", tags=["api"])

_TTS_SAMPLE_DIR = (
    Path(__file__).resolve().parents[3] / "nightwatch" / "assets" / "tts_samples"
)
_AUDIO_CUES = {
    "triage_01": "sample1_greeting.wav",
    "approach_01": "sample1_greeting.wav",
    "diagnose_01": "sample1_greeting.wav",
    "prescribe_01": "sample1_greeting.wav",
    "escort_01": "sample2_escort.wav",
    "nap_registered_01": "sample2_escort.wav",
}


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


class RobotActionRequest(BaseModel):
    action: str


class FatigueAutoTakeoverRequest(BaseModel):
    enabled: bool


class BedroomRequest(BaseModel):
    at_robot: bool = False
    world_x: float | None = None
    world_y: float | None = None
    world_z: float = 0.0


@router.get("/score")
async def get_score(request: Request) -> dict[str, Any]:
    frame = request.app.state.score_source.latest()
    return fatigue_frame_to_dict(frame)


@router.get("/audio/{cue_id}")
async def get_audio_cue(cue_id: str) -> FileResponse:
    """Serve only the reviewed booth voice cues referenced by policy events."""
    filename = _AUDIO_CUES.get(cue_id)
    if filename is None:
        raise HTTPException(status_code=404, detail="unknown audio cue")
    path = _TTS_SAMPLE_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=503, detail="audio cue unavailable")
    return FileResponse(path, media_type="audio/wav", filename=filename)


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


@router.get("/robot/status")
async def get_robot_status(request: Request) -> dict[str, Any]:
    bridge = getattr(request.app.state, "robot_bridge", None)
    if bridge is None:
        return {
            "enabled": False,
            "connected": False,
            "policy_active": False,
        }
    return bridge.snapshot()


@router.post("/robot/action")
async def post_robot_action(
    request: Request, body: RobotActionRequest
) -> dict[str, Any]:
    bridge = getattr(request.app.state, "robot_bridge", None)
    if bridge is None:
        return {"ok": False, "message": "Robot bridge is unavailable"}
    result = await bridge.operator_action(body.action)
    return {"ok": result.ok, "message": result.text}


@router.put("/robot/fatigue-auto-takeover")
async def put_fatigue_auto_takeover(
    request: Request, body: FatigueAutoTakeoverRequest
) -> dict[str, Any]:
    bridge = getattr(request.app.state, "robot_bridge", None)
    if bridge is None:
        raise HTTPException(status_code=503, detail="robot bridge is unavailable")
    return bridge.set_fatigue_auto_takeover(body.enabled)


@router.post("/robot/approach-nearest")
async def post_robot_approach_nearest(request: Request) -> dict[str, Any]:
    bridge = getattr(request.app.state, "robot_bridge", None)
    if bridge is None:
        raise HTTPException(status_code=503, detail="robot bridge is unavailable")
    if not bridge.request_operator_approach():
        raise HTTPException(
            status_code=409,
            detail="robot is offline, busy, held, or not in cruise mode",
        )
    return {"ok": True, "message": "nearest-person interaction started"}


@router.post("/robot/cancel-interaction")
async def post_robot_cancel_interaction(request: Request) -> dict[str, Any]:
    bridge = getattr(request.app.state, "robot_bridge", None)
    if bridge is None:
        raise HTTPException(status_code=503, detail="robot bridge is unavailable")
    return {
        "ok": bridge.cancel_interaction(),
        "message": "interaction cancellation requested",
    }


@router.get("/robot/bedroom")
async def get_robot_bedroom(request: Request) -> dict[str, Any]:
    bridge = getattr(request.app.state, "robot_bridge", None)
    if bridge is None:
        return {"bedroom": None, "connected": False}
    status = bridge.snapshot()
    return {
        "bedroom": status.get("bedroom"),
        "connected": bool(status.get("connected")),
    }


@router.put("/robot/bedroom")
async def put_robot_bedroom(
    request: Request, body: BedroomRequest
) -> dict[str, Any]:
    bridge = getattr(request.app.state, "robot_bridge", None)
    if bridge is None:
        raise HTTPException(status_code=503, detail="robot bridge is unavailable")
    if body.at_robot:
        result = await bridge.set_bedroom_here()
    else:
        if body.world_x is None or body.world_y is None:
            raise HTTPException(
                status_code=422,
                detail="world_x and world_y are required for a map position",
            )
        result = await bridge.set_bedroom_at(
            body.world_x, body.world_y, body.world_z
        )
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.text)
    return {
        "ok": True,
        "message": result.text,
        "bedroom": bridge.snapshot().get("bedroom"),
    }


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
