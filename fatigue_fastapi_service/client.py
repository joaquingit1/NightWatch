from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import cv2
import numpy as np
import websockets

from common.visuals import parse_source


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a camera/video stream to the fatigue FastAPI service."
    )
    parser.add_argument(
        "--url", default="ws://127.0.0.1:8000/v1/streams/detect"
    )
    parser.add_argument(
        "--proxy",
        help=(
            "Optional HTTP/SOCKS proxy URL. By default the client connects "
            "directly and ignores proxy environment variables. Use 'auto' "
            "to inherit the system proxy."
        ),
    )
    parser.add_argument(
        "--source",
        default="0",
        help="Camera index, video path, or OpenCV-supported stream URL.",
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--camera-fps", type=float, default=30.0)
    parser.add_argument("--jpeg-quality", type=int, default=85)
    parser.add_argument(
        "--mirror",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--annotated",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Request annotated JPEG frames from the service.",
    )
    parser.add_argument(
        "--no-display",
        action="store_true",
        help="Print JSON only; do not open an OpenCV preview.",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="Stop after N frames. Zero means until the source ends.",
    )
    return parser


def with_query(url: str, **values: bool) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(
        {key: str(value).lower() for key, value in values.items()}
    )
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


async def run(args: argparse.Namespace) -> int:
    source = parse_source(args.source)
    capture = cv2.VideoCapture(source)
    if isinstance(source, int):
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        capture.set(cv2.CAP_PROP_FPS, args.camera_fps)
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open video source {args.source!r}")

    url = with_query(
        args.url, mirror=args.mirror, annotated=args.annotated
    )
    proxy = True if args.proxy == "auto" else args.proxy
    frame_count = 0
    try:
        async with websockets.connect(
            url,
            max_size=16 * 1024 * 1024,
            proxy=proxy,
        ) as websocket:
            ready = json.loads(await websocket.recv())
            if ready.get("type") != "ready":
                raise RuntimeError(f"Server rejected stream: {ready}")
            print(json.dumps(ready, ensure_ascii=False))

            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                encoded_ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality],
                )
                if not encoded_ok:
                    raise RuntimeError("Unable to encode camera frame")

                await websocket.send(encoded.tobytes())
                response = json.loads(await websocket.recv())
                print(json.dumps(response, ensure_ascii=False))
                if response.get("type") == "error":
                    continue

                display_frame = frame
                if response.get("annotated_frame_follows"):
                    annotated_bytes = await websocket.recv()
                    if not isinstance(annotated_bytes, bytes):
                        raise RuntimeError(
                            "Expected an annotated binary JPEG frame"
                        )
                    display_frame = cv2.imdecode(
                        np.frombuffer(annotated_bytes, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    )

                if not args.no_display:
                    cv2.imshow("Fatigue API client", display_frame)
                    if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                        break

                frame_count += 1
                if args.max_frames and frame_count >= args.max_frames:
                    break
    finally:
        capture.release()
        if not args.no_display:
            cv2.destroyAllWindows()
    return 0


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
