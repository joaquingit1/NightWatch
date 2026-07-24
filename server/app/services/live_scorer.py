from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import cv2
import numpy as np
import websockets

from app.contracts import FatigueFactors, FatigueFrame
from app.services.scorer import ScoreSource

logger = logging.getLogger("nightwatch.live_scorer")


def _idle_frame(ts: float) -> FatigueFrame:
    """Returned before the first real result, or when no face is detected."""
    return FatigueFrame(
        ts=ts,
        person_id=None,
        bbox=(0, 0, 0, 0),
        score=0.0,
        confidence=0.0,
        factors=FatigueFactors(
            perclos=0.0,
            blink_ms_p50=0.0,
            blink_ms_p90=0.0,
            nod_count=0,
            yawn_count=0,
            slump_deg=0.0,
            eye_cnn_perclos=-1.0,
            movement_entropy=0.0,
            sedentary_hours=0.0,
        ),
        calib_state="uncalibrated",
        scorer="fatigue_fastapi_service",
    )


def _map_result(payload: dict) -> FatigueFrame:
    ts = time.time()
    people = payload.get("people") or []
    if not people:
        return _idle_frame(ts)

    primary = max(
        people,
        key=lambda person: (person.get("state") or {}).get("fatigue_score", 0.0),
    )
    state = primary.get("state") or {}
    bbox = primary.get("bbox") or {}
    calibrating = bool(primary.get("calibrating", False))
    landmarks_detected = bool(primary.get("landmarks_detected", False))
    head_pitch = state.get("head_pitch")

    confidence = float(bbox.get("confidence", 0.0))
    if landmarks_detected:
        confidence = max(confidence, 0.55)

    if not landmarks_detected:
        calib_state = "uncalibrated"
    elif calibrating:
        calib_state = "quick"
    else:
        calib_state = "full"

    slump_deg = 0.0
    if head_pitch is not None and head_pitch < 0:
        slump_deg = abs(float(head_pitch))

    return FatigueFrame(
        ts=ts,
        person_id=f"track-{primary.get('track_id', 0)}",
        bbox=(
            int(bbox.get("x1", 0)),
            int(bbox.get("y1", 0)),
            int(bbox.get("x2", 0)),
            int(bbox.get("y2", 0)),
        ),
        score=float(state.get("fatigue_score", 0.0)),
        confidence=confidence,
        factors=FatigueFactors(
            perclos=float(state.get("perclos", 0.0)),
            blink_ms_p50=float(state.get("blink_duration_ms_p50", 0.0)),
            blink_ms_p90=float(state.get("blink_duration_ms_p90", 0.0)),
            nod_count=int(state.get("nod_count", 0)),
            yawn_count=int(state.get("yawn_count", 0)),
            slump_deg=slump_deg,
            eye_cnn_perclos=-1.0,
            movement_entropy=float(state.get("movement_entropy", 0.0)),
            sedentary_hours=float(state.get("sedentary_hours", 0.0)),
        ),
        calib_state=calib_state,
        scorer="fatigue_fastapi_service",
    )


class LiveScoreSource(ScoreSource):
    """Streams webcam frames to the fatigue_fastapi_service WebSocket and
    exposes the latest real detection result as a FatigueFrame."""

    def __init__(
        self,
        ws_url: str,
        get_frame: Callable[[], np.ndarray | None],
        *,
        reconnect_delay: float = 2.0,
        jpeg_quality: int = 80,
    ) -> None:
        self._ws_url = ws_url
        self._get_frame = get_frame
        self._reconnect_delay = reconnect_delay
        self._jpeg_quality = jpeg_quality
        self._frame = _idle_frame(time.time())
        self._task: asyncio.Task | None = None
        self._stopping = False

    def tick(self) -> None:
        # State is updated asynchronously by the background stream task.
        return None

    def latest(self) -> FatigueFrame:
        return self._frame

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_forever(self) -> None:
        while not self._stopping:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.warning("fatigue service connection lost: %s", exc)
                self._frame = _idle_frame(time.time())
                await asyncio.sleep(self._reconnect_delay)

    async def _connect_and_stream(self) -> None:
        async with websockets.connect(self._ws_url, max_size=16 * 1024 * 1024) as ws:
            await ws.recv()  # discard the initial "ready" message
            logger.info("connected to fatigue service at %s", self._ws_url)
            while not self._stopping:
                frame = self._get_frame()
                if frame is None:
                    await asyncio.sleep(0.05)
                    continue

                ok, buffer = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]
                )
                if not ok:
                    continue

                await ws.send(buffer.tobytes())
                message = await ws.recv()
                if isinstance(message, bytes):
                    # Only happens if annotated=true was requested; we don't.
                    continue

                payload = json.loads(message)
                message_type = payload.get("type")
                if message_type == "result":
                    self._frame = _map_result(payload)
                elif message_type == "error":
                    logger.warning("fatigue service error: %s", payload.get("message"))
