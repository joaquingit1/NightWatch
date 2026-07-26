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

FrameSample = np.ndarray | tuple[np.ndarray, float | None]


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
        frame_width=frame_width,
        frame_height=frame_height,
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
        get_frame: Callable[[], FrameSample | None],
        *,
        should_analyze: Callable[[], bool] | None = None,
        reconnect_delay: float = 2.0,
        max_reconnect_delay: float = 15.0,
        connect_timeout: float = 5.0,
        response_timeout: float = 10.0,
        jpeg_quality: int = 80,
        request_annotated_frames: bool = False,
    ) -> None:
        self._ws_url = ws_url
        self._get_frame = get_frame
        self._should_analyze = should_analyze or (lambda: True)
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max(reconnect_delay, max_reconnect_delay)
        self._connect_timeout = connect_timeout
        self._response_timeout = response_timeout
        self._backoff = reconnect_delay
        self._jpeg_quality = jpeg_quality
        self._request_annotated_frames = request_annotated_frames
        self._frame = _idle_frame(time.time())
        # Exact model-rendered frame paired with the result that produced it.
        # Assignment of this immutable tuple is atomic under CPython, so the
        # synchronous MJPEG worker can read it without holding up inference.
        self._annotated_sample: tuple[bytes, float, int] | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    def tick(self) -> None:
        # State is updated asynchronously by the background stream task.
        return None

    def latest(self) -> FatigueFrame:
        return self._frame

    def latest_annotated_sample(self) -> tuple[bytes, float] | None:
        sample = self._annotated_sample
        if sample is None:
            return None
        jpeg, capture_ts, _sequence = sample
        return jpeg, capture_ts

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
        self._backoff = self._reconnect_delay
        while not self._stopping:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.warning("fatigue service connection lost: %s", exc)
                # source_status must reflect reality: a hung or dead model is
                # reported as offline instead of freezing the last score.
                self._frame = _idle_frame(
                    time.time(), source_status="model_offline"
                )
                self._annotated_sample = None
                delay = self._backoff
                self._backoff = min(self._backoff * 2, self._max_reconnect_delay)
                await asyncio.sleep(delay)

    async def _connect_and_stream(self) -> None:
        # Every await on the socket is bounded. Without timeouts, a stalled
        # model service left ws.recv() pending forever and the published frame
        # kept a stale ts and source_status until restart.
        ws_url = self._ws_url
        if self._request_annotated_frames:
            separator = "&" if "?" in ws_url else "?"
            ws_url = f"{ws_url}{separator}annotated=true"
        async with websockets.connect(
            ws_url,
            max_size=16 * 1024 * 1024,
            open_timeout=self._connect_timeout,
        ) as ws:
            # The service is single-stream: instead of "ready" it may answer
            # {"type": "error", "code": "stream_capacity_reached"} and close.
            # Surface that reason instead of silently retrying forever.
            greeting_raw = await asyncio.wait_for(
                ws.recv(), timeout=self._response_timeout
            )
            greeting: dict = {}
            if isinstance(greeting_raw, (str, bytes, bytearray)):
                try:
                    parsed = json.loads(greeting_raw)
                    if isinstance(parsed, dict):
                        greeting = parsed
                except ValueError:
                    pass
            if greeting.get("type") != "ready":
                raise RuntimeError(
                    "fatigue service refused the stream: "
                    f"type={greeting.get('type')!r} "
                    f"code={greeting.get('code')!r} "
                    f"message={greeting.get('message')!r}"
                )
            logger.info("connected to fatigue service at %s", self._ws_url)
            self._backoff = self._reconnect_delay
            while not self._stopping:
                if not self._should_analyze():
                    # Refresh ts on every pass so standby is visibly alive
                    # rather than a snapshot frozen at the moment the gate
                    # closed.
                    self._frame = _idle_frame(
                        time.time(), source_status="standby"
                    )
                    self._annotated_sample = None
                    await asyncio.sleep(0.1)
                    continue
                sample = self._get_frame()
                if sample is None:
                    await asyncio.sleep(0.05)
                    continue
                if (
                    isinstance(sample, tuple)
                    and len(sample) == 2
                    and isinstance(sample[0], np.ndarray)
                ):
                    frame = sample[0]
                    capture_ts = (
                        float(sample[1])
                        if isinstance(sample[1], (int, float))
                        else time.time()
                    )
                else:
                    frame = sample
                    capture_ts = time.time()

                ok, buffer = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]
                )
                if not ok:
                    continue

                await asyncio.wait_for(
                    ws.send(buffer.tobytes()), timeout=self._response_timeout
                )
                message = await asyncio.wait_for(
                    ws.recv(), timeout=self._response_timeout
                )
                if isinstance(message, bytes):
                    # Annotated mode is JSON-result-then-JPEG. A binary frame
                    # here is out of sequence, so ignore it and resynchronize
                    # on the next request instead of parsing bytes as JSON.
                    continue

                payload = json.loads(message)
                message_type = payload.get("type")
                if message_type == "result":
                    self._frame = _map_result(payload)
                    if (
                        self._request_annotated_frames
                        and payload.get("annotated_frame_follows")
                    ):
                        annotated = await asyncio.wait_for(
                            ws.recv(), timeout=self._response_timeout
                        )
                        if not isinstance(annotated, bytes):
                            raise RuntimeError(
                                "fatigue service returned a non-binary "
                                "annotated frame"
                            )
                        self._annotated_sample = (
                            annotated,
                            capture_ts,
                            int(payload.get("sequence", -1)),
                        )
                elif message_type == "error":
                    logger.warning("fatigue service error: %s", payload.get("message"))
