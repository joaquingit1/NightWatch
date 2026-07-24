from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app.contracts import FatigueFactors, FatigueFrame
from app.services.robot_bridge import (
    RobotBridge,
    SkillResult,
    frame_to_assessment,
)


def _frame(*, score: float = 82.0, confidence: float = 0.8) -> FatigueFrame:
    return FatigueFrame(
        ts=time.time(),
        person_id="track-7",
        bbox=(10, 20, 100, 180),
        score=score,
        confidence=confidence,
        factors=FatigueFactors(
            perclos=0.3,
            blink_ms_p50=250.0,
            blink_ms_p90=520.0,
            nod_count=2,
            yawn_count=1,
            slump_deg=20.0,
            eye_cnn_perclos=-1.0,
            movement_entropy=0.1,
            sedentary_hours=1.0,
        ),
        calib_state="full",
        scorer="test",
    )


def _bridge(tmp_path, **overrides: Any) -> RobotBridge:
    options: dict[str, Any] = {
        "assessment_path": str(tmp_path / "assessments.jsonl"),
        "status_url": "http://robot/operator/status",
        "assessment_url": "http://robot/operator/assessment",
        "mcp_url": "http://robot/mcp",
        "window_seconds": 1.0,
        "get_latest_frame": _frame,
    }
    options.update(overrides)
    return RobotBridge(**options)


def test_frame_to_assessment_matches_robot_contract() -> None:
    assessment = frame_to_assessment(
        _frame(score=82.0, confidence=0.8),
        observation_seconds=12.0,
        track_id="track-7",
    )

    assert assessment.fatigue_score == 0.82
    assert assessment.confidence == 0.8
    assert assessment.quality == 0.8
    assert assessment.bbox == (10.0, 20.0, 100.0, 180.0)
    assert set(assessment.factors) == {
        "perclos",
        "long_blinks",
        "yawning",
        "nodding",
        "slump",
        "stillness",
    }


def test_emit_once_posts_to_robot_and_keeps_jsonl_audit(
    tmp_path, monkeypatch
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
                return {
                    "behavior": "patrol",
                    "owner": "curiosity",
                    "map_phase": "MAPPED",
                    "mission_mode": "cruise",
                    "control_mode": "autonomous",
                }

    monkeypatch.setattr(
        "app.services.robot_bridge.requests.get",
        lambda *_args, **_kwargs: Response(),
    )

    def fake_post(url: str, *, json: dict[str, Any], **_kwargs: Any) -> Response:
        posted.append((url, json))
        return Response()

    monkeypatch.setattr("app.services.robot_bridge.requests.post", fake_post)
    bridge = _bridge(tmp_path, min_observation_seconds=999.0)

    asyncio.run(bridge._emit_once())

    assert posted[0][0] == "http://robot/operator/assessment"
    assert posted[0][1]["track_id"] == "track-7"
    line = (tmp_path / "assessments.jsonl").read_text().strip()
    assert json.loads(line)["fatigue_score"] == 0.82
    assert bridge.snapshot()["connected"] is True


def test_care_sequence_calls_intervention_then_escort(tmp_path) -> None:
    events = []
    calls: list[str] = []
    bridge = _bridge(
        tmp_path,
        publish_event=events.append,
        consecutive_windows=2,
        escort_score=0.75,
        intake_timeout_seconds=1.0,
    )
    now = time.time()
    for offset in (10.0, 15.0):
        bridge._recent.append(
            frame_to_assessment(
                _frame(),
                observation_seconds=offset,
                track_id="track-7",
            )
        )

    async def fake_call(
        name: str, _arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        calls.append(name)
        return SkillResult(True, f"{name} completed")

    bridge._call_skill = fake_call  # type: ignore[method-assign]
    bridge._latest_status = {
        "enabled": True,
        "connected": True,
        "behavior": "patrol",
        "mission_mode": "cruise",
        "control_mode": "autonomous",
        "ts": now,
    }

    async def scenario() -> None:
        task = asyncio.create_task(bridge._run_care_sequence("track-7"))
        for _ in range(100):
            interaction = bridge.active_interaction()
            if interaction is not None:
                break
            await asyncio.sleep(0)
        assert interaction is not None
        assert bridge.bind_intake_response(
            interaction["interaction_id"],
            {
                "response_id": "response-1",
                "consent_analysis": False,
                "tiredness": "tired",
                "wants_escort": True,
            },
        )
        await task

    asyncio.run(scenario())

    assert "potential_detected" in calls
    assert "escort_to_sleeping_area" in calls
    assert calls.index("potential_detected") < calls.index(
        "escort_to_sleeping_area"
    )
    assert [event.state for event in events] == [
        "TRIAGE",
        "APPROACH",
        "PRESCRIBE",
        "ESCORT",
        "NAP_REGISTERED",
    ]


def test_explicit_intake_escort_marks_request_escorted(tmp_path) -> None:
    updates: list[tuple[str, str]] = []
    bridge = _bridge(
        tmp_path,
        update_intake_status=lambda response_id, status: updates.append(
            (response_id, status)
        ),
    )

    async def fake_call(
        name: str, _arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        assert name in {"set_interaction_state", "escort_to_sleeping_area"}
        return SkillResult(True, "arrived")

    bridge._call_skill = fake_call  # type: ignore[method-assign]
    asyncio.run(bridge._run_intake_escort("response-1", "session-1"))

    assert updates == [("response-1", "escorted")]


def test_command_center_posture_actions_are_allow_listed(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    bridge._latest_status = {"connected": True}
    calls: list[str] = []

    async def fake_call(
        name: str, _arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        calls.append(name)
        return SkillResult(True, f"{name} completed")

    bridge._call_skill = fake_call  # type: ignore[method-assign]

    lie = asyncio.run(bridge.operator_action("lie_down"))
    stand = asyncio.run(bridge.operator_action("stand_up"))
    refused = asyncio.run(bridge.operator_action("front_flip"))

    assert lie.ok and stand.ok
    assert calls == ["lie_down_until_resumed", "stand_up_and_resume"]
    assert refused.ok is False


def test_escort_failure_text_is_not_misreported_as_success(
    tmp_path, monkeypatch
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "No sleeping area is known yet; explore or tag "
                                "one first."
                            ),
                        }
                    ]
                },
            }

    monkeypatch.setattr(
        "app.services.robot_bridge.requests.post",
        lambda *_args, **_kwargs: Response(),
    )
    result = _bridge(tmp_path)._call_skill_sync(
        "escort_to_sleeping_area", {}
    )

    assert result.ok is False
