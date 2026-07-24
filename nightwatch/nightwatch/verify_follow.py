"""Autonomous follow_person verification.

Polls the dog camera until a person stands prominently in the center of
frame (they are close and deliberately positioned), then drives the full
agentic chain via the web API: follow command -> watch responses -> let it
track ~15 s -> stop following. Saves evidence frames to the given dir.

Usage: python -m nightwatch.verify_follow <evidence_dir> [wait_seconds]
"""

from pathlib import Path
import sys
import time

import requests

BASE = "http://localhost:5555"


def grab_frame(path: Path) -> bytes | None:
    try:
        r = requests.get(f"{BASE}/video_feed/camera", stream=True, timeout=10)
        buf = b""
        for chunk in r.iter_content(8192):
            buf += chunk
            s = buf.find(b"\xff\xd8")
            e = buf.find(b"\xff\xd9", s + 2)
            if s != -1 and e != -1:
                data = buf[s : e + 2]
                path.write_bytes(data)
                r.close()
                return data
        r.close()
    except Exception as exc:
        print(f"[frame error] {exc}", flush=True)
    return None


def person_prominent(jpeg: bytes, detector) -> tuple[bool, str]:
    import cv2
    import numpy as np

    from dimos.msgs.sensor_msgs.Image import Image

    arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return False, "decode failed"
    h, w = arr.shape[:2]
    dets = detector.process_image(Image.from_numpy(arr))
    best = None
    for d in dets.detections:
        x1, y1, x2, y2 = d.bbox
        cx = (x1 + x2) / 2
        height_frac = (y2 - y1) / h
        central = w * 0.25 < cx < w * 0.75
        if central and height_frac > 0.45:
            best = (height_frac, d.track_id)
    if best:
        return True, f"person height {best[0]:.0%} of frame, track {best[1]}"
    return False, f"{len(dets)} detection(s), none central+close"


def send(query: str) -> None:
    requests.post(f"{BASE}/submit_query", data={"query": query}, timeout=10)
    print(f"[sent] {query!r}", flush=True)


def main() -> int:
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    wait_s = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0

    from nightwatch.tracker import YoloFollowTracker

    detector = YoloFollowTracker()._detector
    print("[armed] waiting for a person to stand in front of the dog", flush=True)

    deadline = time.monotonic() + wait_s
    trigger = None
    streak = 0  # consecutive positive scans: filters people walking past
    while time.monotonic() < deadline:
        jpeg = grab_frame(outdir / "latest.jpg")
        if jpeg:
            ok, why = person_prominent(jpeg, detector)
            print(f"[scan] {why} (streak {streak})", flush=True)
            if ok:
                streak += 1
                if streak >= 2:
                    (outdir / "before.jpg").write_bytes(jpeg)
                    trigger = why
                    break
            else:
                streak = 0
        time.sleep(3)

    if not trigger:
        print("[result] TIMEOUT: nobody stood in front of the dog", flush=True)
        return 2

    print(f"[trigger] {trigger}", flush=True)
    send(
        "Immediately call follow_person with query 'person standing in front "
        "of you'. Do NOT call speak or any other tool first."
    )

    for i in range(5):
        time.sleep(4)
        grab_frame(outdir / f"during_{i}.jpg")

    send("stop following now")
    time.sleep(3)
    grab_frame(outdir / "after.jpg")
    print("[result] follow sequence completed; check frames + agent log", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
