from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

router = APIRouter(tags=["video"])

BOUNDARY = b"--frame\r\n"
STREAM_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "X-Accel-Buffering": "no",
}


async def _mjpeg_stream(request: Request, annotated: bool) -> AsyncIterator[bytes]:
    frame_source = request.app.state.frame_source
    target_fps = 8
    frame_interval = 1.0 / target_fps

    while True:
        if await request.is_disconnected():
            break
        started = time.perf_counter()
        # Decode/draw/JPEG work must not run on FastAPI's event loop. A single
        # slow encode used to stall status, policy, and every other video
        # client together.
        jpeg, capture_ts = await asyncio.to_thread(
            frame_source.get_jpeg_sample,
            annotated,
        )
        frame_headers = (
            f"X-Capture-Timestamp: {capture_ts:.6f}\r\n"
            f"X-Frame-Age-Ms: {max(0.0, (time.time() - capture_ts) * 1000.0):.1f}\r\n"
            if capture_ts is not None
            else ""
        ).encode()
        yield (
            BOUNDARY
            + b"Content-Type: image/jpeg\r\n"
            + frame_headers
            + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
            + jpeg
            + b"\r\n"
        )
        elapsed = time.perf_counter() - started
        await asyncio.sleep(max(0.0, frame_interval - elapsed))


@router.get("/video_feed/pov")
async def video_feed_pov(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _mjpeg_stream(request, annotated=False),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers=STREAM_HEADERS,
    )


@router.get("/video_feed/annotated")
async def video_feed_annotated(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _mjpeg_stream(request, annotated=True),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers=STREAM_HEADERS,
    )


@router.get("/video_feed/robot")
async def video_feed_robot(request: Request) -> StreamingResponse:
    """Byte-for-byte proxy of the robot's first-person MJPEG camera
    (ROBOT_CAMERA_URL, the Go2 operator API). Independent of CAMERA_SOURCE,
    so the booth camera config never affects the /lidar robot-eyes feed.
    A 502 tells the client to retry; the connect must fail fast so the retry
    loop stays responsive."""
    if request.app.state.settings.camera_source == "robot":
        return StreamingResponse(
            _mjpeg_stream(request, annotated=False),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers=STREAM_HEADERS,
        )

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

    return StreamingResponse(
        stream(), media_type=media_type, headers=STREAM_HEADERS
    )
