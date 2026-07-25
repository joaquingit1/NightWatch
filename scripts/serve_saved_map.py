"""Offline saved-map streamer: /lidar shows the last full premap without the robot.

The /lidar page renders whatever the nightwatch MapStreamer publishes on
ws://127.0.0.1:8010/ws/map, so when the robot is off the page is empty even
though a complete venue premap sits on disk. This server speaks the exact same
wire protocol (see nightwatch/nightwatch/map_stream.py and
scripts/fake_map_stream.py) and streams the canonical saved premap:

- binary CLOUD frame ("NWPC"): the saved map as the primary visible cloud;
- binary PREMAP frame ("NWPM") + {"type": "premap", ...} status: the same
  points on the reference layer, with the last persisted world_to_map
  alignment so operator zones anchor exactly where the robot last had them.

Points are stored in the persistent MAP frame; when
maps/nightwatch_relocalization.json holds an accepted alignment they are
projected into that session's WORLD frame (the same math MapStreamer uses),
so zones drawn against this view line up with the robot's next session if it
restores the same alignment.

run_scout.sh stops this server automatically before starting the real
hardware streamer (same handover as fake_map_stream.py).

Run with the dimos venv (needs dimos for the .pc2.lcm decode):
    dimos/.venv/bin/python scripts/serve_saved_map.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import struct
import sys
import time

import numpy as np
import websockets.asyncio.server as ws_server

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "dimos"))

from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2  # noqa: E402

CLOUD_MAGIC = 0x4E575043  # "NWPC" accumulated global map
PREMAP_MAGIC = 0x4E57504D  # "NWPM" saved premap
HOST = "127.0.0.1"
PORT = 8010
MAX_POINTS = 200_000
RESEND_S = 3.0

MAPS_DIR = os.path.join(PROJECT_ROOT, "dimos", "assets", "output", "maps")
PREMAP_PATH = os.environ.get(
    "NIGHTWATCH_SAVED_PREMAP",
    os.path.join(MAPS_DIR, "export", "nightwatch_map.pc2.lcm"),
)
RELOC_PATH = os.path.join(MAPS_DIR, "nightwatch_relocalization.json")


def load_points() -> tuple[np.ndarray, str]:
    with open(PREMAP_PATH, "rb") as handle:
        encoded = handle.read()
    cloud = PointCloud2.lcm_decode(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    points = np.asarray(cloud.points_f32(), dtype=np.float64)
    if len(points) > MAX_POINTS:
        stride = -(-len(points) // MAX_POINTS)
        points = points[::stride]
    return points, digest


def load_alignment(premap_digest: str) -> dict | None:
    """Load the persisted world->map alignment, but only for THIS premap.

    The alignment file records the sha256 of the map it was accepted against.
    Serving it with a different premap (a re-export, another venue's map)
    would compute wrong MAP coordinates for every zone drawn on the offline
    view, so a digest mismatch degrades to the unaligned reference frame.
    """
    try:
        with open(RELOC_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
        saved_digest = payload.get("map_sha256")
        if saved_digest != premap_digest:
            print(
                "ignoring persisted alignment: it belongs to a different "
                f"premap (saved sha={str(saved_digest)[:12]}, "
                f"serving sha={premap_digest[:12]})"
            )
            return None
        transform = payload["world_to_map"]
        return {
            "x": float(transform["x"]),
            "y": float(transform["y"]),
            "yaw": float(transform["yaw"]),
        }
    except Exception:
        return None


def to_world(points: np.ndarray, transform: dict) -> np.ndarray:
    cos_yaw = math.cos(transform["yaw"])
    sin_yaw = math.sin(transform["yaw"])
    shifted = points[:, :2] - np.array([transform["x"], transform["y"]])
    world = points.copy()
    world[:, 0] = cos_yaw * shifted[:, 0] + sin_yaw * shifted[:, 1]
    world[:, 1] = -sin_yaw * shifted[:, 0] + cos_yaw * shifted[:, 1]
    return world


def frame(magic: int, seq: int, points: np.ndarray) -> bytes:
    header = struct.pack("<IIdI", magic, seq, time.time(), len(points))
    return header + np.ascontiguousarray(points, dtype="<f4").tobytes()


async def main() -> None:
    points, premap_digest = load_points()
    transform = load_alignment(premap_digest)
    if transform is not None:
        points = to_world(points, transform)
    status: dict = {"type": "premap", "aligned": transform is not None}
    if transform is not None:
        status["world_to_map"] = transform
    status_json = json.dumps(status)
    print(
        f"serving saved premap: {len(points)} points from {PREMAP_PATH} "
        f"(aligned={transform is not None})"
    )

    clients: set = set()
    seq = 0

    async def handler(websocket) -> None:
        clients.add(websocket)
        try:
            websocket_seq = seq or 1
            await websocket.send(frame(CLOUD_MAGIC, websocket_seq, points))
            await websocket.send(frame(PREMAP_MAGIC, websocket_seq, points))
            await websocket.send(status_json)
            async for _ in websocket:
                pass
        finally:
            clients.discard(websocket)

    async with ws_server.serve(handler, HOST, PORT, max_size=None, compression=None):
        while True:
            await asyncio.sleep(RESEND_S)
            seq += 1
            ws_server.broadcast(clients, frame(CLOUD_MAGIC, seq, points))
            ws_server.broadcast(clients, frame(PREMAP_MAGIC, seq, points))
            ws_server.broadcast(clients, status_json)


if __name__ == "__main__":
    asyncio.run(main())
