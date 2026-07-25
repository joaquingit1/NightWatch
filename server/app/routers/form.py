from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services.intake_db import routing_hint

router = APIRouter(prefix="/api/form", tags=["form"])

MAX_SUBMISSIONS_PER_HOUR = 10

# Canonical questionnaire: mirrors the Tencent-deployed form exactly. Two
# questions, no consent question; submitting the form IS the consent boundary
# (consent_analysis is recorded server-side on every submission).
FORM_SCHEMA: dict[str, Any] = {
    "questions": [
        {
            "id": "tiredness",
            "prompt_zh": "你现在感觉如何？",
            "prompt_en": "How do you feel right now?",
            "options": [
                {"value": "energized", "label_zh": "还精神", "label_en": "Energized"},
                {"value": "tired", "label_zh": "有点累", "label_en": "Tired"},
            ],
        },
        {
            "id": "wants_escort",
            "prompt_zh": "要我带你去休息吗？",
            "prompt_en": "Would you like me to take you somewhere to rest?",
            "options": [
                {"value": True, "label_zh": "好的，带我去", "label_en": "Yes"},
                {"value": False, "label_zh": "不用了，谢谢", "label_en": "No"},
            ],
        },
    ]
}


class IntakeSubmitRequest(BaseModel):
    submission_id: str | None = Field(default=None, min_length=8, max_length=64)
    session_id: str = Field(min_length=8, max_length=64)
    interaction_id: str | None = Field(default=None, min_length=8, max_length=64)
    interaction_token: str | None = Field(default=None, min_length=16, max_length=128)
    tiredness: Literal["energized", "tired"] = "energized"
    wants_escort: bool = False
    name_alias: str | None = Field(default=None, max_length=32)


class IntakeStatusUpdate(BaseModel):
    status: Literal["acknowledged", "escorted", "declined"]


def _check_operator_key(request: Request) -> None:
    settings = request.app.state.settings
    operator_key = getattr(settings, "intake_operator_key", None)
    if not operator_key:
        return
    provided = request.headers.get("X-Intake-Operator-Key")
    if provided != operator_key:
        raise HTTPException(status_code=403, detail="operator key required")


def _signing_secret(request: Request) -> bytes:
    settings = getattr(request.app.state, "settings", None)
    secret = getattr(settings, "intake_signing_secret", None)
    return str(secret or "nightwatch-local-session").encode("utf-8")


def _interaction_token(
    request: Request,
    *,
    session_id: str,
    interaction_id: str,
    expires_at: float,
) -> str:
    message = f"{session_id}:{interaction_id}:{int(expires_at)}".encode("utf-8")
    return hmac.new(_signing_secret(request), message, hashlib.sha256).hexdigest()


@router.get("/schema")
async def get_form_schema(
    request: Request,
    s: str | None = Query(default=None, alias="s"),
) -> dict[str, Any]:
    bridge = getattr(request.app.state, "robot_bridge", None)
    active = bridge.active_intake() if bridge is not None else None
    active_session = str(active["session_id"]) if active is not None else None
    session_id = s or active_session or uuid.uuid4().hex
    bound = bool(active_session and session_id == active_session)
    interaction_id = str(active["interaction_id"]) if bound else None
    expires_at = float(active["expires_at"]) if bound else None
    return {
        **FORM_SCHEMA,
        "session_id": session_id,
        "interaction_id": interaction_id,
        "interaction_token": (
            _interaction_token(
                request,
                session_id=session_id,
                interaction_id=interaction_id,
                expires_at=expires_at,
            )
            if bound and interaction_id is not None and expires_at is not None
            else None
        ),
        "interaction_active": bound,
        "expires_at": expires_at,
    }


@router.post("/responses")
async def submit_intake(request: Request, body: IntakeSubmitRequest) -> dict[str, Any]:
    db = request.app.state.intake_db
    if body.submission_id is not None:
        prior = db.get(body.submission_id)
        if prior is not None:
            if prior.session_id != body.session_id:
                raise HTTPException(status_code=409, detail="submission id conflict")
            return {
                **prior.to_dict(),
                "routing_hint": routing_hint(
                    prior.consent_analysis,
                    prior.wants_escort,
                ),
                "bound_to_robot": False,
                # A retry must never fire a second escort for the same person.
                "auto_escort_dispatched": False,
                "duplicate": True,
            }
    recent = db.count_recent_submissions(body.session_id)
    if recent >= MAX_SUBMISSIONS_PER_HOUR:
        raise HTTPException(status_code=429, detail="too many submissions for this session")

    # The canonical form carries no consent question: answering the dog's
    # invitation IS the consent boundary, so every submission records
    # consent_analysis=True server-side. Never trust a client-sent value.
    record = db.insert(
        session_id=body.session_id,
        consent_analysis=True,
        tiredness=body.tiredness,
        wants_escort=body.wants_escort,
        name_alias=body.name_alias,
        response_id=body.submission_id,
    )
    if record.session_id != body.session_id:
        raise HTTPException(status_code=409, detail="submission id conflict")
    request.app.state.ledger.register_intake(
        session_id=body.session_id,
        name_alias=body.name_alias,
        tiredness=body.tiredness,
        wants_escort=body.wants_escort,
    )
    hint = routing_hint(record.consent_analysis, body.wants_escort)
    bridge = getattr(request.app.state, "robot_bridge", None)
    active = bridge.active_intake() if bridge is not None else None
    token_valid = False
    if (
        active is not None
        and body.interaction_id == active["interaction_id"]
        and body.session_id == active["session_id"]
        and body.interaction_token is not None
        and float(active["expires_at"]) > time.time()
    ):
        expected = _interaction_token(
            request,
            session_id=body.session_id,
            interaction_id=str(body.interaction_id),
            expires_at=float(active["expires_at"]),
        )
        token_valid = hmac.compare_digest(body.interaction_token, expected)
    bound_to_robot = bool(
        bridge is not None
        and token_valid
        and bridge.accept_intake_response(record.to_dict())
    )
    # AGENTS.md invariant 14: a public response without a signed interaction
    # token stays unbound and must never command the robot by itself. It only
    # becomes actionable while an operator holds the AUTO ESCORT switch on,
    # which is that human consent; the bridge owns the decision (switch state,
    # robot online, nothing else running) and publishes a policy event when it
    # declines, so the response is never queued-and-forgotten. Bound responses
    # keep today's behaviour: the waiting interaction resolves them.
    auto_escort_dispatched = False
    if bridge is not None and body.wants_escort and not token_valid:
        auto_escort_dispatched = bool(
            bridge.request_auto_escort(record.response_id, record.session_id)
        )
    return {
        **record.to_dict(),
        "routing_hint": hint,
        "bound_to_robot": bound_to_robot,
        "auto_escort_dispatched": auto_escort_dispatched,
        "duplicate": False,
    }


@router.get("/responses/latest")
async def list_latest_responses(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    records = request.app.state.intake_db.list_latest(limit=limit)
    return {"responses": [record.to_dict() for record in records]}


@router.get("/responses/pending-escort")
async def list_pending_escort(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    records = request.app.state.intake_db.list_pending_escort(limit=limit)
    return {"responses": [record.to_dict() for record in records]}


@router.patch("/responses/{response_id}")
async def update_response_status(
    response_id: str,
    body: IntakeStatusUpdate,
    request: Request,
) -> dict[str, Any]:
    _check_operator_key(request)
    updated = request.app.state.intake_db.update_status(response_id, body.status)
    if updated is None:
        raise HTTPException(status_code=404, detail="response not found")
    robot_dispatched = False
    bridge = getattr(request.app.state, "robot_bridge", None)
    if (
        body.status == "acknowledged"
        and updated.wants_escort
    ):
        if bridge is not None:
            robot_dispatched = bridge.request_intake_escort(
                updated.response_id,
                updated.session_id,
            )
        if not robot_dispatched:
            request.app.state.intake_db.update_status(response_id, "pending")
            raise HTTPException(
                status_code=409,
                detail="robot is offline, held, or already handling another request",
            )
    return {**updated.to_dict(), "robot_dispatched": robot_dispatched}
