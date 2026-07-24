from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, Callable

import anyio
import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .analyzer import FatigueInferenceEngine
from .config import ServiceSettings

EngineFactory = Callable[[Any], Any]


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected a boolean value, got {value!r}")


def decode_image(data: bytes) -> np.ndarray | None:
    encoded = np.frombuffer(data, dtype=np.uint8)
    if encoded.size == 0:
        return None
    return cv2.imdecode(encoded, cv2.IMREAD_COLOR)


def encode_jpeg(frame: np.ndarray, quality: int) -> bytes:
    ok, encoded = cv2.imencode(
        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality]
    )
    if not ok:
        raise ValueError("Unable to encode annotated frame as JPEG")
    return encoded.tobytes()


async def _send_error(
    websocket: WebSocket,
    code: str,
    message: str,
    *,
    sequence: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "type": "error",
        "code": code,
        "message": message,
    }
    if sequence is not None:
        payload["sequence"] = sequence
    await websocket.send_json(payload)


def create_app(
    settings: ServiceSettings | None = None,
    engine_factory: EngineFactory | None = None,
) -> FastAPI:
    resolved_settings = settings or ServiceSettings.from_env()
    resolved_engine_factory = engine_factory or FatigueInferenceEngine

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.engine = await anyio.to_thread.run_sync(
            resolved_engine_factory, resolved_settings.analyzer
        )
        application.state.active_stream = False
        application.state.started_at = time.time()
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(application.state.engine.close)

    application = FastAPI(
        title="Real-time Fatigue Detection API",
        version="1.0.0",
        description=(
            "Send JPEG or PNG frames over WebSocket and receive one temporal "
            "fatigue detection result per frame."
        ),
        lifespan=lifespan,
    )

    @application.get("/")
    async def root() -> dict[str, Any]:
        return {
            "name": "Real-time Fatigue Detection API",
            "version": application.version,
            "health": "/health",
            "websocket": "/v1/streams/detect",
        }

    @application.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "model_loaded": hasattr(application.state, "engine"),
            "active_stream": getattr(
                application.state, "active_stream", False
            ),
            "uptime_seconds": max(
                0.0,
                time.time()
                - getattr(application.state, "started_at", time.time()),
            ),
        }

    @application.websocket("/v1/streams/detect")
    async def detect_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        if application.state.active_stream:
            await _send_error(
                websocket,
                "stream_capacity_reached",
                "This process already has an active detection stream.",
            )
            await websocket.close(code=1013)
            return

        try:
            mirror = _parse_bool(
                websocket.query_params.get("mirror"), default=False
            )
            annotated = _parse_bool(
                websocket.query_params.get("annotated"), default=False
            )
        except ValueError as exc:
            await _send_error(websocket, "invalid_query", str(exc))
            await websocket.close(code=1008)
            return

        application.state.active_stream = True
        session = application.state.engine.new_session()
        session_started = time.perf_counter()
        consecutive_errors = 0

        await websocket.send_json(
            {
                "type": "ready",
                "protocol_version": 1,
                "input": "binary JPEG or PNG frame",
                "output": "one JSON result per input frame",
                "annotated_frame_follows_result": annotated,
                "mirror": mirror,
            }
        )

        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break

                text_message = message.get("text")
                if text_message is not None:
                    try:
                        command = json.loads(text_message)
                    except json.JSONDecodeError:
                        await _send_error(
                            websocket,
                            "invalid_json",
                            "Text messages must contain a JSON command.",
                        )
                        continue
                    command_type = command.get("type")
                    if command_type == "ping":
                        await websocket.send_json(
                            {"type": "pong", "timestamp": time.time()}
                        )
                    elif command_type == "reset":
                        session.reset()
                        session_started = time.perf_counter()
                        await websocket.send_json({"type": "reset"})
                    else:
                        await _send_error(
                            websocket,
                            "unknown_command",
                            "Supported text commands are 'ping' and 'reset'.",
                        )
                    continue

                frame_bytes = message.get("bytes")
                if frame_bytes is None:
                    await _send_error(
                        websocket,
                        "invalid_message",
                        "Send a JPEG/PNG binary frame or a JSON text command.",
                    )
                    continue
                if len(frame_bytes) > resolved_settings.max_frame_bytes:
                    await _send_error(
                        websocket,
                        "frame_too_large",
                        (
                            f"Encoded frame exceeds "
                            f"{resolved_settings.max_frame_bytes} bytes."
                        ),
                        sequence=session.sequence,
                    )
                    continue

                frame = decode_image(frame_bytes)
                if frame is None:
                    consecutive_errors += 1
                    await _send_error(
                        websocket,
                        "invalid_frame",
                        "Binary payload is not a decodable JPEG or PNG image.",
                        sequence=session.sequence,
                    )
                    if consecutive_errors >= 5:
                        await websocket.close(code=1008)
                        break
                    continue
                height, width = frame.shape[:2]
                if width * height > resolved_settings.max_frame_pixels:
                    await _send_error(
                        websocket,
                        "frame_dimensions_too_large",
                        (
                            f"Decoded frame has {width * height} pixels; "
                            f"limit is {resolved_settings.max_frame_pixels}."
                        ),
                        sequence=session.sequence,
                    )
                    continue

                timestamp = time.perf_counter() - session_started
                try:
                    analysis = await anyio.to_thread.run_sync(
                        partial(
                            session.process,
                            frame,
                            timestamp,
                            mirror=mirror,
                            annotate=annotated,
                        )
                    )
                except Exception as exc:
                    consecutive_errors += 1
                    await _send_error(
                        websocket,
                        "inference_failed",
                        str(exc),
                        sequence=session.sequence,
                    )
                    if consecutive_errors >= 5:
                        await websocket.close(code=1011)
                        break
                    continue

                consecutive_errors = 0
                analysis.payload["annotated_frame_follows"] = annotated
                await websocket.send_json(analysis.payload)
                if annotated:
                    annotated_bytes = await anyio.to_thread.run_sync(
                        encode_jpeg,
                        analysis.frame,
                        resolved_settings.annotated_jpeg_quality,
                    )
                    await websocket.send_bytes(annotated_bytes)
        except WebSocketDisconnect:
            pass
        finally:
            application.state.active_stream = False

    return application


app = create_app()
