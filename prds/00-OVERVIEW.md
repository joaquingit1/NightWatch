# Night Watch — PRD Suite Overview

Read `../PRD.md` first. It is the product authority. These phase documents are
implementation gates, not parallel wish lists.

## Architecture rule

Night Watch is a hierarchical behavior system:

```text
camera/lidar/odom ──► world model + person observations + fatigue assessments
                                │
                                ▼
                    deterministic behavior supervisor
                                │
            ┌───────────────────┼─────────────────────┐
            ▼                   ▼                     ▼
      curiosity lease    intervention lease     explicit-task lease
            └───────────────────┼─────────────────────┘
                                ▼
                  DimensionalOS MovementManager
                                ▼
                              Go2
```

The LLM may request tasks and explain events. It does not decide safety, velocity
priority, liveness, model confidence, or whether an NFC request is fresh.

## Frozen contracts

P0 implements these in `nightwatch/contracts.py`. After P0, changes require agreement
from the robot, model, and product owners.

```python
class BehaviorKind(str, Enum):
    HOLD = "hold"
    EXPLORE = "explore"
    PATROL = "patrol"
    OBSERVE = "observe"
    FOLLOW = "follow"
    INTERVENE = "intervene"
    ESCORT = "escort"
    EXPLICIT_TASK = "explicit_task"
    MANUAL = "manual"

@dataclass(frozen=True)
class BehaviorLease:
    owner: BehaviorKind
    lease_id: str
    priority: int
    issued_at: float
    expires_at: float
    reason: str

@dataclass(frozen=True)
class RobotActivity:
    ts: float
    behavior: BehaviorKind
    moving_expected: bool
    last_motion_ts: float
    hold_reason: str | None
    map_phase: str
    battery_soc: int | None

@dataclass(frozen=True)
class FatigueAssessment:
    assessment_id: str
    ts: float
    track_id: int
    anonymous_person_id: str | None
    bbox: tuple[float, float, float, float]
    fatigue_score: float
    confidence: float
    quality: float
    factors: dict[str, float]
    observation_seconds: float
    model_version: str

@dataclass(frozen=True)
class EscortRequest:
    request_id: str
    robot_id: str
    intervention_id: str
    action: Literal["escort", "remind", "decline"]
    created_at: float
    expires_at: float
    consented_alias: str | None
```

## Ownership

- Robot owner: behavior supervisor, motion leases, mapping, relocalization, escort.
- Model owner: fatigue feature extraction/model and `FatigueAssessment` publisher.
- Product owner: NFC/QR page, request relay, operator UI, consent and person ledger.

Nobody invents a second motion publisher, person schema, or model-output shape.

## Quality gates

Every phase must preserve:

- manual override;
- battery and safety holds;
- latest-only camera behavior;
- a runnable replay/mock path;
- no cloud dependency in physical control;
- no unmeasured product claim.

The phase is complete only when its acceptance tests pass. A screenshot, one successful
manual run, or an LLM saying a tool succeeded is not an acceptance test.

## Cut policy

If time is short, cut from the end:

1. named/face-based recognition;
2. longitudinal presence rules;
3. UI polish and speech variants;
4. model sophistication beyond the stable model-owner output.

Never cut the curiosity supervisor, safety arbitration, NFC consent, or the complete
approach-to-escort-to-curiosity loop.
