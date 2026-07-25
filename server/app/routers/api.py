from __future__ import annotations

import io
import os
import socket
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.contracts import CaptureRecord, FeatureVector, fatigue_frame_to_dict
from app.services.zone_store import current_odom_epoch_id

router = APIRouter(prefix="/api", tags=["api"])


def _zones_with_frame_validity(request: Request) -> list[dict[str, Any]]:
    """Annotate each zone with whether its WORLD snapshot is currently valid.

    ``world_frame_valid`` is True when the zone's world_epoch stamp matches
    the running session's odometry epoch, False when it provably belongs to
    another session (the client must not render/enforce the snapshot then,
    AGENTS invariant 10), and None for unstamped legacy entries. MAP-frame
    geometry is unaffected; it is only displayable once a live transform
    exists.
    """
    store = request.app.state.zone_store
    current = current_odom_epoch_id(store.path)
    annotated: list[dict[str, Any]] = []
    for zone in store.list():
        stamp = zone.get("world_epoch")
        annotated.append(
            {
                **zone,
                "world_frame_valid": (
                    None if not stamp else bool(current and stamp == current)
                ),
            }
        )
    return annotated

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


def _detect_lan_ip() -> str:
    """Best reachable address for phones on the booth network.

    The UDP-connect trick never sends a packet; it only asks the kernel which
    interface would route to the internet. Loopback is useless on a phone, so
    it is rejected at every step and the mDNS hostname is the last resort.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            candidate = probe.getsockname()[0]
        if candidate and not candidate.startswith("127."):
            return candidate
    except OSError:
        pass
    hostname = socket.gethostname()
    try:
        candidate = socket.gethostbyname(hostname)
        if candidate and not candidate.startswith("127."):
            return candidate
    except OSError:
        pass
    # Airplane-mode fallback: phones on the same LAN can still resolve the
    # Mac's mDNS name, and it is never 127.0.0.1.
    return hostname


def _public_form_url() -> str:
    override = os.environ.get("NIGHTWATCH_PUBLIC_FORM_URL", "").strip()
    if override:
        return override
    return f"http://{_detect_lan_ip()}:3000/form"


_QR_PNG_CACHE: dict[str, bytes] = {}


def _intake_qr_png() -> bytes:
    """PNG bytes for the public form URL, rendered once per resolved URL."""
    url = _public_form_url()
    cached = _QR_PNG_CACHE.get(url)
    if cached is not None:
        return cached
    import qrcode
    from qrcode.image.pure import PyPNGImage

    image = qrcode.make(url, box_size=10, border=4, image_factory=PyPNGImage)
    buffer = io.BytesIO()
    image.save(buffer)
    rendered = buffer.getvalue()
    _QR_PNG_CACHE.clear()
    _QR_PNG_CACHE[url] = rendered
    return rendered


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


class AutoEscortRequest(BaseModel):
    enabled: bool


class ZoneCreateRequest(BaseModel):
    name: str = Field(default="", max_length=48)
    kind: Literal["keep_in", "keep_out", "sleeping"]
    points_frame: Literal["map", "world"] = "map"
    points: list[tuple[float, float]] = Field(min_length=3, max_length=128)
    world_points: list[tuple[float, float]] | None = Field(
        default=None, min_length=3, max_length=128
    )


class ZoneModeRequest(BaseModel):
    mode: Literal["focus", "full", "booth", "demo"]


class ZoneActiveRequest(BaseModel):
    active: bool


@router.get("/zones")
async def get_zones(request: Request) -> dict[str, Any]:
    return {
        "version": 1,
        "venue": request.app.state.settings.venue_name,
        "zones": _zones_with_frame_validity(request),
    }


@router.post("/zones", status_code=201)
async def create_zone(
    request: Request, body: ZoneCreateRequest
) -> dict[str, Any]:
    try:
        zone = request.app.state.zone_store.create(
            name=body.name,
            kind=body.kind,
            points_frame=body.points_frame,
            points=[list(point) for point in body.points],
            world_points=(
                [list(point) for point in body.world_points]
                if body.world_points is not None
                else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "zone": zone}


@router.delete("/zones/{zone_id}")
async def delete_zone(request: Request, zone_id: str) -> dict[str, bool]:
    if not request.app.state.zone_store.delete(zone_id):
        raise HTTPException(status_code=404, detail="zone not found")
    return {"ok": True}


@router.patch("/zones/{zone_id}")
async def set_zone_active(
    request: Request, zone_id: str, body: ZoneActiveRequest
) -> dict[str, Any]:
    zone = request.app.state.zone_store.set_active(zone_id, body.active)
    if zone is None:
        raise HTTPException(status_code=404, detail="zone not found")
    return {"ok": True, "zone": zone}


@router.post("/zones/mode")
async def set_zone_mode(
    request: Request, body: ZoneModeRequest
) -> dict[str, Any]:
    active = body.mode in {"focus", "booth"}
    changed = request.app.state.zone_store.set_keep_in_active(active)
    return {
        "ok": True,
        "mode": "focus" if active else "full",
        "changed": changed,
        "zones": _zones_with_frame_validity(request),
    }


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


@router.get("/intake/qr.png")
async def get_intake_qr() -> Response:
    """QR code pointing phones at the public intake form.

    The URL is static for the lifetime of the process (env override or the
    LAN address detected at first request), so the PNG is cached in module
    state after the first render.
    """
    return Response(
        content=_intake_qr_png(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=60"},
    )


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
            "auto_escort_enabled": False,
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


@router.post("/robot/auto_escort")
async def post_auto_escort(
    request: Request, body: AutoEscortRequest
) -> dict[str, Any]:
    """Operator consent switch for public (unbound) questionnaire responses.

    AGENTS.md invariant 14: an unbound QR/NFC submission never commands the
    robot on its own. Flipping this switch on is the human decision that makes
    the public queue actionable; it is off at every startup, and it is settable
    in every operating mode (it changes server state, not robot motion).
    """
    bridge = getattr(request.app.state, "robot_bridge", None)
    if bridge is None:
        return {
            "ok": False,
            "auto_escort_enabled": False,
            "message": "Robot bridge is unavailable",
        }
    result = await bridge.operator_action(
        "auto_escort_on" if body.enabled else "auto_escort_off"
    )
    return {
        "ok": result.ok,
        "auto_escort_enabled": bool(bridge.auto_escort_enabled()),
        "message": result.text,
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
