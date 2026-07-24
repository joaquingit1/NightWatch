"""Runtime contracts shared by Nightwatch behavior modules.

These are deliberately small, immutable values.  The curiosity supervisor is
the single writer of RobotActivity; higher-level fatigue and escort modules can
consume the same stream without inferring robot state from logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BehaviorKind(str, Enum):
    HOLD = "hold"
    EXPLORE = "explore"
    PATROL = "patrol"
    RETURN_HOME = "return_home"
    OBSERVE = "observe"
    FOLLOW = "follow"
    EXPRESS = "express"
    INTERVENE = "intervene"
    ESCORT = "escort"
    EXPLICIT_TASK = "explicit_task"
    MANUAL = "manual"


class MissionMode(str, Enum):
    """Operator-selected product mission, independent of map maturity."""

    EXPLORATION = "exploration"
    CRUISE = "cruise"


class ControlMode(str, Enum):
    """Who currently owns ordinary motion."""

    AUTONOMOUS = "autonomous"
    MANUAL = "manual"


class InteractionState(str, Enum):
    """Deterministic care-loop phase surfaced to the operator."""

    IDLE = "idle"
    SCHEDULED_SCAN = "scheduled_scan"
    FORCED_SCAN = "forced_scan"
    FATIGUE_CANDIDATE = "fatigue_candidate"
    APPROACHING = "approaching"
    OFFERING_FORM = "offering_form"
    WAITING_FORM = "waiting_form"
    FAREWELL = "farewell"
    ESCORTING = "escorting"
    ARRIVED = "arrived"
    RETREATING = "retreating"


@dataclass(frozen=True, slots=True)
class BehaviorLease:
    owner: str
    lease_id: str
    behavior: BehaviorKind
    priority: int
    issued_at: float
    expires_at: float
    reason: str


@dataclass(frozen=True, slots=True)
class RobotActivity:
    ts: float
    behavior: BehaviorKind
    owner: str
    moving_expected: bool
    last_motion_ts: float | None
    hold_reason: str | None
    map_phase: str
    battery_soc: int | None
    mission_mode: MissionMode = MissionMode.EXPLORATION
    control_mode: ControlMode = ControlMode.AUTONOMOUS
    interaction_state: InteractionState = InteractionState.IDLE


@dataclass(frozen=True, slots=True)
class FatigueAssessment:
    """Frozen model-integration contract (PRD section 3.1).

    The model owner controls feature extraction and scoring; the robot policy
    controls when an assessment is actionable. One frame is never sufficient
    for an intervention. ``factors`` is a model explanation, not a diagnosis.
    """

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


@dataclass(frozen=True, slots=True)
class EscortRequest:
    request_id: str
    created_at: float
    source: str
    destination: str
    expires_at: float
    person_id: str | None = None
