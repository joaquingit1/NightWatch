from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import numpy as np

FSM_STATES: tuple[str, ...] = (
    "IDLE",
    "PATROL",
    "TRIAGE",
    "APPROACH",
    "DIAGNOSE",
    "PRESCRIBE",
    "ESCORT",
    "NAP_REGISTERED",
    "PASS_CHECK",
    "DETER",
    "WAKE_LADDER",
    "CELEBRATE",
    "RESET",
    "ESTOP",
)


@dataclass
class FeatureVector:
    ts: float
    ear_l: float
    ear_r: float
    ear_corrected: float
    eye_cnn_p: float
    mar: float
    head_pitch: float
    head_yaw: float
    head_roll: float
    pitch_vel: float
    slump_deg: float
    movement: float
    quality: float


@dataclass
class FatigueFactors:
    perclos: float
    blink_ms_p50: float
    blink_ms_p90: float
    nod_count: int
    yawn_count: int
    slump_deg: float
    eye_cnn_perclos: float
    movement_entropy: float
    sedentary_hours: float


@dataclass
class FatigueFrame:
    ts: float
    person_id: str | None
    bbox: tuple[int, int, int, int]
    score: float
    confidence: float
    factors: FatigueFactors
    calib_state: str
    scorer: str
    quality: float = 0.0
    status: str = "NO_FACE"
    calibration_progress: float = 0.0
    landmarks_detected: bool = False
    model_version: str = "unknown"
    processing_ms: float = 0.0
    sequence: int = -1
    source_status: str = "connecting"
    looking_at_camera: bool = False
    head_pitch: float | None = None
    head_yaw: float | None = None
    gaze_horizontal: float | None = None
    gaze_vertical: float | None = None
    people: list[FatigueFrame] = field(default_factory=list)


@dataclass
class PolicyEvent:
    ts: float
    state: str
    target_person: str | None
    utterance: str | None
    detail: str


class RobotFacade(Protocol):
    def navigate_to(self, tag: str) -> bool: ...
    def approach(self, person_id: str) -> bool: ...
    def sport(self, cmd: str) -> bool: ...
    def say(self, clip_id: str) -> bool: ...
    def observe(self) -> np.ndarray | None: ...
    def stop(self) -> None: ...
    def status(self) -> dict[str, Any]: ...


@dataclass
class CaptureRecord:
    session_id: str
    person_id: str
    ts_start: float
    fps: float
    features: list[FeatureVector] = field(default_factory=list)
    kss: int = 5
    hours_awake: float = 0.0
    glasses: bool = False
    lighting: str = "normal"
    consent_raw_video: bool = False


@dataclass(frozen=True)
class FatigueAssessment:
    """Handoff contract for the robot policy bridge (bin/NightWatch 守夜犬！.md)."""

    assessment_id: str
    ts: float
    track_id: str
    anonymous_person_id: str | None
    bbox: tuple[float, float, float, float] | None
    fatigue_score: float
    confidence: float
    quality: float
    factors: tuple[str, ...]
    observation_seconds: float
    model_version: str


def fatigue_frame_to_dict(frame: FatigueFrame) -> dict[str, Any]:
    data = asdict(frame)
    return data


def policy_event_to_dict(event: PolicyEvent) -> dict[str, Any]:
    return asdict(event)


def fatigue_assessment_to_dict(assessment: FatigueAssessment) -> dict[str, Any]:
    data = asdict(assessment)
    data["factors"] = list(assessment.factors)
    if assessment.bbox is not None:
        data["bbox"] = list(assessment.bbox)
    return data
