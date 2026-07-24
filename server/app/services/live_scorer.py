from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import replace

import cv2
import numpy as np
import websockets

from app.contracts import FatigueFactors, FatigueFrame
from app.services.scorer import ScoreSource

logger = logging.getLogger("nightwatch.live_scorer")


def _idle_frame(ts: float, *, source_status: str = "connecting") -> FatigueFrame:
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
        source_status=source_status,
    )


def _map_person(
    person: dict,
    *,
    ts: float,
    processing_ms: float,
    sequence: int,
    model_version: str,
    frame_width: int,
    frame_height: int,
) -> FatigueFrame:
    state = person.get("state") or {}
    bbox = person.get("bbox") or {}
    calibrating = bool(person.get("calibrating", False))
    landmarks_detected = bool(person.get("landmarks_detected", False))
    head_pitch = state.get("head_pitch")
    attention = person.get("attention") or {}

    confidence = min(1.0, max(0.0, float(bbox.get("confidence", 0.0))))
    x1 = int(bbox.get("x1", 0))
    y1 = int(bbox.get("y1", 0))
    x2 = int(bbox.get("x2", 0))
    y2 = int(bbox.get("y2", 0))
    face_area_ratio = (
        max(0, x2 - x1)
        * max(0, y2 - y1)
        / max(1, frame_width * frame_height)
    )
    quality = person.get("quality")
    if quality is None:
        size_quality = min(1.0, (face_area_ratio / 0.02) ** 0.5)
        quality = confidence * size_quality * (1.0 if landmarks_detected else 0.25)
    quality = min(1.0, max(0.0, float(quality)))

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
        person_id=f"track-{person.get('track_id', 0)}",
        bbox=(x1, y1, x2, y2),
        score=float(state.get("fatigue_score", 0.0)),
        confidence=confidence,
        quality=quality,
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
        status=str(person.get("status", "CALIBRATING")),
        calibration_progress=min(
            1.0, max(0.0, float(person.get("calibration_progress", 0.0)))
        ),
        landmarks_detected=landmarks_detected,
        model_version=model_version,
        processing_ms=processing_ms,
        sequence=sequence,
        source_status="live",
        looking_at_camera=bool(person.get("looking_at_camera", False)),
        head_pitch=(
            float(attention["head_pitch"])
            if attention.get("head_pitch") is not None
            else None
        ),
        head_yaw=(
            float(attention["head_yaw"])
            if attention.get("head_yaw") is not None
            else None
        ),
        gaze_horizontal=(
            float(attention["gaze_horizontal"])
            if attention.get("gaze_horizontal") is not None
            else None
        ),
        gaze_vertical=(
            float(attention["gaze_vertical"])
            if attention.get("gaze_vertical") is not None
            else None
        ),
    )


def _map_result(payload: dict) -> FatigueFrame:
    ts = time.time()
    people = payload.get("people") or []
    processing_ms = float(payload.get("processing_ms", 0.0))
    sequence = int(payload.get("sequence", -1))
    model = payload.get("model") or {}
    model_version = str(model.get("version", "fatigue-yolov8face-mediapipe-v1"))
    frame_info = payload.get("frame") or {}
    frame_width = int(frame_info.get("width", 0))
    frame_height = int(frame_info.get("height", 0))
    if not people:
        frame = _idle_frame(ts, source_status="live")
        frame.processing_ms = processing_ms
        frame.sequence = sequence
        frame.model_version = model_version
        return frame

    tracked = [
        _map_person(
            person,
            ts=ts,
            processing_ms=processing_ms,
            sequence=sequence,
            model_version=model_version,
            frame_width=frame_width,
            frame_height=frame_height,
        )
        for person in people
    ]
    # The large score card follows the strongest *usable* signal, while every
    # stable model track remains present in `people` and on the video overlay.
    primary = max(
        tracked,
        key=lambda frame: (
            frame.quality >= 0.55 and frame.calib_state == "full",
            frame.score * max(frame.quality, 0.05),
            frame.confidence,
        ),
    )
    # Return a copy for the summary card. Reusing the selected object here
    # would put that object inside its own `people` list and make dataclass
    # serialization recurse forever as soon as a face is detected.
    return replace(primary, people=tracked)


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
        self._enabled = True
        self._pause_reason: str | None = None

    def tick(self) -> None:
        # State is updated asynchronously by the background stream task.
        return None

    def latest(self) -> FatigueFrame:
        return self._frame

    def set_enabled(self, enabled: bool, reason: str | None = None) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled and (enabled or reason == self._pause_reason):
            return
        self._enabled = enabled
        self._pause_reason = None if enabled else (reason or "policy_paused")
        if not enabled:
            self._frame = _idle_frame(
                time.time(), source_status=f"paused:{self._pause_reason}"
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pause_reason(self) -> str | None:
        return self._pause_reason

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
                self._frame = _idle_frame(
                    time.time(), source_status="model_offline"
                )
                await asyncio.sleep(self._reconnect_delay)

    async def _connect_and_stream(self) -> None:
        async with websockets.connect(self._ws_url, max_size=16 * 1024 * 1024) as ws:
            await ws.recv()  # discard the initial "ready" message
            logger.info("connected to fatigue service at %s", self._ws_url)
            while not self._stopping:
                if not self._enabled:
                    await asyncio.sleep(0.1)
                    continue
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
