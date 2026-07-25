from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import time
from typing import Any

import pytest

from app.contracts import FatigueFactors, FatigueFrame
from app.services.robot_bridge import (
    AUTO_ESCORT_CONFIRMATION,
    ActiveIntake,
    RobotBridge,
    SkillResult,
    frame_to_assessment,
)

_ARRIVED = (
    "Escort complete: arrived at sleeping area 'sleep' "
    "(session) after 3.2 m in 12.0s."
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
        frame_width=960,
        frame_height=540,
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


def test_non_sleep_modes_do_not_accumulate_or_publish_fatigue(
    tmp_path, monkeypatch
) -> None:
    calls: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "behavior": "explore",
                "owner": "curiosity",
                "operating_mode": "autonomous",
            }

    monkeypatch.setattr(
        "app.services.robot_bridge.requests.get",
        lambda *_args, **_kwargs: Response(),
    )
    monkeypatch.setattr(
        "app.services.robot_bridge.requests.post",
        lambda *_args, **_kwargs: calls.append("post") or Response(),
    )
    bridge = _bridge(
        tmp_path,
        get_latest_frame=lambda: calls.append("frame") or _frame(),
    )
    bridge._streaks["old-track"] = 2

    asyncio.run(bridge._emit_once())

    assert calls == []
    assert dict(bridge._streaks) == {}
    assert not (tmp_path / "assessments.jsonl").exists()
    assert bridge.snapshot()["enabled"] is False


def test_sleep_patrol_does_not_replay_the_previous_scan_frame(
    tmp_path, monkeypatch
) -> None:
    calls: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "behavior": "patrol",
                "owner": "curiosity",
                "operating_mode": "sleep_analysis",
                "scan_active": False,
            }

    monkeypatch.setattr(
        "app.services.robot_bridge.requests.get",
        lambda *_args, **_kwargs: Response(),
    )
    bridge = _bridge(
        tmp_path,
        get_latest_frame=lambda: calls.append("frame") or _frame(),
    )
    asyncio.run(bridge._emit_once())
    assert calls == []
    assert not (tmp_path / "assessments.jsonl").exists()


def test_sleep_scan_hold_allows_intervention(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    bridge._latest_status = {
        "connected": True,
        "operating_mode": "sleep_analysis",
        "behavior": "observe",
        "hold_reason": "SLEEP_SCAN",
    }
    assert bridge._robot_accepts_intervention() is True


def test_face_track_is_bound_to_robot_confirmed_anonymous_person(
    tmp_path, monkeypatch
) -> None:
    frame = replace(_frame(), looking_at_camera=True)

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "behavior": "patrol",
                "owner": "face_observation",
                "map_phase": "MAPPED",
                "face_observation_subject": "person:person_confirmed",
                "face_observation_bbox": [0.0, 0.0, 0.25, 0.6],
            }

    monkeypatch.setattr(
        "app.services.robot_bridge.requests.get",
        lambda *_args, **_kwargs: Response(),
    )
    monkeypatch.setattr(
        "app.services.robot_bridge.requests.post",
        lambda *_args, **_kwargs: Response(),
    )
    bridge = _bridge(
        tmp_path,
        get_latest_frame=lambda: frame,
        min_observation_seconds=999.0,
    )

    asyncio.run(bridge._emit_once())

    payload = json.loads((tmp_path / "assessments.jsonl").read_text())
    assert payload["track_id"] == "person_confirmed"
    assert payload["anonymous_person_id"] == "person_confirmed"


def test_normalized_body_box_rejects_a_different_face() -> None:
    assert RobotBridge._face_belongs_to_observed_body(
        (10, 20, 100, 180),
        [0.0, 0.0, 0.25, 0.6],
        frame_width=960,
        frame_height=540,
    )
    assert not RobotBridge._face_belongs_to_observed_body(
        (700, 20, 820, 180),
        [0.0, 0.0, 0.25, 0.6],
        frame_width=960,
        frame_height=540,
    )


def test_fatigue_candidate_requires_direct_attention(tmp_path) -> None:
    bridge = _bridge(tmp_path, consecutive_windows=2)
    assessment = frame_to_assessment(
        _frame(), observation_seconds=12.0, track_id="person_confirmed"
    )

    bridge._evaluate_candidate(assessment, direct_attention=False)
    assert bridge._streaks["person_confirmed"] == 0

    bridge._evaluate_candidate(assessment, direct_attention=True)
    assert bridge._streaks["person_confirmed"] == 1


def test_care_sequence_calls_intervention_then_escort(tmp_path) -> None:
    events = []
    calls: list[str] = []
    bridge = _bridge(
        tmp_path,
        publish_event=events.append,
        consecutive_windows=2,
        escort_score=0.75,
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
        if name == "potential_detected":
            for offset in (20.0, 21.0):
                fresh = frame_to_assessment(
                    _frame(),
                    observation_seconds=offset,
                    track_id="track-7",
                )
                bridge._recent.append(
                    replace(fresh, ts=time.time() + offset / 1000.0)
                )
        if name == "escort_to_sleeping_area":
            # Speak the skill's real success contract: NAP_REGISTERED requires
            # a proven arrival, not merely a successful MCP call.
            return SkillResult(
                True,
                "Escort complete: arrived at sleeping area 'sleep' "
                "(session) after 3.2 m in 12.0s.",
            )
        return SkillResult(True, f"{name} completed")

    bridge._call_skill = fake_call  # type: ignore[method-assign]
    async def accepted_intake(_track_id: str) -> dict[str, Any]:
        return {
            "session_id": "bound-session",
            "consent_analysis": True,
            "wants_escort": True,
        }

    bridge._wait_for_intake = accepted_intake  # type: ignore[method-assign]
    bridge._latest_status = {
        "enabled": True,
        "connected": True,
        "behavior": "patrol",
        "ts": now,
    }

    asyncio.run(bridge._run_care_sequence("track-7"))

    assert calls == ["potential_detected", "escort_to_sleeping_area"]
    assert [event.state for event in events] == [
        "TRIAGE",
        "APPROACH",
        "DIAGNOSE",
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
        assert name == "escort_to_sleeping_area"
        return SkillResult(
            True,
            "Escort complete: arrived at sleeping area 'sleep' "
            "(session) after 3.2 m in 12.0s.",
        )

    bridge._call_skill = fake_call  # type: ignore[method-assign]
    asyncio.run(bridge._run_intake_escort("response-1", "session-1"))

    assert updates == [("response-1", "escorted")]


def test_escort_without_arrival_never_registers_nap(tmp_path) -> None:
    # The skill call succeeds but the escort timed out mid-route. The intake
    # must stay pending and no NAP_REGISTERED event may be emitted.
    events = []
    updates: list[tuple[str, str]] = []
    bridge = _bridge(
        tmp_path,
        publish_event=events.append,
        update_intake_status=lambda response_id, status: updates.append(
            (response_id, status)
        ),
    )

    async def fake_call(
        name: str, _arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        assert name == "escort_to_sleeping_area"
        return SkillResult(
            True,
            "Escort ended without reaching sleeping area 'sleep' "
            "(session) after 3 goal attempt(s), 1.1 m, 120.0s.",
        )

    bridge._call_skill = fake_call  # type: ignore[method-assign]
    asyncio.run(bridge._run_intake_escort("response-1", "track-1"))

    assert updates == [("response-1", "pending")]
    states = [event.state for event in events]
    assert "NAP_REGISTERED" not in states
    assert states[-1] == "RESET"


def test_bound_intake_accepts_only_first_response_for_active_session(
    tmp_path,
) -> None:
    bridge = _bridge(tmp_path, intake_timeout_seconds=5.0)

    async def fake_call(
        _name: str, _arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        return SkillResult(True, "spoken")

    bridge._call_skill = fake_call  # type: ignore[method-assign]

    async def exercise() -> dict[str, Any] | None:
        task = asyncio.create_task(bridge._wait_for_intake("track-7"))
        await asyncio.sleep(0)
        active = bridge.active_intake()
        assert active is not None
        accepted = {
            "session_id": active["session_id"],
            "consent_analysis": True,
            "wants_escort": False,
        }
        assert bridge.accept_intake_response(accepted) is True
        assert bridge.accept_intake_response(accepted) is False
        return await task

    result = asyncio.run(exercise())
    assert result is not None
    assert result["wants_escort"] is False
    assert bridge.active_intake() is None


def test_declined_intake_farewell_never_starts_escort(tmp_path) -> None:
    events = []
    calls: list[str] = []
    bridge = _bridge(tmp_path, publish_event=events.append)

    async def fake_call(
        name: str, _arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        calls.append(name)
        if name == "potential_detected":
            for offset in (1.0, 2.0):
                fresh = frame_to_assessment(
                    _frame(),
                    observation_seconds=10.0,
                    track_id="track-7",
                )
                bridge._recent.append(
                    replace(fresh, ts=time.time() + offset / 1000.0)
                )
        return SkillResult(True, "ok")

    async def declined(_track_id: str) -> dict[str, Any]:
        return {
            "session_id": "bound",
            "consent_analysis": True,
            "wants_escort": False,
        }

    bridge._call_skill = fake_call  # type: ignore[method-assign]
    bridge._wait_for_intake = declined  # type: ignore[method-assign]
    asyncio.run(bridge._run_care_sequence("track-7"))

    assert "escort_to_sleeping_area" not in calls
    assert calls == [
        "potential_detected",
        "speak",
        "perform_dog_expression",
    ]
    assert events[-1].state == "RESET"


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


def test_start_manual_intake_refuses_while_a_session_is_active(
    tmp_path,
) -> None:
    bridge = _bridge(tmp_path)
    bridge._active_intake = ActiveIntake(
        interaction_id="interaction-1",
        session_id="session-1",
        track_id="track-7",
        opened_at=time.time(),
        expires_at=time.time() + 60.0,
    )

    result = bridge.start_manual_intake()

    assert result.ok is False
    assert "already active" in result.text
    assert bridge._policy_task is None


def test_start_manual_intake_runs_once_and_guards_concurrent_triggers(
    tmp_path,
) -> None:
    bridge = _bridge(tmp_path)
    runs: list[str] = []

    async def exercise() -> tuple[SkillResult, SkillResult]:
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(track_id: str) -> None:
            runs.append(track_id)
            started.set()
            await release.wait()

        bridge._run_manual_intake = fake_run  # type: ignore[method-assign]
        first = bridge.start_manual_intake()
        await started.wait()
        second = bridge.start_manual_intake()
        release.set()
        assert bridge._policy_task is not None
        await bridge._policy_task
        return first, second

    first, second = asyncio.run(exercise())

    assert first.ok is True
    assert second.ok is False
    assert "in progress" in second.text
    assert runs == ["operator-manual"]


def test_manual_intake_decline_says_farewell_without_escort(tmp_path) -> None:
    events = []
    calls: list[str] = []
    bridge = _bridge(tmp_path, publish_event=events.append)

    async def fake_call(
        name: str, _arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        calls.append(name)
        return SkillResult(True, f"{name} completed")

    async def declined(_track_id: str) -> dict[str, Any]:
        return {
            "session_id": "bound",
            "consent_analysis": True,
            "wants_escort": False,
        }

    bridge._call_skill = fake_call  # type: ignore[method-assign]
    bridge._wait_for_intake = declined  # type: ignore[method-assign]

    asyncio.run(bridge._run_manual_intake("operator-manual"))

    assert "escort_to_sleeping_area" not in calls
    assert calls == ["speak", "perform_dog_expression"]
    assert [event.state for event in events] == ["PRESCRIBE", "RESET"]


def test_auto_escort_is_off_until_an_operator_arms_it(tmp_path) -> None:
    # Fail-safe default (AGENTS.md invariant 14): an unbound public response
    # cannot command the robot until a human flips the switch.
    events: list[Any] = []
    bridge = _bridge(tmp_path, publish_event=events.append)
    bridge._latest_status = {"connected": True}

    assert bridge.snapshot()["auto_escort_enabled"] is False
    assert bridge.auto_escort_enabled() is False
    assert bridge.request_auto_escort("response-1", "session-1") is False
    assert bridge._auto_escort_task is None
    assert events == []


def test_auto_escort_toggle_round_trips_even_while_robot_is_offline(
    tmp_path,
) -> None:
    bridge = _bridge(tmp_path)
    bridge._latest_status = {"connected": False}
    calls: list[str] = []

    async def fake_call(
        name: str, _arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        calls.append(name)
        return SkillResult(True, f"{name} completed")

    bridge._call_skill = fake_call  # type: ignore[method-assign]

    on = asyncio.run(bridge.operator_action("auto_escort_on"))
    assert on.ok is True
    assert "Auto-escort ON" in on.text
    assert bridge.snapshot()["auto_escort_enabled"] is True

    off = asyncio.run(bridge.operator_action("auto_escort_off"))
    assert off.ok is True
    assert "Auto-escort OFF" in off.text
    assert bridge.snapshot()["auto_escort_enabled"] is False

    # Pure server state: no robot skill is involved, so an offline dog never
    # blocks the operator from arming or disarming the queue.
    assert calls == []


def test_auto_escort_speaks_first_then_escorts_and_marks_escorted(
    tmp_path,
) -> None:
    events = []
    calls: list[tuple[str, dict[str, Any] | None]] = []
    updates: list[tuple[str, str]] = []
    bridge = _bridge(
        tmp_path,
        publish_event=events.append,
        update_intake_status=lambda response_id, status: updates.append(
            (response_id, status)
        ),
    )
    bridge._latest_status = {"connected": True, "operating_mode": "autonomous"}
    bridge.set_auto_escort(True)

    async def fake_call(
        name: str, arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        calls.append((name, arguments))
        if name == "escort_to_sleeping_area":
            return SkillResult(True, _ARRIVED)
        return SkillResult(True, f"{name} completed")

    bridge._call_skill = fake_call  # type: ignore[method-assign]

    async def exercise() -> bool:
        started = bridge.request_auto_escort("response-1", "session-1")
        assert bridge._auto_escort_task is not None
        await bridge._auto_escort_task
        return started

    assert asyncio.run(exercise()) is True
    assert [name for name, _ in calls] == ["speak", "escort_to_sleeping_area"]
    assert calls[0][1] == {"text": AUTO_ESCORT_CONFIRMATION}
    assert updates == [("response-1", "escorted")]
    assert [event.state for event in events] == ["ESCORT", "NAP_REGISTERED"]


def test_auto_escort_without_arrival_leaves_the_response_pending(
    tmp_path,
) -> None:
    events = []
    updates: list[tuple[str, str]] = []
    bridge = _bridge(
        tmp_path,
        publish_event=events.append,
        update_intake_status=lambda response_id, status: updates.append(
            (response_id, status)
        ),
    )
    bridge._latest_status = {"connected": True}
    bridge.set_auto_escort(True)

    async def fake_call(
        name: str, _arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        if name == "escort_to_sleeping_area":
            return SkillResult(
                True,
                "Escort ended without reaching sleeping area 'sleep' "
                "(session) after 3 goal attempt(s), 1.1 m, 120.0s.",
            )
        return SkillResult(True, f"{name} completed")

    bridge._call_skill = fake_call  # type: ignore[method-assign]

    async def exercise() -> None:
        assert bridge.request_auto_escort("response-1", "session-1") is True
        assert bridge._auto_escort_task is not None
        await bridge._auto_escort_task

    asyncio.run(exercise())

    assert updates == [("response-1", "pending")]
    states = [event.state for event in events]
    assert "NAP_REGISTERED" not in states
    assert states[-1] == "RESET"


def test_auto_escort_is_skipped_and_explained_while_the_robot_is_busy(
    tmp_path,
) -> None:
    events = []
    updates: list[tuple[str, str]] = []
    bridge = _bridge(
        tmp_path,
        publish_event=events.append,
        update_intake_status=lambda response_id, status: updates.append(
            (response_id, status)
        ),
    )
    bridge._latest_status = {"connected": True}
    bridge.set_auto_escort(True)

    async def exercise() -> bool:
        release = asyncio.Event()

        async def busy() -> None:
            await release.wait()

        bridge._policy_task = asyncio.create_task(busy())
        await asyncio.sleep(0)
        started = bridge.request_auto_escort("response-1", "session-1")
        release.set()
        await bridge._policy_task
        return started

    assert asyncio.run(exercise()) is False
    # Never queued-and-forgotten: the operator sees why, and the record keeps
    # its pending status for manual dispatch.
    assert [event.state for event in events] == ["RESET"]
    assert "Auto-escort skipped: robot busy" in events[0].detail
    assert "机器人正忙" in events[0].detail
    assert updates == []


def test_auto_escort_is_skipped_while_an_intake_session_is_active(
    tmp_path,
) -> None:
    events = []
    bridge = _bridge(tmp_path, publish_event=events.append)
    bridge._latest_status = {"connected": True}
    bridge.set_auto_escort(True)
    bridge._active_intake = ActiveIntake(
        interaction_id="interaction-1",
        session_id="session-1",
        track_id="track-7",
        opened_at=time.time(),
        expires_at=time.time() + 60.0,
    )

    assert bridge.request_auto_escort("response-1", "session-2") is False
    assert bridge._auto_escort_task is None
    assert [event.state for event in events] == ["RESET"]
    assert "intake session is active" in events[0].detail


def test_auto_escort_is_skipped_with_an_event_when_the_robot_is_offline(
    tmp_path,
) -> None:
    events = []
    bridge = _bridge(tmp_path, publish_event=events.append)
    bridge._latest_status = {"connected": False}
    bridge.set_auto_escort(True)

    assert bridge.request_auto_escort("response-1", "session-1") is False
    assert bridge._auto_escort_task is None
    assert [event.state for event in events] == ["RESET"]
    assert "Auto-escort skipped: robot offline" in events[0].detail


def test_auto_escort_defers_to_a_latched_manual_override(tmp_path) -> None:
    events = []
    bridge = _bridge(tmp_path, publish_event=events.append)
    bridge._latest_status = {"connected": True, "operating_mode": "manual"}
    bridge.set_auto_escort(True)

    assert bridge.request_auto_escort("response-1", "session-1") is False
    assert bridge._auto_escort_task is None
    assert "manual override is latched" in events[0].detail


def test_autonomous_mode_keeps_the_auto_escort_but_manual_preempts_it(
    tmp_path, monkeypatch
) -> None:
    # The poll loop cancels the fatigue policy task outside Sleep Analysis; the
    # operator-armed escort must survive that in Autonomous (the mode the
    # switch exists for) and stop for Manual Override (invariant 2).
    events = []
    mode = {"operating_mode": "autonomous"}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"behavior": "explore", "owner": "curiosity", **mode}

    monkeypatch.setattr(
        "app.services.robot_bridge.requests.get",
        lambda *_args, **_kwargs: Response(),
    )
    bridge = _bridge(tmp_path, publish_event=events.append)
    bridge._latest_status = {"connected": True, **mode}
    bridge.set_auto_escort(True)

    async def exercise() -> None:
        release = asyncio.Event()

        async def fake_call(
            _name: str, _arguments: dict[str, Any] | None = None
        ) -> SkillResult:
            await release.wait()
            return SkillResult(True, _ARRIVED)

        bridge._call_skill = fake_call  # type: ignore[method-assign]
        assert bridge.request_auto_escort("response-1", "session-1") is True
        task = bridge._auto_escort_task
        assert task is not None

        await bridge._emit_once()
        assert not task.done()
        assert bridge.snapshot()["policy_active"] is True

        mode["operating_mode"] = "manual"
        await bridge._emit_once()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert [event.state for event in events] == ["ESCORT", "RESET"]
    assert "Auto-escort cancelled" in events[-1].detail


def test_auto_escort_blocks_a_second_interaction_from_starting(tmp_path) -> None:
    # Invariant 13/1: no fatigue care sequence or manual intake may start while
    # the operator-armed escort owns the route.
    bridge = _bridge(tmp_path)
    bridge._latest_status = {"connected": True}
    bridge.set_auto_escort(True)

    async def exercise() -> None:
        release = asyncio.Event()

        async def fake_call(
            _name: str, _arguments: dict[str, Any] | None = None
        ) -> SkillResult:
            await release.wait()
            return SkillResult(True, _ARRIVED)

        bridge._call_skill = fake_call  # type: ignore[method-assign]
        assert bridge.request_auto_escort("response-1", "session-1") is True
        await asyncio.sleep(0)

        assert bridge.start_manual_intake().ok is False
        assert bridge.request_intake_escort("response-2", "session-2") is False
        assert bridge.request_auto_escort("response-3", "session-3") is False
        release.set()
        assert bridge._auto_escort_task is not None
        await bridge._auto_escort_task

    asyncio.run(exercise())


def test_auto_escort_endpoint_round_trips_through_robot_status(tmp_path) -> None:
    from types import SimpleNamespace

    from app.routers.api import (
        AutoEscortRequest,
        get_robot_status,
        post_auto_escort,
    )

    bridge = _bridge(tmp_path)
    bridge._latest_status = {"connected": True}
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(robot_bridge=bridge))
    )

    assert asyncio.run(get_robot_status(request))["auto_escort_enabled"] is False

    armed = asyncio.run(
        post_auto_escort(request, AutoEscortRequest(enabled=True))
    )
    assert armed["ok"] is True
    assert armed["auto_escort_enabled"] is True
    assert asyncio.run(get_robot_status(request))["auto_escort_enabled"] is True

    disarmed = asyncio.run(
        post_auto_escort(request, AutoEscortRequest(enabled=False))
    )
    assert disarmed["auto_escort_enabled"] is False
    assert asyncio.run(get_robot_status(request))["auto_escort_enabled"] is False


def test_robot_status_without_a_bridge_reports_auto_escort_off() -> None:
    from types import SimpleNamespace

    from app.routers.api import get_robot_status

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    assert asyncio.run(get_robot_status(request))["auto_escort_enabled"] is False


def test_manual_intake_escort_stays_silent_before_moving(tmp_path) -> None:
    # The operator/bound path already spoke to the visitor, so extending
    # _run_intake_escort with the auto-escort pre-speech must not add a line
    # there.
    calls: list[str] = []
    bridge = _bridge(tmp_path)

    async def fake_call(
        name: str, _arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        calls.append(name)
        return SkillResult(True, _ARRIVED)

    bridge._call_skill = fake_call  # type: ignore[method-assign]
    asyncio.run(bridge._run_intake_escort("response-1", "session-1"))

    assert calls == ["escort_to_sleeping_area"]


def test_intake_wait_refusal_text_reads_as_failure(
    tmp_path, monkeypatch
) -> None:
    # Regression: unknown robot-side refusals used to parse as success and
    # block the single policy task for the full 180 s window.
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
                                "Intake wait refused outside Sleep Analysis "
                                "mode."
                            ),
                        }
                    ]
                },
            }

    monkeypatch.setattr(
        "app.services.robot_bridge.requests.post",
        lambda *_args, **_kwargs: Response(),
    )

    result = _bridge(tmp_path)._call_skill_sync("begin_intake_wait", {})

    assert result.ok is False


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
