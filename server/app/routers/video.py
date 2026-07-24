from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

import cv2
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["video"])

BOUNDARY = b"--frame\r\n"


def _encode_jpeg(frame) -> bytes:
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        raise RuntimeError("failed to encode jpeg frame")
    return buffer.tobytes()


async def _mjpeg_stream(request: Request, annotated: bool) -> AsyncIterator[bytes]:
    frame_source = request.app.state.frame_source
    target_fps = 10
    frame_interval = 1.0 / target_fps

    while True:
        if await request.is_disconnected():
            break
        started = time.perf_counter()
        frame = (
            frame_source.get_annotated_frame()
            if annotated
            else frame_source.get_pov_frame()
        )
        jpeg = _encode_jpeg(frame)
        yield BOUNDARY + b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        elapsed = time.perf_counter() - started
        await asyncio.sleep(max(0.0, frame_interval - elapsed))


@router.get("/video_feed/pov")
async def video_feed_pov(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _mjpeg_stream(request, annotated=False),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/video_feed/annotated")
async def video_feed_annotated(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _mjpeg_stream(request, annotated=True),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
