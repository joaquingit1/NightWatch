"""WebSocket relay for the nightwatch live-map stream.

Browsers connect to /ws/lidar on this server; each connection opens its own
upstream connection to the nightwatch MapStreamer (LIDAR_BRIDGE_WS_URL) and
relays frames verbatim: binary point-cloud snapshots and JSON pose messages.
The upstream replays its latest snapshot on connect, so a fresh browser gets
a full picture immediately. While the upstream is unreachable, the relay
emits JSON status frames so the UI can show "waiting for robot".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

router = APIRouter()
logger = logging.getLogger(__name__)

RECONNECT_INITIAL_S = 1.0
RECONNECT_MAX_S = 10.0
UPSTREAM_OPEN_TIMEOUT_S = 5.0


async def _watch_browser(websocket: WebSocket) -> None:
    """Consume inbound browser frames so a client disconnect is noticed
    even while the relay is idle waiting on a silent upstream."""
    while True:
        await websocket.receive()


async def _relay_upstream(websocket: WebSocket, url: str) -> None:
    backoff = RECONNECT_INITIAL_S
    while True:
        try:
            async with connect(
                url,
                # Cloud snapshots run ~1.8 MB; the client default of 1 MiB
                # would silently drop them.
                max_size=None,
                open_timeout=UPSTREAM_OPEN_TIMEOUT_S,
            ) as upstream:
                backoff = RECONNECT_INITIAL_S
                await websocket.send_text(
                    json.dumps({"type": "status", "connected": True})
                )
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)
        except (OSError, WebSocketException, asyncio.TimeoutError):
            await websocket.send_text(
                json.dumps(
                    {"type": "status", "connected": False, "retry_s": backoff}
                )
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)


@router.websocket("/ws/lidar")
async def lidar_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    url = websocket.app.state.settings.lidar_bridge_ws_url

    relay = asyncio.create_task(_relay_upstream(websocket, url))
    watcher = asyncio.create_task(_watch_browser(websocket))
    try:
        done, pending = await asyncio.wait(
            {relay, watcher}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(
                exc, (WebSocketDisconnect, RuntimeError)
            ):
                logger.warning("lidar relay ended: %r", exc)
    finally:
        for task in (relay, watcher):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
