"""Outbound WebSocket streamer for the live voxel map and robot pose.

Feeds the booth's /lidar viewer: the FastAPI server relays this stream to
browsers at /ws/lidar. Wire protocol (shared with scripts/fake_map_stream.py
and client/components/lidar/protocol.ts):

Binary frames (little-endian), same 20-byte header, distinguished by magic:
    offset 0   uint32   magic: 0x4E575043 ("NWPC") accumulated global map
                               0x4E57534E ("NWSN") live lidar scan
                               0x4E57504D ("NWPM") saved premap
    offset 4   uint32   seq (per-stream counter)
    offset 8   float64  timestamp_s
    offset 16  uint32   point_count N
    offset 20  float32  x, y, z interleaved (N * 3 floats, Z-up world frame)

The scan stream is the Go2's rolling ~6.4 m voxel snapshot around the robot,
already in world coordinates, forwarded at up to 5 Hz: what the robot sees
right now, versus the 1 Hz accumulated map.

Text frame (JSON): {"type": "pose", "x", "y", "z", "yaw", "t"}.
Text frame (JSON): {"type": "premap", "aligned": bool}.

New clients immediately receive the cached latest map, scan, and pose.
Server lifecycle mirrors RerunWebSocketServer; In-port handlers run on
the module's own asyncio loop with a latest-only mailbox, so a slow tick
drops stale clouds instead of building a backlog.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import threading
import time
from typing import Any, Protocol

import numpy as np
import websockets
import websockets.asyncio.server as ws_server

from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.spec.utils import Spec
from dimos.utils.logging_config import setup_logger
from dimos.visualization.rerun.websocket_server import _handshake_noise_filter

logger = setup_logger()

CLOUD_MAGIC = 0x4E575043  # "NWPC", accumulated global map
SCAN_MAGIC = 0x4E57534E  # "NWSN", live lidar scan
PREMAP_MAGIC = 0x4E57504D  # "NWPM", saved premap


class RelocalizationSpec(Spec, Protocol):
    def world_to_map_2d(self) -> dict | None: ...


class MapStreamerConfig(ModuleConfig):
    # Loopback only: the booth FastAPI relay is the single consumer.
    host: str = "127.0.0.1"
    port: int = 8010
    ws_path: str = "/ws/map"
    # Same budget as the Rerun viewer (_MAX_VIEWER_CLOUD_POINTS).
    max_points: int = 150_000
    scan_max_points: int = 60_000
    cloud_min_interval_s: float = 1.0
    scan_min_interval_s: float = 0.2
    pose_min_interval_s: float = 0.1


class MapStreamer(Module):
    """Broadcast live map, saved premap, scan, and odometry over WebSocket."""

    config: MapStreamerConfig

    global_map: In[PointCloud2]
    loaded_map: In[PointCloud2]
    lidar: In[PointCloud2]
    odom: In[PoseStamped]
    _reloc: RelocalizationSpec | None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._clients: set[Any] = set()
        self._latest_cloud: bytes | None = None
        self._latest_premap: bytes | None = None
        self._latest_premap_status: str | None = None
        self._latest_scan: bytes | None = None
        self._latest_pose: str | None = None
        self._seq = 0
        self._premap_seq = 0
        self._scan_seq = 0
        self._last_cloud_t = 0.0
        self._last_scan_t = 0.0
        self._last_pose_t = 0.0
        self._stop_event: asyncio.Event | None = None
        self._server_ready = threading.Event()
        # Relocalization can spend more than a minute inside ICP. Calling its
        # status RPC from handle_loaded_map used to block this module's asyncio
        # loop for the entire solve, starving WebSocket handshakes and every
        # live map/scan/pose callback. Query it on one daemon thread and let the
        # stream continue publishing the unaligned reference until a transform
        # is actually available.
        self._reloc_transform: dict[str, float] | None = None
        self._reloc_query_inflight = False
        self._reloc_query_lock = threading.Lock()
        self._reloc_query_not_before = 0.0

    @rpc
    def start(self) -> None:
        super().start()
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        self._server_ready.wait()

    @rpc
    def stop(self) -> None:
        if not self._server_ready.is_set():
            super().stop()
            return
        if (
            self._loop is not None
            and not self._loop.is_closed()
            and self._stop_event is not None
        ):
            self._loop.call_soon_threadsafe(self._stop_event.set)
        super().stop()

    async def _serve(self) -> None:
        self._stop_event = asyncio.Event()

        ws_logger = logging.getLogger("websockets.server")
        ws_logger.addFilter(_handshake_noise_filter)

        async with ws_server.serve(
            self._handle_client,
            host=self.config.host,
            port=self.config.port,
            ping_interval=30,
            ping_timeout=30,
            max_size=2**20,  # inbound cap; outbound frames are unaffected
            logger=ws_logger,
        ):
            self._server_ready.set()
            logger.info(
                f"MapStreamer: serving ws://{self.config.host}:{self.config.port}{self.config.ws_path}"
            )
            await self._stop_event.wait()

    async def _handle_client(self, websocket: Any) -> None:
        if (
            hasattr(websocket, "request")
            and websocket.request.path != self.config.ws_path
        ):
            await websocket.close(1008, "Not Found")
            return
        self._clients.add(websocket)
        logger.info(f"MapStreamer: client connected from {websocket.remote_address}")
        try:
            if self._latest_cloud is not None:
                await websocket.send(self._latest_cloud)
            if self._latest_premap is not None:
                await websocket.send(self._latest_premap)
            if self._latest_premap_status is not None:
                await websocket.send(self._latest_premap_status)
            if self._latest_scan is not None:
                await websocket.send(self._latest_scan)
            if self._latest_pose is not None:
                await websocket.send(self._latest_pose)
            async for _ in websocket:
                pass  # inbound data is ignored; iterating detects close
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)

    async def handle_global_map(self, msg: PointCloud2) -> None:
        now = time.monotonic()
        if now - self._last_cloud_t < self.config.cloud_min_interval_s:
            return
        self._last_cloud_t = now

        points = msg.points_f32()
        if len(points) > self.config.max_points:
            # Uniform ceil-stride, same policy as the Rerun bridge subsampler.
            stride = -(-len(points) // self.config.max_points)
            points = points[::stride]

        self._seq += 1
        header = struct.pack(
            "<IIdI", CLOUD_MAGIC, self._seq, float(msg.ts), len(points)
        )
        self._latest_cloud = (
            header + np.ascontiguousarray(points, dtype="<f4").tobytes()
        )
        # broadcast() never blocks; clients with full buffers skip this frame,
        # which is fine because every frame is a full snapshot.
        ws_server.broadcast(self._clients, self._latest_cloud)

    async def handle_loaded_map(self, msg: PointCloud2) -> None:
        """Publish the persisted floor even before relocalization succeeds.

        Before lock, points remain in the saved MAP frame and the UI labels the
        layer as an unaligned reference. Once a world->map transform is
        available, the inverse is applied here so the saved floor overlays the
        live WORLD map exactly. The unaligned layer is visualization-only; it
        never enters the navigation costmap.
        """
        points = msg.points_f32()
        aligned = False
        transform = self._reloc_transform
        if transform is None:
            self._schedule_relocalization_query()
        if transform is not None:
            yaw = float(transform["yaw"])
            cos_yaw = float(np.cos(yaw))
            sin_yaw = float(np.sin(yaw))
            map_xy = points[:, :2].astype(np.float64, copy=False)
            shifted = map_xy - np.array(
                [float(transform["x"]), float(transform["y"])],
                dtype=np.float64,
            )
            world_xy = np.empty_like(shifted)
            # Inverse of map_xy = R(yaw) @ world_xy + translation.
            world_xy[:, 0] = cos_yaw * shifted[:, 0] + sin_yaw * shifted[:, 1]
            world_xy[:, 1] = -sin_yaw * shifted[:, 0] + cos_yaw * shifted[:, 1]
            points = points.copy()
            points[:, :2] = world_xy.astype(points.dtype, copy=False)
            aligned = True

        if len(points) > self.config.max_points:
            stride = -(-len(points) // self.config.max_points)
            points = points[::stride]

        self._premap_seq += 1
        header = struct.pack(
            "<IIdI",
            PREMAP_MAGIC,
            self._premap_seq,
            float(msg.ts),
            len(points),
        )
        self._latest_premap = (
            header + np.ascontiguousarray(points, dtype="<f4").tobytes()
        )
        self._latest_premap_status = json.dumps({"type": "premap", "aligned": aligned})
        ws_server.broadcast(self._clients, self._latest_premap)
        ws_server.broadcast(self._clients, self._latest_premap_status)

    def _schedule_relocalization_query(self) -> None:
        """Refresh the optional map transform without blocking the stream loop."""
        reloc = getattr(self, "_reloc", None)
        now = time.monotonic()
        if reloc is None or now < self._reloc_query_not_before:
            return
        with self._reloc_query_lock:
            if self._reloc_query_inflight:
                return
            self._reloc_query_inflight = True
            self._reloc_query_not_before = now + 5.0
        threading.Thread(
            target=self._query_relocalization_transform,
            name="nightwatch-map-reloc-status",
            daemon=True,
        ).start()

    def _query_relocalization_transform(self) -> None:
        try:
            reloc = getattr(self, "_reloc", None)
            transform = reloc.world_to_map_2d() if reloc is not None else None
            if transform is not None:
                self._reloc_transform = {
                    "x": float(transform["x"]),
                    "y": float(transform["y"]),
                    "yaw": float(transform["yaw"]),
                }
        except Exception:
            logger.exception("MapStreamer: relocalization status query failed")
        finally:
            with self._reloc_query_lock:
                self._reloc_query_inflight = False

    async def handle_lidar(self, msg: PointCloud2) -> None:
        now = time.monotonic()
        if now - self._last_scan_t < self.config.scan_min_interval_s:
            return
        self._last_scan_t = now

        points = msg.points_f32()
        if len(points) > self.config.scan_max_points:
            stride = -(-len(points) // self.config.scan_max_points)
            points = points[::stride]

        self._scan_seq += 1
        header = struct.pack(
            "<IIdI", SCAN_MAGIC, self._scan_seq, float(msg.ts), len(points)
        )
        self._latest_scan = header + np.ascontiguousarray(points, dtype="<f4").tobytes()
        ws_server.broadcast(self._clients, self._latest_scan)

    async def handle_odom(self, msg: PoseStamped) -> None:
        now = time.monotonic()
        if now - self._last_pose_t < self.config.pose_min_interval_s:
            return
        self._last_pose_t = now

        self._latest_pose = json.dumps(
            {
                "type": "pose",
                "x": float(msg.position.x),
                "y": float(msg.position.y),
                "z": float(msg.position.z),
                "yaw": float(msg.orientation.to_euler().z),
                "t": float(msg.ts),
            }
        )
        ws_server.broadcast(self._clients, self._latest_pose)
