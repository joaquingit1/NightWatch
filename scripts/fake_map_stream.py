"""Fake nightwatch MapStreamer for developing the /lidar viewer without the robot.

Serves the exact wire protocol of nightwatch.map_stream.MapStreamer on
ws://127.0.0.1:8010/ws/map:

Binary frames (little-endian), same 20-byte header, distinguished by magic:
    offset 0   uint32   magic: 0x4E575043 ("NWPC") accumulated global map
                               0x4E57534E ("NWSN") live lidar scan
    offset 4   uint32   seq (per-stream counter)
    offset 8   float64  timestamp_s
    offset 16  uint32   point_count N
    offset 20  float32  x, y, z interleaved (N * 3 floats, Z-up world frame)

The map streams at 1 Hz; the scan (points within ~5 m of the robot, jittered
per frame) streams at 5 Hz.

Text frame (JSON): {"type": "pose", "x", "y", "z", "yaw", "t"} at 10 Hz.

New clients immediately receive the cached latest map, scan, and pose.
The synthetic room grows over ~90 s and spins slowly so cloud updates are
obvious in the viewer.

Run with the dimos venv (has websockets >= 13 and numpy):
    dimos/.venv/bin/python scripts/fake_map_stream.py
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
import time

import numpy as np
import websockets
import websockets.asyncio.server as ws_server

CLOUD_MAGIC = 0x4E575043
SCAN_MAGIC = 0x4E57534E
SCAN_RADIUS = 5.0
HOST = "127.0.0.1"
PORT = 8010
WS_PATH = "/ws/map"

ROOM_W = 12.0  # meters, along x
ROOM_D = 9.0  # meters, along y
WALL_H = 2.5
REVEAL_SECONDS = 90.0
SPIN_RAD_S = 0.05
POSE_RADIUS = 3.0
POSE_RAD_S = 0.3


def build_scene(rng: np.random.Generator) -> np.ndarray:
    """Static synthetic room, centered on the origin: floor, 4 walls, columns."""
    parts: list[np.ndarray] = []

    n_floor = 60_000
    fx = rng.uniform(-ROOM_W / 2, ROOM_W / 2, n_floor)
    fy = rng.uniform(-ROOM_D / 2, ROOM_D / 2, n_floor)
    fz = rng.normal(0.0, 0.01, n_floor)
    parts.append(np.column_stack([fx, fy, fz]))

    n_wall = 10_000
    for axis, sign in ((0, -1), (0, 1), (1, -1), (1, 1)):
        along = rng.uniform(-ROOM_W / 2, ROOM_W / 2, n_wall) if axis == 1 else rng.uniform(-ROOM_D / 2, ROOM_D / 2, n_wall)
        height = rng.uniform(0.0, WALL_H, n_wall)
        jitter = rng.normal(0.0, 0.015, n_wall)
        if axis == 0:
            wall = np.column_stack([np.full(n_wall, sign * ROOM_W / 2) + jitter, along, height])
        else:
            wall = np.column_stack([along, np.full(n_wall, sign * ROOM_D / 2) + jitter, height])
        parts.append(wall)

    n_col = 3_500
    for _ in range(6):
        cx = rng.uniform(-ROOM_W / 2 + 1.5, ROOM_W / 2 - 1.5)
        cy = rng.uniform(-ROOM_D / 2 + 1.5, ROOM_D / 2 - 1.5)
        theta = rng.uniform(0, 2 * math.pi, n_col)
        r = 0.15 + rng.normal(0.0, 0.01, n_col)
        h = rng.uniform(0.0, 2.2, n_col)
        parts.append(np.column_stack([cx + r * np.cos(theta), cy + r * np.sin(theta), h]))

    scene = np.concatenate(parts).astype(np.float32)
    rng.shuffle(scene)  # so progressive reveal grows everywhere at once
    return scene


async def main() -> None:
    rng = np.random.default_rng(7)
    scene = build_scene(rng)
    clients: set = set()
    latest: dict = {"cloud": None, "scan": None, "pose": None, "seq": 0, "scan_seq": 0}
    t0 = time.monotonic()

    def pose_at(elapsed: float) -> tuple[float, float, float]:
        a = POSE_RAD_S * elapsed
        return POSE_RADIUS * math.cos(a), POSE_RADIUS * math.sin(a), a + math.pi / 2

    def make_cloud() -> bytes:
        elapsed = time.monotonic() - t0
        frac = min(1.0, 0.1 + elapsed / REVEAL_SECONDS)
        n = int(len(scene) * frac)
        angle = SPIN_RAD_S * elapsed
        c, s = math.cos(angle), math.sin(angle)
        pts = scene[:n]
        rotated = np.empty_like(pts)
        rotated[:, 0] = c * pts[:, 0] - s * pts[:, 1]
        rotated[:, 1] = s * pts[:, 0] + c * pts[:, 1]
        rotated[:, 2] = pts[:, 2]
        latest["seq"] += 1
        header = struct.pack("<IIdI", CLOUD_MAGIC, latest["seq"], time.time(), n)
        return header + np.ascontiguousarray(rotated, dtype="<f4").tobytes()

    def make_scan() -> bytes:
        # World-frame points near the robot, like the Go2's rolling snapshot.
        # The scene spins, so rotate the pose into scene frame to select,
        # then rotate the selected points back into world frame.
        elapsed = time.monotonic() - t0
        px, py, _ = pose_at(elapsed)
        angle = SPIN_RAD_S * elapsed
        c, s = math.cos(angle), math.sin(angle)
        sx = c * px + s * py
        sy = -s * px + c * py
        near = scene[
            (scene[:, 0] - sx) ** 2 + (scene[:, 1] - sy) ** 2 < SCAN_RADIUS**2
        ]
        if len(near) > 25_000:
            near = near[:: -(-len(near) // 25_000)]
        pts = np.empty_like(near)
        pts[:, 0] = c * near[:, 0] - s * near[:, 1]
        pts[:, 1] = s * near[:, 0] + c * near[:, 1]
        pts[:, 2] = near[:, 2]
        pts = pts + rng.normal(0.0, 0.012, pts.shape).astype(np.float32)
        latest["scan_seq"] += 1
        header = struct.pack("<IIdI", SCAN_MAGIC, latest["scan_seq"], time.time(), len(pts))
        return header + np.ascontiguousarray(pts, dtype="<f4").tobytes()

    def make_pose() -> str:
        x, y, yaw = pose_at(time.monotonic() - t0)
        return json.dumps(
            {"type": "pose", "x": x, "y": y, "z": 0.0, "yaw": yaw, "t": time.time()}
        )

    async def handle_client(websocket) -> None:
        if websocket.request.path != WS_PATH:
            await websocket.close(1008, "Not Found")
            return
        clients.add(websocket)
        print(f"client connected: {websocket.remote_address} ({len(clients)} total)")
        try:
            if latest["cloud"] is not None:
                await websocket.send(latest["cloud"])
            if latest["scan"] is not None:
                await websocket.send(latest["scan"])
            if latest["pose"] is not None:
                await websocket.send(latest["pose"])
            async for _ in websocket:
                pass  # inbound data is ignored; iterating detects close
        except websockets.ConnectionClosed:
            pass
        finally:
            clients.discard(websocket)
            print(f"client disconnected ({len(clients)} total)")

    async def cloud_loop() -> None:
        while True:
            latest["cloud"] = make_cloud()
            ws_server.broadcast(clients, latest["cloud"])
            await asyncio.sleep(1.0)

    async def scan_loop() -> None:
        while True:
            latest["scan"] = make_scan()
            ws_server.broadcast(clients, latest["scan"])
            await asyncio.sleep(0.2)

    async def pose_loop() -> None:
        while True:
            latest["pose"] = make_pose()
            ws_server.broadcast(clients, latest["pose"])
            await asyncio.sleep(0.1)

    async with ws_server.serve(handle_client, host=HOST, port=PORT, max_size=2**20):
        print(f"fake map stream: ws://{HOST}:{PORT}{WS_PATH} ({len(scene)} points when fully revealed)")
        await asyncio.gather(cloud_loop(), scan_loop(), pose_loop())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
