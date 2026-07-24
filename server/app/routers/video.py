from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator

import cv2
import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

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


@router.get("/video_feed/robot")
async def video_feed_robot(request: Request) -> StreamingResponse:
    """Byte-for-byte proxy of the robot's first-person MJPEG camera
    (ROBOT_CAMERA_URL, the Go2 operator API). Independent of CAMERA_SOURCE,
    so the booth camera config never affects the /lidar robot-eyes feed.
    A 502 tells the client to retry; the connect must fail fast so the retry
    loop stays responsive."""
    url = request.app.state.settings.robot_camera_url
    try:
        upstream = await run_in_threadpool(
            lambda: requests.get(url, stream=True, timeout=(3.05, 10))
        )
        upstream.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="robot camera unavailable") from exc

    media_type = upstream.headers.get(
        "content-type", "multipart/x-mixed-replace; boundary=frame"
    )

    def stream() -> Iterator[bytes]:
        try:
            yield from upstream.iter_content(chunk_size=16384)
        except requests.RequestException:
            pass  # upstream died mid-stream; ending the response triggers a client retry
        finally:
            upstream.close()

    return StreamingResponse(stream(), media_type=media_type)
