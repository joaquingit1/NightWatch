from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.contracts import policy_event_to_dict

router = APIRouter(tags=["thoughts"])


async def _event_stream(request: Request) -> AsyncIterator[bytes]:
    policy_source = request.app.state.policy_source
    events = policy_source.subscribe()
    last_ts = 0.0

    while True:
        if await request.is_disconnected():
            break

        # The source is a bounded deque. Index cursors stop forever once it
        # reaches maxlen because each append also removes index zero and the
        # length no longer grows. Timestamp cursors continue across rollover.
        pending = [event for event in list(events) if event.ts > last_ts]
        for event in pending:
            payload = json.dumps(policy_event_to_dict(event))
            yield f"data: {payload}\n\n".encode()
            last_ts = max(last_ts, event.ts)

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
