from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.contracts import policy_event_to_dict

router = APIRouter(tags=["thoughts"])


async def _event_stream(request: Request) -> AsyncIterator[bytes]:
    policy_source = request.app.state.policy_source
    events = policy_source.subscribe()
    last_index = 0

    while True:
        if await request.is_disconnected():
            break

        while last_index < len(events):
            event = events[last_index]
            last_index += 1
            payload = json.dumps(policy_event_to_dict(event))
            yield f"data: {payload}\n\n".encode()

        await asyncio.sleep(0.25)


@router.get("/text_stream/thoughts")
async def thought_stream(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
