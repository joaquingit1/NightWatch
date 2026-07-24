from __future__ import annotations

import asyncio

from app.routers.form import FORM_SCHEMA
from app.services.intake_db import IntakeDatabase, routing_hint
from app.services.robot_bridge import RobotBridge


def _bridge(tmp_path) -> RobotBridge:
    return RobotBridge(
        assessment_path=str(tmp_path / "assessments.jsonl"),
        status_url="http://robot/operator/status",
        assessment_url="",
        mcp_url="http://robot/mcp",
        window_seconds=1.0,
        get_latest_frame=lambda: None,  # type: ignore[arg-type]
        intake_timeout_seconds=1.0,
    )


def test_intake_schema_migration_and_interaction_id_round_trip(tmp_path) -> None:
    path = tmp_path / "intake.sqlite3"
    db = IntakeDatabase(path)
    db.init_schema()

    record = db.insert(
        session_id="session-1234",
        interaction_id="interaction-1234",
        consent_analysis=True,
        tiredness="tired",
        wants_escort=True,
    )

    latest = db.list_latest()[0]
    assert record.interaction_id == "interaction-1234"
    assert latest.interaction_id == "interaction-1234"


def test_only_first_matching_form_binds_active_interaction(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    bridge._interaction_state = "waiting_form"
    from app.services.robot_bridge import InteractionSession
    import time

    bridge._active_interaction = InteractionSession(
        interaction_id="interaction-1234",
        track_id="track-7",
        source="test",
        started_ts=time.time(),
        deadline_ts=time.time() + 30,
    )

    assert bridge.bind_intake_response(
        "interaction-1234", {"response_id": "first"}
    )
    assert not bridge.bind_intake_response(
        "interaction-1234", {"response_id": "second"}
    )
    assert not bridge.bind_intake_response(
        "wrong-interaction", {"response_id": "third"}
    )


def test_escort_routing_uses_ui_design_tiredness_and_request_answers() -> None:
    assert routing_hint(True, True, "tired") == "escort"
    assert routing_hint(True, True, "energized") == "observe"
    assert routing_hint(True, False, "tired") == "observe"
    assert routing_hint(False, True, "tired") == "escort"


def test_form_schema_matches_ui_design_two_question_flow() -> None:
    questions = FORM_SCHEMA["questions"]

    assert [question["id"] for question in questions] == [
        "tiredness",
        "wants_escort",
    ]
    assert questions[1]["prompt_zh"] == "要我带你去休息吗？"
    assert [option["label_en"] for option in questions[1]["options"]] == [
        "Yes",
        "No",
    ]
