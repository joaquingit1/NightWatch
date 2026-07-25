from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.routers.form import IntakeSubmitRequest, get_form_schema, submit_intake
from app.services.intake_db import IntakeDatabase
from app.services.robot_bridge import RobotBridge, SkillResult


class Bridge:
    def __init__(self, *, auto_escort_enabled: bool = False) -> None:
        self.active = {
            "interaction_id": "interaction-1",
            "session_id": "session-active-123",
            "track_id": "track-7",
            "opened_at": 1.0,
            "expires_at": 9999999999.0,
        }
        self.accepted: list[dict] = []
        self.auto_escort_enabled = auto_escort_enabled
        # Every ask, dispatched or not: proves the router consulted the bridge
        # (which owns the switch state) instead of deciding on its own.
        self.auto_escort_asks: list[tuple[str, str]] = []
        self.auto_escort_dispatches: list[tuple[str, str]] = []

    def active_intake(self):
        return self.active

    def accept_intake_response(self, response: dict) -> bool:
        if response["session_id"] != self.active["session_id"]:
            return False
        self.accepted.append(response)
        return len(self.accepted) == 1

    def request_auto_escort(self, response_id: str, track_id: str) -> bool:
        self.auto_escort_asks.append((response_id, track_id))
        if not self.auto_escort_enabled:
            return False
        self.auto_escort_dispatches.append((response_id, track_id))
        return True


class Ledger:
    def register_intake(self, **_kwargs) -> None:
        return None


def _request(tmp_path, bridge: Bridge | None):
    db = IntakeDatabase(tmp_path / "intake.sqlite3")
    db.init_schema()
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                robot_bridge=bridge,
                intake_db=db,
                ledger=Ledger(),
            )
        )
    )


def test_schema_is_the_canonical_two_question_form(tmp_path) -> None:
    bridge = Bridge()
    schema = asyncio.run(get_form_schema(_request(tmp_path, bridge), s=None))
    assert [question["id"] for question in schema["questions"]] == [
        "tiredness",
        "wants_escort",
    ]
    escort = schema["questions"][1]
    assert escort["prompt_zh"] == "要我带你去休息吗？"
    assert escort["prompt_en"] == "Would you like me to take you somewhere to rest?"
    assert escort["options"] == [
        {"value": True, "label_zh": "好的，带我去", "label_en": "Yes"},
        {"value": False, "label_zh": "不用了，谢谢", "label_en": "No"},
    ]


def test_submission_records_consent_implicitly_and_ignores_client_value(
    tmp_path,
) -> None:
    # The canonical form has no consent question: engaging with the invitation
    # form is the consent boundary, and the server never trusts a client-sent
    # consent flag.
    bridge = Bridge()
    request = _request(tmp_path, bridge)
    payload = IntakeSubmitRequest.model_validate(
        {
            "session_id": "session-active-123",
            "tiredness": "tired",
            "wants_escort": True,
            "consent_analysis": False,  # extra field: ignored, not trusted
        }
    )
    result = asyncio.run(submit_intake(request, payload))
    assert result["consent_analysis"] is True
    assert result["routing_hint"] == "escort"
    stored = request.app.state.intake_db.list_latest()[0]
    assert stored.consent_analysis is True


def test_schema_without_query_binds_to_the_active_robot_visitor(tmp_path) -> None:
    bridge = Bridge()
    schema = asyncio.run(get_form_schema(_request(tmp_path, bridge), s=None))
    assert schema["session_id"] == "session-active-123"
    assert schema["interaction_id"] == "interaction-1"
    assert schema["interaction_active"] is True


def test_stale_qr_session_is_visible_but_cannot_command_robot(tmp_path) -> None:
    bridge = Bridge()
    schema = asyncio.run(
        get_form_schema(_request(tmp_path, bridge), s="stale-session-123")
    )
    assert schema["session_id"] == "stale-session-123"
    assert schema["interaction_id"] is None
    assert schema["interaction_active"] is False


def test_first_bound_submission_resolves_robot_session(tmp_path) -> None:
    bridge = Bridge()
    request = _request(tmp_path, bridge)
    schema = asyncio.run(get_form_schema(request, s=None))
    payload = IntakeSubmitRequest(
        session_id="session-active-123",
        interaction_id=schema["interaction_id"],
        interaction_token=schema["interaction_token"],
        tiredness="tired",
        wants_escort=True,
    )
    first = asyncio.run(submit_intake(request, payload))
    second = asyncio.run(submit_intake(request, payload))

    assert first["bound_to_robot"] is True
    assert second["bound_to_robot"] is False
    assert len(bridge.accepted) == 2


def test_forged_interaction_token_cannot_command_robot(tmp_path) -> None:
    bridge = Bridge()
    request = _request(tmp_path, bridge)
    payload = IntakeSubmitRequest(
        session_id="session-active-123",
        interaction_id="interaction-1",
        interaction_token="0" * 64,
        tiredness="tired",
        wants_escort=True,
    )
    result = asyncio.run(submit_intake(request, payload))
    assert result["bound_to_robot"] is False
    assert bridge.accepted == []


def test_unbound_escort_request_does_nothing_while_auto_escort_is_off(
    tmp_path,
) -> None:
    # AGENTS.md invariant 14 default: no signed token, no operator switch, so
    # the public response only lands in the queue.
    bridge = Bridge()
    request = _request(tmp_path, bridge)
    payload = IntakeSubmitRequest(
        session_id="public-session-999",
        tiredness="tired",
        wants_escort=True,
    )

    result = asyncio.run(submit_intake(request, payload))

    assert result["bound_to_robot"] is False
    assert result["auto_escort_dispatched"] is False
    assert bridge.auto_escort_dispatches == []
    stored = request.app.state.intake_db.list_pending_escort()
    assert [record.response_id for record in stored] == [result["response_id"]]
    assert stored[0].status == "pending"


def test_unbound_escort_request_dispatches_while_auto_escort_is_on(
    tmp_path,
) -> None:
    bridge = Bridge(auto_escort_enabled=True)
    request = _request(tmp_path, bridge)
    payload = IntakeSubmitRequest(
        session_id="public-session-999",
        tiredness="tired",
        wants_escort=True,
    )

    result = asyncio.run(submit_intake(request, payload))

    assert result["auto_escort_dispatched"] is True
    assert bridge.auto_escort_dispatches == [
        (result["response_id"], "public-session-999")
    ]


def test_auto_escort_ignores_submissions_that_decline_the_escort(
    tmp_path,
) -> None:
    bridge = Bridge(auto_escort_enabled=True)
    request = _request(tmp_path, bridge)
    payload = IntakeSubmitRequest(
        session_id="public-session-999",
        tiredness="tired",
        wants_escort=False,
    )

    result = asyncio.run(submit_intake(request, payload))

    assert result["auto_escort_dispatched"] is False
    assert bridge.auto_escort_asks == []


def test_bound_submission_keeps_the_interaction_path_without_auto_escort(
    tmp_path,
) -> None:
    bridge = Bridge(auto_escort_enabled=True)
    request = _request(tmp_path, bridge)
    schema = asyncio.run(get_form_schema(request, s=None))
    payload = IntakeSubmitRequest(
        session_id="session-active-123",
        interaction_id=schema["interaction_id"],
        interaction_token=schema["interaction_token"],
        tiredness="tired",
        wants_escort=True,
    )

    result = asyncio.run(submit_intake(request, payload))

    assert result["bound_to_robot"] is True
    assert result["auto_escort_dispatched"] is False
    # The waiting interaction resolves a bound response; the switch must not
    # add a second escort on top of it.
    assert bridge.auto_escort_asks == []


def test_armed_public_submission_escorts_and_marks_the_record_escorted(
    tmp_path,
) -> None:
    # End-to-end wiring over the real bridge: submission -> speech -> escort ->
    # record status, with only the MCP skill call faked.
    request = _request(tmp_path, None)
    db = request.app.state.intake_db
    bridge = RobotBridge(
        assessment_path="",
        status_url="",
        assessment_url="",
        mcp_url="",
        window_seconds=1.0,
        get_latest_frame=lambda: None,  # type: ignore[arg-type,return-value]
        update_intake_status=db.update_status,
    )
    request.app.state.robot_bridge = bridge
    bridge._latest_status = {"connected": True, "operating_mode": "autonomous"}
    bridge.set_auto_escort(True)
    calls: list[str] = []

    async def fake_call(name, _arguments=None):
        calls.append(name)
        if name == "escort_to_sleeping_area":
            return SkillResult(
                True,
                "Escort complete: arrived at sleeping area 'sleep' "
                "(session) after 3.2 m in 12.0s.",
            )
        return SkillResult(True, f"{name} completed")

    bridge._call_skill = fake_call  # type: ignore[method-assign]
    payload = IntakeSubmitRequest(
        session_id="public-session-999",
        tiredness="tired",
        wants_escort=True,
    )

    async def exercise() -> dict:
        result = await submit_intake(request, payload)
        assert bridge._auto_escort_task is not None
        await bridge._auto_escort_task
        return result

    result = asyncio.run(exercise())

    assert result["auto_escort_dispatched"] is True
    assert calls == ["speak", "escort_to_sleeping_area"]
    assert db.get(result["response_id"]).status == "escorted"
    assert db.list_pending_escort() == []


def test_submission_retry_is_idempotent(tmp_path) -> None:
    bridge = Bridge()
    request = _request(tmp_path, bridge)
    schema = asyncio.run(get_form_schema(request, s=None))
    payload = IntakeSubmitRequest(
        submission_id="retry-safe-response-1",
        session_id="session-active-123",
        interaction_id=schema["interaction_id"],
        interaction_token=schema["interaction_token"],
        tiredness="tired",
        wants_escort=True,
    )

    first = asyncio.run(submit_intake(request, payload))
    retry = asyncio.run(submit_intake(request, payload))

    assert retry["response_id"] == first["response_id"]
    assert retry["duplicate"] is True
    assert len(request.app.state.intake_db.list_latest()) == 1
