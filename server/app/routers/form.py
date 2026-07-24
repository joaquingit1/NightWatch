from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services.intake_db import IntakeStatus, routing_hint

router = APIRouter(prefix="/api/form", tags=["form"])

MAX_SUBMISSIONS_PER_HOUR = 10

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
    session_id: str = Field(min_length=8, max_length=64)
    consent_analysis: bool
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


@router.get("/schema")
async def get_form_schema(
    s: str | None = Query(default=None, alias="s"),
) -> dict[str, Any]:
    session_id = s or uuid.uuid4().hex
    return {**FORM_SCHEMA, "session_id": session_id}


@router.post("/responses")
async def submit_intake(request: Request, body: IntakeSubmitRequest) -> dict[str, Any]:
    db = request.app.state.intake_db
    recent = db.count_recent_submissions(body.session_id)
    if recent >= MAX_SUBMISSIONS_PER_HOUR:
        raise HTTPException(status_code=429, detail="too many submissions for this session")

    record = db.insert(
        session_id=body.session_id,
        consent_analysis=body.consent_analysis,
        tiredness=body.tiredness,
        wants_escort=body.wants_escort,
        name_alias=body.name_alias,
    )
    request.app.state.ledger.register_intake(
        session_id=body.session_id,
        name_alias=body.name_alias,
        tiredness=body.tiredness,
        wants_escort=body.wants_escort,
    )
    hint = routing_hint(body.consent_analysis, body.wants_escort)
    return {
        **record.to_dict(),
        "routing_hint": hint,
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
    return updated.to_dict()
