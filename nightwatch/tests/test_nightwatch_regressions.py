from __future__ import annotations

import asyncio
from collections import Counter, deque
import json
import math
import os
from pathlib import Path
from queue import Queue
import sqlite3
import struct
import subprocess
import sys
from threading import Condition, Event, Lock, RLock, Thread
import time
from types import SimpleNamespace

from dimos.core.transport import JpegLcmTransport
from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import OccupancyGrid
from dimos.msgs.sensor_msgs.Image import Image, ImageFormat
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.navigation.base import NavigationState
from dimos.navigation.replanning_a_star.module import _repeated_obstacle_cluster
from dimos.protocol.pubsub.impl.lcmpubsub import LCM
from dimos.protocol.pubsub.impl.zenohpubsub import Zenoh
from dimos.robot.unitree.connection import UnitreeWebRTCConnection
from fastapi.testclient import TestClient
from nightwatch import (
    connection as nightwatch_connection,
    unitree as nightwatch_unitree,
)
from nightwatch.agent import McpClient
from nightwatch.blueprints import (
    FRONTIER_GOAL_TIMEOUT_S,
    _MAX_VIEWER_CLOUD_POINTS,
    _convert_big_cloud,
    _convert_saved_map,
    _convert_saved_map_preview,
    _scout_rerun_config,
    scout,
)
from nightwatch.connection import GO2Connection
from nightwatch.contracts import BehaviorKind, BehaviorLease, RobotActivity
from nightwatch.curiosity import (
    CuriosityConfig,
    CuriositySupervisor,
    _skin_cover_fraction,
)
from nightwatch.escort import EscortConfig, EscortResult, run_escort
from nightwatch.follow import (
    NightwatchPersonFollow,
    _frontal_face_looking,
    _raised_hand,
)
from nightwatch.identity import AnonymousPersonMemory
from nightwatch.intervene import (
    InterventionSkill,
    check_gaze,
    frontal_face_looking,
    run_wave_protocol,
)
from nightwatch.memory import (
    NightwatchMapRecorder,
    NightwatchMapRecorderConfig,
    NightwatchRelocalization,
    NightwatchSpatialMemory,
    _atomic_write_json,
)
from nightwatch.map_stream import MapStreamer, PREMAP_MAGIC
from nightwatch.navigation import (
    NightwatchCoveragePatrolRouter,
    PatrollingModule,
    WavefrontFrontierExplorer,
    _anchor_world_operator_zones,
    _load_operator_zones,
    _point_obeys_operator_zones,
    _prelock_world_keep_outs,
    _segment_obeys_operator_zones,
    _world_to_map_point,
    _resolve_odom_epoch,
)
from nightwatch.planar_relocalize import relocalize_planar
from nightwatch.speak import SpeakSkill, _chain_for_mode
from nightwatch import voice_presets
from nightwatch.tracker import (
    YoloFollowTracker,
    _appearance_descriptor,
    _best_device,
)
from nightwatch.webchat import (
    _OPERATOR_ACTIONS,
    _OPERATOR_HTML,
    _camera_frame_is_current,
    _project_cached_status,
    LatestFrameRobotWebInterface,
    NightwatchWebInput,
    _validate_assessment,
)
from nightwatch.world_model import (
    _SPATIAL_NAVIGATION_FAILURES,
    _TRANSLATIONAL_BEHAVIORS,
    NightwatchWorldModel,
    WorldModelConfig,
)
import numpy as np
import pytest
from reactivex.subject import Subject
from unitree_webrtc_connect.constants import SPORT_CMD


def test_launcher_never_preflights_unitree_signaling_slot() -> None:
    launcher = (Path(__file__).parents[1] / "run_scout.sh").read_text()
    executable = "\n".join(
        line for line in launcher.splitlines() if not line.lstrip().startswith("#")
    )

    # On the venue Go2, even an abandoned TCP connection occupies the
    # firmware's single signaling accept slot. Only the real WebRTC SDP
    # transaction may touch either signaling port.
    assert "/con_notify" not in executable
    assert "192.168.12.1 9991" not in executable
    assert "192.168.12.1 8081" not in executable


def test_operator_zone_map_projection_and_route_guard(tmp_path) -> None:
    path = tmp_path / "zones.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "zones": [
                    {
                        "id": "glass",
                        "kind": "keep_out",
                        "points": [[1, -1], [3, -1], [3, 1], [1, 1]],
                        "world_points": [[11, -1], [13, -1], [13, 1], [11, 1]],
                    }
                ],
            }
        )
    )

    fallback = _load_operator_zones(str(path), None)
    projected = _load_operator_zones(str(path), (10.0, 0.0, 0.0))
    assert fallback[0]["points"][0] == (11.0, -1.0)
    assert projected[0]["points"][0] == (-9.0, -1.0)
    assert _point_obeys_operator_zones(12.0, 0.0, fallback) is False
    assert _segment_obeys_operator_zones(10.0, 0.0, 14.0, 0.0, fallback) is False


def test_prelock_world_zone_is_anchored_without_jumping(
    tmp_path,
) -> None:
    path = tmp_path / "zones.json"
    points = [[1, -1], [3, -1], [3, 1], [1, 1]]
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "zones": [
                    {
                        "id": "focus",
                        "kind": "keep_in",
                        # Legacy UI duplicated WORLD into `points` before a
                        # WORLD-to-MAP transform was available.
                        "points": points,
                        "world_points": points,
                    }
                ],
            }
        )
    )

    before_lock = _load_operator_zones(str(path), None)
    transform = (10.0, 4.0, 0.5)
    assert _anchor_world_operator_zones(str(path), transform) == 1
    after_lock = _load_operator_zones(str(path), transform)
    persisted = json.loads(path.read_text())["zones"][0]

    assert before_lock[0]["points"][0] == (1.0, -1.0)
    assert np.allclose(after_lock[0]["points"], before_lock[0]["points"])
    assert persisted["points_frame"] == "map"
    assert persisted["points"][0] == pytest.approx(
        _world_to_map_point(1.0, -1.0, transform)
    )


def test_map_zone_refreshes_world_fallback_for_global_planner(tmp_path) -> None:
    path = tmp_path / "zones.json"
    map_points = [[10, 4], [12, 4], [12, 6], [10, 6]]
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "zones": [
                    {
                        "id": "focus",
                        "kind": "keep_in",
                        "points_frame": "map",
                        "points": map_points,
                        "world_points": [[999, 999]] * 4,
                    }
                ],
            }
        )
    )

    transform = (10.0, 4.0, 0.0)
    assert _anchor_world_operator_zones(str(path), transform) == 0
    persisted = json.loads(path.read_text())["zones"][0]

    assert np.allclose(
        persisted["world_points"],
        [[0, 0], [2, 0], [2, 2], [0, 2]],
    )
    loaded = _load_operator_zones(str(path), transform)
    assert np.allclose(loaded[0]["points"], persisted["world_points"])


def test_camera_replay_cannot_replace_the_current_live_frame() -> None:
    now = 1_000.0

    assert _camera_frame_is_current(999.9, 999.8, now=now)
    assert not _camera_frame_is_current(999.7, 999.8, now=now)
    assert not _camera_frame_is_current(600.0, 999.8, now=now)

def test_frontier_and_patrol_legs_allow_full_floor_travel() -> None:
    assert FRONTIER_GOAL_TIMEOUT_S >= 60.0
    assert PatrollingModule._patrol_goal_timeout_s >= 60.0
    assert WavefrontFrontierExplorer._max_consecutive_frontier_failures <= 2
    assert WavefrontFrontierExplorer._frontier_retry_s <= 0.5


def test_repeated_obstacle_cluster_uses_current_contact_region() -> None:
    events: list[tuple[float, float, float]] = []
    clustered, events = _repeated_obstacle_cluster(
        events, now=1.0, x=2.0, y=3.0
    )
    assert clustered is False
    clustered, events = _repeated_obstacle_cluster(
        events, now=4.0, x=2.3, y=3.1
    )
    assert clustered is False
    clustered, events = _repeated_obstacle_cluster(
        events, now=7.0, x=1.8, y=2.9
    )
    assert clustered is True

    # An unrelated obstacle elsewhere is not merged into this boundary.
    clustered, _events = _repeated_obstacle_cluster(
        events, now=8.0, x=8.0, y=8.0
    )
    assert clustered is False


def test_route_boundary_signal_requests_one_guarded_escape() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._lock = RLock()
    curiosity._route_hazard_escape_requested = False

    curiosity._on_navigation_obstacle(
        PoseStamped(position=Vector3(2.0, 3.0, 0.0))
    )

    assert curiosity._route_hazard_escape_requested is True


def test_global_no_path_recovery_requests_one_guarded_escape() -> None:
    # The planner's dead-end branches ("No safe goal", "No path found") fire
    # recovery_request instead of navigation_obstacle so a blocked START pose
    # is never learned as a route-boundary exclusion.
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._lock = RLock()
    curiosity._route_hazard_escape_requested = False

    curiosity._on_recovery_request(
        PoseStamped(position=Vector3(1.0, -2.0, 0.0))
    )

    assert curiosity._route_hazard_escape_requested is True


def test_recovery_request_port_contract_matches_planner_module() -> None:
    # recovery_request relies on the same implicit name-based port connection
    # as the proven navigation_obstacle channel: the planner module publishes
    # Out[PoseStamped] and the supervisor consumes In[PoseStamped] under the
    # SAME port name. A rename on either side silently severs the channel, so
    # pin both names and payload types here.
    import typing

    from dimos.core.stream import In, Out
    from dimos.navigation.replanning_a_star.module import ReplanningAStarPlanner

    planner_hints = typing.get_type_hints(ReplanningAStarPlanner)
    curiosity_hints = typing.get_type_hints(CuriositySupervisor)
    for port in ("recovery_request", "navigation_obstacle"):
        planner_port = planner_hints[port]
        curiosity_port = curiosity_hints[port]
        assert typing.get_origin(planner_port) is Out
        assert typing.get_origin(curiosity_port) is In
        assert typing.get_args(planner_port) == (PoseStamped,)
        assert typing.get_args(curiosity_port) == (PoseStamped,)


def test_vectorized_frontiers_only_use_robot_reachable_free_space() -> None:
    grid = np.full((120, 120), -1, dtype=np.int8)
    grid[10:50, 10:50] = 0
    grid[70:100, 70:100] = 0  # disconnected saved-map island
    costmap = OccupancyGrid(grid=grid, resolution=0.1)
    robot = costmap.grid_to_world(Vector3(25.0, 25.0, 0.0))
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.config = SimpleNamespace(
        occupancy_threshold=99,
        min_frontier_perimeter=0.5,
    )
    captured: list[Vector3] = []

    def capture(
        frontiers: list[Vector3],
        _sizes: list[int],
        _robot: Vector3,
        _costmap: OccupancyGrid,
    ) -> list[Vector3]:
        captured.extend(frontiers)
        return frontiers

    explorer._rank_frontiers = capture
    ranked = explorer.detect_frontiers(robot, costmap)

    assert ranked == captured
    assert len(captured) == 1
    expected = costmap.grid_to_world(Vector3(29.5, 29.5, 0.0))
    assert math.hypot(captured[0].x - expected.x, captured[0].y - expected.y) < 0.2


def test_macos_yolo_never_defaults_to_mps(monkeypatch) -> None:
    monkeypatch.delenv("NIGHTWATCH_YOLO_DEVICE", raising=False)
    monkeypatch.setattr("nightwatch.tracker.platform.system", lambda: "Darwin")
    assert _best_device() == "cpu"


def test_yolo_device_can_be_explicitly_overridden(monkeypatch) -> None:
    monkeypatch.setenv("NIGHTWATCH_YOLO_DEVICE", "cpu")
    assert _best_device() == "cpu"


def test_follow_velocity_is_arbitrated_by_movement_manager() -> None:
    assert (
        scout.remapping_map[(NightwatchPersonFollow.name, "cmd_vel")] == "nav_cmd_vel"
    )


def test_motion_rearm_uses_full_dimos_stand_ready_sequence(monkeypatch) -> None:
    requests: list[dict] = []
    connection = SimpleNamespace(
        publish_request=lambda _topic, request: (
            requests.append(request.copy()) or {"status": "ok"}
        )
    )
    monkeypatch.setattr(nightwatch_unitree.time, "sleep", lambda _seconds: None)
    nightwatch_unitree._last_ready[0] = 0.0

    assert nightwatch_unitree.ensure_motion_ready(connection, force=True) is True
    assert [request["api_id"] for request in requests] == [
        # MCF's documented Pose exit; firmware state can outlive the process.
        SPORT_CMD["StopMove"],
        SPORT_CMD["StandUp"],
        SPORT_CMD["RecoveryStand"],
        SPORT_CMD["BalanceStand"],
    ]


def test_motion_rearm_does_not_claim_success_after_rejected_step(monkeypatch) -> None:
    requests: list[dict] = []

    def publish(_topic, request):
        requests.append(request.copy())
        if request["api_id"] == SPORT_CMD["RecoveryStand"]:
            return {"status": "error", "code": 3}
        return {"status": "ok"}

    monkeypatch.setattr(nightwatch_unitree.time, "sleep", lambda _seconds: None)
    nightwatch_unitree._last_ready[0] = 0.0

    assert (
        nightwatch_unitree.ensure_motion_ready(
            SimpleNamespace(publish_request=publish), force=True
        )
        is False
    )
    assert [request["api_id"] for request in requests] == [
        SPORT_CMD["StopMove"],
        SPORT_CMD["StandUp"],
        SPORT_CMD["RecoveryStand"],
    ]
    assert nightwatch_unitree._last_ready[0] == 0.0


def test_motion_rearm_continues_when_lying_robot_rejects_stopmove(
    monkeypatch,
) -> None:
    """A down Go2 has nothing to stop, but must still receive StandReady."""
    requests: list[dict] = []

    def publish(_topic, request):
        requests.append(request.copy())
        if request["api_id"] == SPORT_CMD["StopMove"]:
            return {
                "type": "res",
                "data": {"header": {"status": {"code": -1}}, "data": ""},
            }
        return {"status": "ok"}

    monkeypatch.setattr(nightwatch_unitree.time, "sleep", lambda _seconds: None)
    nightwatch_unitree._last_ready[0] = 0.0
    nightwatch_unitree._pose_mode_on[0] = True

    assert (
        nightwatch_unitree.ensure_motion_ready(
            SimpleNamespace(publish_request=publish), force=True
        )
        is True
    )
    assert [request["api_id"] for request in requests] == [
        SPORT_CMD["StopMove"],
        SPORT_CMD["StandUp"],
        SPORT_CMD["RecoveryStand"],
        SPORT_CMD["BalanceStand"],
    ]
    assert nightwatch_unitree.pose_mode_latched() is False


def test_motion_rearm_is_not_repeated_for_ordinary_planner_restarts(
    monkeypatch,
) -> None:
    requests: list[dict] = []
    clock = [1000.0]
    connection = SimpleNamespace(
        publish_request=lambda _topic, request: (
            requests.append(request.copy()) or {"status": "ok"}
        )
    )
    monkeypatch.setattr(nightwatch_unitree.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(nightwatch_unitree.time, "monotonic", lambda: clock[0])
    nightwatch_unitree._last_ready[0] = 0.0

    assert nightwatch_unitree.ensure_motion_ready(connection) is True
    first_request_count = len(requests)
    clock[0] += 30.0
    assert nightwatch_unitree.ensure_motion_ready(connection) is True

    assert first_request_count == 4
    assert len(requests) == first_request_count


def test_dog_expressions_do_not_poison_navigation_failure_map() -> None:
    assert BehaviorKind.EXPRESS not in _TRANSLATIONAL_BEHAVIORS
    assert BehaviorKind.EXPLORE in _TRANSLATIONAL_BEHAVIORS
    assert BehaviorKind.FOLLOW in _TRANSLATIONAL_BEHAVIORS


def test_edge_first_frontier_ranking_rejects_near_repeat() -> None:
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.config = SimpleNamespace(
        occupancy_threshold=99,
        safe_distance=3.0,
        min_frontier_perimeter=0.5,
        max_explored_distance=10.0,
        lookahead_distance=5.0,
    )
    explorer._coverage_lock = RLock()
    explorer._goal_visits = [(0.8, 0.0, time.time())]
    explorer._failure_zones = []
    explorer.exploration_direction = Vector3()
    costmap = OccupancyGrid(
        grid=np.zeros((200, 200), dtype=np.int8),
        resolution=0.05,
    )

    ranked = explorer._rank_frontiers(
        [Vector3(0.8, 0.0, 0.0), Vector3(4.0, 0.0, 0.0)],
        [20, 20],
        Vector3(0.0, 0.0, 0.0),
        costmap,
    )

    assert ranked[0].x == 4.0


def test_stop_at_current_pose_is_not_learned_as_failed_goal() -> None:
    model = object.__new__(NightwatchWorldModel)
    model._lock = RLock()
    model._latest_odom = PoseStamped(position=Vector3(2.0, 3.0, 0.0))
    model._goal_history = deque(maxlen=20)
    failures: list[str] = []
    model._record_failure = lambda category, *_args, **_kwargs: failures.append(
        category
    )

    for _ in range(4):
        model._on_goal_request(PoseStamped(position=Vector3(2.1, 3.1, 0.0)))

    assert not model._goal_history
    assert not failures


def test_camera_health_is_not_a_spatial_navigation_failure() -> None:
    assert "camera_stale" not in _SPATIAL_NAVIGATION_FAILURES
    assert {"stuck", "coverage_loop", "repeated_goal"} <= _SPATIAL_NAVIGATION_FAILURES


def test_camera_uses_bounded_jpeg_transport_not_raw_zenoh_shm() -> None:
    spec = scout.transport_map[("color_image", Image)]
    assert isinstance(spec, JpegLcmTransport)


def test_go2_camera_worker_does_not_load_duplicate_avfoundation() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import numpy as np; "
                "from nightwatch.connection import GO2Connection; "
                "from dimos.msgs.sensor_msgs.Image import Image,ImageFormat; "
                "Image(data=np.zeros((8,8,3),dtype=np.uint8),"
                "format=ImageFormat.RGB).lcm_jpeg_encode(); "
                "print('clean')"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "AVFFrameReceiver is implemented in both" not in result.stderr
    assert result.stdout.strip() == "clean"


def test_silent_continuation_does_not_enqueue_an_llm_turn() -> None:
    client = object.__new__(McpClient)
    client._tool_registry = {"follow_person": object()}
    client._message_queue = Queue()
    calls: list[tuple[str, dict]] = []
    client._mcp_tool_call = lambda name, args: calls.append((name, args)) or {}

    dispatched = client.dispatch_continuation(
        {"tool": "follow_person", "args": {"query": "person", "initial_bbox": "$bbox"}},
        {"bbox": [1, 2, 3, 4], "_silent": True},
    )

    assert dispatched is True
    assert calls == [
        ("follow_person", {"query": "person", "initial_bbox": [1, 2, 3, 4]})
    ]
    assert client._message_queue.empty()


def test_silent_continuation_rejects_mcp_text_errors() -> None:
    client = object.__new__(McpClient)
    client._tool_registry = {}
    client._mcp_tool_call = lambda _name, _args: {
        "content": [{"type": "text", "text": "Tool not found: begin_exploration"}]
    }

    assert (
        client.dispatch_continuation(
            {"tool": "begin_exploration", "args": {}},
            {"_silent": True},
        )
        is False
    )


def test_curiosity_close_person_uses_shared_follow_detector() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = SimpleNamespace(
        person_close_frac=0.45, person_poll_s=0.0, follow_streak=2
    )
    curiosity._follow_block_until = 0.0
    curiosity._last_person_poll = 0.0
    curiosity._close_streak = 1
    curiosity._follow = SimpleNamespace(
        observe_person=lambda threshold: {
            "bbox": [10.0, 20.0, 100.0, 400.0],
            "offset": 0.1,
            "track_id": 7,
        }
    )

    assert curiosity._person_is_close(1.0) is True
    assert curiosity._close_bbox == [10.0, 20.0, 100.0, 400.0]


def test_spatial_memory_stop_is_idempotent(monkeypatch) -> None:
    memory = object.__new__(NightwatchSpatialMemory)
    memory._nightwatch_memory_stopped = False
    calls: list[str] = []
    monkeypatch.setattr(
        "dimos.perception.spatial_perception.SpatialMemory.stop",
        lambda _self: calls.append("stop"),
    )

    memory.stop()
    memory.stop()

    assert calls == ["stop"]


def test_map_recorder_excludes_high_rate_camera() -> None:
    declared_ports = set(NightwatchMapRecorder.__annotations__)
    assert {"lidar", "odom"} <= declared_ports
    assert "color_image" not in declared_ports
    assert NightwatchMapRecorderConfig().record_tf is False


def test_map_recorder_stops_callbacks_before_store_and_only_once(monkeypatch) -> None:
    events: list[str] = []
    recorder = object.__new__(NightwatchMapRecorder)
    recorder._nightwatch_recorder_stopped = False
    recorder._recording_disposables = SimpleNamespace(
        dispose=lambda: events.append("callbacks")
    )
    recorder._loop = None
    monkeypatch.setattr(
        "dimos.core.module.Module.stop", lambda _self: events.append("store")
    )

    recorder.stop()
    recorder.stop()

    assert events == ["callbacks", "store"]


def test_go2_shutdown_is_idempotent(monkeypatch) -> None:
    events: list[str] = []
    connection = object.__new__(GO2Connection)
    connection._nightwatch_connection_stopped = False
    connection._lifecycle_stop = Event()
    connection._camera_info_stop = Event()
    connection._lidar_pulse_thread = None
    connection._watchdog_thread = None
    connection._camera_info_thread = None
    connection._sensor_subscriptions = None
    connection._connection_lock = RLock()
    connection.connection = SimpleNamespace(
        stop_movement=lambda: events.append("stop-movement"),
        stop=lambda: events.append("webrtc"),
    )
    monkeypatch.setattr(
        "dimos.core.module.Module.stop", lambda _self: events.append("module")
    )

    connection.stop()
    connection.stop()

    assert events == ["stop-movement", "webrtc", "module"]


def test_video_stale_watchdog_reasserts_on_without_replacing_peer(monkeypatch) -> None:
    events: list[object] = []
    driver = object.__new__(UnitreeWebRTCConnection)
    driver.conn = SimpleNamespace(
        video=SimpleNamespace(
            switchVideoChannel=lambda enabled: events.append(("video", enabled))
        )
    )

    class ImmediateLoop:
        def call_soon_threadsafe(self, callback, *args):
            callback(*args)

        def call_later(self, delay, callback, *args):
            events.append(("delay", delay))
            callback(*args)

    driver.loop = ImmediateLoop()
    connection = object.__new__(GO2Connection)
    connection.connection = driver
    connection._last_video_rearm_at = 0.0
    connection._video_rearm_count = 0
    connection._reconnect = lambda: events.append("peer-replaced")
    monkeypatch.setattr("nightwatch.connection.time.monotonic", lambda: 100.0)

    connection._rearm_video_channel()

    assert events == [("video", True)]
    assert connection._video_rearm_count == 1
    assert "peer-replaced" not in events


def _watchdog_connection(peer_state: str = "connected") -> tuple[GO2Connection, list]:
    events: list[object] = []
    driver = object.__new__(UnitreeWebRTCConnection)
    driver.conn = SimpleNamespace(
        pc=SimpleNamespace(
            connectionState=peer_state,
            iceConnectionState="completed",
        ),
        video=SimpleNamespace(
            switchVideoChannel=lambda enabled: events.append(("video", enabled))
        ),
    )
    driver.loop = SimpleNamespace(
        call_soon_threadsafe=lambda callback, *args: callback(*args)
    )
    connection = object.__new__(GO2Connection)
    connection.connection = driver
    connection._connected_at = 90.0
    connection._last_video_frame_at = 90.0
    connection._last_video_rearm_at = 0.0
    connection._video_rearm_count = 0
    connection._reconnecting = False
    connection._request_full_recovery = (
        lambda reason: events.append(("recover", reason)) or True
    )
    return connection, events


def test_video_watchdog_uses_channel_rearm_for_transient_staleness(
    monkeypatch,
) -> None:
    connection, events = _watchdog_connection()
    monkeypatch.setattr("nightwatch.connection.time.monotonic", lambda: 100.0)

    assert connection._video_recovery_tick(100.0) == "rearm"
    assert events == [("video", True)]


@pytest.mark.parametrize(
    ("peer_state", "age", "reason"),
    [
        ("closed", 1.0, "peer_closed"),
        ("failed", 1.0, "peer_failed"),
        ("connected", 31.0, "video_prolonged_stale"),
    ],
)
def test_video_watchdog_rebuilds_only_terminal_or_prolonged_failures(
    peer_state: str,
    age: float,
    reason: str,
) -> None:
    connection, events = _watchdog_connection(peer_state)
    connection._last_video_frame_at = 100.0 - age

    assert connection._video_recovery_tick(100.0) == "reconnect"
    assert events == [("recover", reason)]


def test_full_webrtc_recovery_request_is_serialized_and_backed_off(
    monkeypatch,
) -> None:
    starts: list[object] = []

    class ParkedThread:
        def __init__(self, *, target, name, daemon):
            starts.append((target, name, daemon))

        def start(self):
            starts.append("started")

    monkeypatch.setattr(nightwatch_connection, "Thread", ParkedThread)
    monkeypatch.setattr("nightwatch.connection.time.monotonic", lambda: 100.0)
    connection = object.__new__(GO2Connection)
    connection._recovery_lock = Lock()
    connection._lifecycle_stop = Event()
    connection._reconnecting = False
    connection._last_reconnect_attempt_at = 0.0
    connection._last_reconnect_reason = None
    connection._reconnect_thread = None

    assert connection._request_full_recovery("peer_closed") is True
    assert connection._request_full_recovery("peer_closed") is False
    assert starts == [
        (connection._reconnect, "Go2-WebRTC-recovery", True),
        "started",
    ]
    assert connection._last_reconnect_reason == "peer_closed"


def test_full_webrtc_recovery_restores_all_sensor_subscriptions(
    monkeypatch,
) -> None:
    events: list[object] = []
    old = SimpleNamespace(
        stop_movement=lambda: events.append("old-stop-motion"),
        stop=lambda: events.append("old-stop"),
    )
    replacement = object.__new__(UnitreeWebRTCConnection)
    replacement.start = lambda: events.append("new-start")
    replacement.stop = lambda: events.append("new-stop")
    disposed = SimpleNamespace(dispose=lambda: events.append("dispose-streams"))
    build_kwargs: dict[str, object] = {}

    def fake_make_connection(*_args, **kwargs):
        build_kwargs.update(kwargs)
        return replacement

    monkeypatch.setattr(nightwatch_connection, "WEBRTC_SLOT_FREE_S", 0.0)
    monkeypatch.setattr(
        nightwatch_connection, "make_connection", fake_make_connection
    )
    connection = object.__new__(GO2Connection)
    connection.connection = old
    connection.config = SimpleNamespace(
        ip="192.168.12.1",
        g=SimpleNamespace(),
        aes_128_key=None,
        velocity_api=True,
        motion_mode=None,
        lidar=True,
    )
    connection._connection_lock = RLock()
    connection._recovery_lock = Lock()
    connection._lifecycle_stop = Event()
    connection._sensor_subscriptions = disposed
    connection._reconnecting = True
    connection._reconnect_count = 0
    connection._last_reconnect_reason = "peer_closed"
    connection._bind_sensor_streams = lambda: events.append("bind-all-streams")
    connection._ensure_locomotion_controller = (
        lambda: events.append("ensure-locomotion")
    )
    connection._configure_live_connection = lambda: events.append("configure")
    connection._set_lidar_stream = (
        lambda enabled: events.append(("lidar", enabled))
    )
    monkeypatch.setattr(
        nightwatch_connection,
        "ensure_motion_ready",
        lambda _connection, force=False: events.append(("motion-ready", force))
        or True,
    )

    connection._reconnect()

    assert connection.connection is replacement
    assert connection._reconnect_count == 1
    assert connection._reconnecting is False
    # A rebuilt peer preserves firmware MCF and restores the same direct sport
    # velocity path before accepting new motion.
    assert build_kwargs["mode"] is None
    assert build_kwargs["velocity_api"] is True
    assert events == [
        "old-stop-motion",
        "dispose-streams",
        "old-stop",
        "new-start",
        "bind-all-streams",
        "ensure-locomotion",
        ("motion-ready", True),
        "configure",
        ("lidar", True),
    ]


class _FakeMotionSwitcher:
    """Firmware motion-switcher that records accidental mode mutations."""

    def __init__(self, active: str = "mcf") -> None:
        self.active = active
        self.select_calls = 0

    def __call__(self, topic: str, data: dict) -> dict:
        api_id = data.get("api_id")
        if api_id == 1001:
            return {"data": {"data": json.dumps({"name": self.active})}}
        if api_id == 1002:
            self.select_calls += 1
        return {"code": 0}


def _locomotion_connection(
    firmware: _FakeMotionSwitcher,
) -> GO2Connection:
    connection = object.__new__(GO2Connection)
    connection.config = SimpleNamespace(motion_mode=None, velocity_api=True)
    webrtc = object.__new__(UnitreeWebRTCConnection)
    webrtc.publish_request = firmware
    connection.connection = webrtc
    connection._locomotion_controller = None
    return connection


def test_locomotion_probe_preserves_firmware_mcf_controller() -> None:
    firmware = _FakeMotionSwitcher(active="mcf")
    connection = _locomotion_connection(firmware)

    assert connection._ensure_locomotion_controller() is True
    assert firmware.select_calls == 0
    assert connection._locomotion_controller == "mcf"


def test_nested_firmware_error_is_not_mistaken_for_success() -> None:
    # Exact envelope from the live SelectMode("normal") response.
    assert (
        nightwatch_unitree._request_succeeded(
            {"data": {"header": {"status": {"code": 7004}}, "data": ""}}
        )
        is False
    )
    assert (
        nightwatch_unitree._request_succeeded(
            {"data": {"header": {"status": {"code": 0}}, "data": ""}}
        )
        is True
    )


def test_hung_replacement_build_never_wedges_recovery(monkeypatch) -> None:
    # Live failure 2026-07-25: the robot walked out of WiFi range, the
    # rebuild's make_connection()/start() blocked forever, _reconnecting
    # stayed True, and the camera never recovered even after the robot came
    # back into range. The build is now bounded: a hung attempt is abandoned,
    # the recovery latch is released for the next watchdog retry, and the
    # abandoned build's late connection stops itself to free the single
    # firmware WebRTC slot.
    events: list[object] = []
    release = Event()

    def blocked_start() -> None:
        release.wait(5.0)

    late = SimpleNamespace(
        start=blocked_start,
        stop=lambda: events.append("late-stop"),
    )
    old = SimpleNamespace(
        stop_movement=lambda: events.append("old-stop-motion"),
        stop=lambda: events.append("old-stop"),
    )
    monkeypatch.setattr(nightwatch_connection, "WEBRTC_SLOT_FREE_S", 0.0)
    monkeypatch.setattr(nightwatch_connection, "RECOVERY_BUILD_TIMEOUT_S", 0.2)
    monkeypatch.setattr(
        nightwatch_connection,
        "make_connection",
        lambda *_args, **_kwargs: late,
    )
    connection = object.__new__(GO2Connection)
    connection.connection = old
    connection.config = SimpleNamespace(
        ip="192.168.12.1",
        g=SimpleNamespace(),
        aes_128_key=None,
        velocity_api=True,
        motion_mode=None,
        lidar=True,
    )
    connection._connection_lock = RLock()
    connection._recovery_lock = Lock()
    connection._lifecycle_stop = Event()
    connection._sensor_subscriptions = None
    connection._reconnecting = True
    connection._reconnect_count = 0
    connection._last_reconnect_reason = "video_prolonged_stale"
    connection._connected_at = 0.0
    connection._last_video_frame_at = 0.0

    connection._reconnect()

    # The hung attempt finished in bounded time, counted as a failure, and
    # released the latch so the watchdog can retry after backoff.
    assert connection._reconnecting is False
    assert connection.connection is old
    assert connection._reconnect_count == 0
    assert "late-stop" not in events

    # The abandoned build eventually completes: it must immediately stop its
    # connection so the firmware slot is freed for the next real attempt.
    release.set()
    deadline = time.monotonic() + 2.0
    while "late-stop" not in events and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "late-stop" in events


def test_sensor_watchdogs_never_invoke_peer_reconnect() -> None:
    source = (
        __import__("inspect")
        .getsource(nightwatch_connection.GO2Connection._lidar_pulse_loop)
    )
    assert "self._reconnect()" not in source


def test_go2_continuous_move_does_not_wait_for_webrtc_loop(monkeypatch) -> None:
    callbacks: list[tuple] = []
    driver = object.__new__(UnitreeWebRTCConnection)
    driver.loop = SimpleNamespace(
        is_running=lambda: True,
        call_soon_threadsafe=lambda callback, *args: callbacks.append((callback, args)),
    )
    driver.stop_timer = None
    driver.cmd_vel_timeout = 0.2
    driver.stop_movement = lambda: None
    driver._publish_movement = lambda *_args: None

    class FakeTimer:
        daemon = False

        def __init__(self, _timeout, _callback):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(nightwatch_connection, "ThreadTimer", FakeTimer)
    connection = object.__new__(GO2Connection)
    connection.connection = driver
    connection._motion_commanded_until = 0.0

    assert connection.move(
        Twist(linear=Vector3(0.4, 0.0, 0.0), angular=Vector3(0.0, 0.0, 0.2))
    )
    assert callbacks == [(driver._publish_movement, (0.4, 0.0, 0.2))]


def test_go2_firmware_request_timeout_is_bounded(monkeypatch) -> None:
    cancelled: list[bool] = []

    async def pending_request(_topic, _data):
        return None

    class FakeFuture:
        def result(self, timeout):
            assert timeout == nightwatch_connection.WEBRTC_REQUEST_TIMEOUT_S
            raise nightwatch_connection.FutureTimeoutError()

        def cancel(self):
            cancelled.append(True)

    def fake_submit(coroutine, _loop):
        coroutine.close()
        return FakeFuture()

    driver = object.__new__(UnitreeWebRTCConnection)
    driver.loop = SimpleNamespace(is_running=lambda: True)
    driver.conn = SimpleNamespace(
        datachannel=SimpleNamespace(
            pub_sub=SimpleNamespace(publish_request_new=pending_request)
        )
    )
    monkeypatch.setattr(
        nightwatch_connection.asyncio, "run_coroutine_threadsafe", fake_submit
    )
    connection = object.__new__(GO2Connection)
    connection.connection = driver

    assert connection.publish_request("sport", {"api_id": 1}) == {
        "error": "firmware request timeout"
    }
    assert cancelled == [True]


def test_curiosity_is_immediate_and_liveness_is_three_seconds() -> None:
    config = CuriosityConfig()
    assert config.enabled is True
    assert config.liveness_timeout_s <= 3.0
    assert config.explore_liveness_timeout_s < 10.0
    assert config.patrol_liveness_timeout_s < 10.0
    assert config.autonomy_start_grace_s < 10.0
    assert config.min_battery_soc == 5
    assert config.hand_cover_fraction == 0.60
    assert config.return_home_radius_m > 0.0
    assert config.dog_expressions is True
    assert config.dog_expression_min_interval_s >= 30.0
    assert config.trust_persisted_map_complete is False
    assert not hasattr(config, "idle_after_s")


def test_safe_dog_expression_excludes_dynamic_tricks() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._lock = RLock()
    curiosity._leases = {}
    curiosity._battery_soc = 80
    curiosity._explicit_hold_reason = None
    curiosity._dog_expression_name = None
    curiosity._navigation_state = lambda: NavigationState.IDLE
    curiosity._is_following = lambda: False
    started: list[tuple[str, float]] = []
    curiosity._start_dog_expression = (
        lambda name, duration, _now: started.append((name, duration)) or True
    )

    assert curiosity.perform_dog_expression("WiggleHips") == "Performing WiggleHips."
    assert started and started[0][0] == "WiggleHips"
    assert "Unsupported safe expression" in curiosity.perform_dog_expression(
        "FrontFlip"
    )


def test_active_dog_expression_is_interruptible() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._dog_expression_name = "Stretch"
    curiosity._dog_expression_deadline = 99.0
    calls: list[int] = []
    curiosity._connection = SimpleNamespace(
        sport_command=lambda api_id: calls.append(api_id) or True
    )

    curiosity._interrupt_dog_expression("manual override")

    assert curiosity._dog_expression_name is None
    assert curiosity._dog_expression_deadline == 0.0
    assert calls == [SPORT_CMD["StopMove"]]


def test_low_battery_hold_lies_down_only_once(monkeypatch) -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._low_battery_liedown = False
    calls: list[str] = []
    curiosity._connection = SimpleNamespace(
        liedown=lambda: calls.append("liedown") or True
    )
    curiosity._is_following = lambda: False
    curiosity._curious_follow_started = 0.0
    curiosity._follow_stop_requested_at = 0.0
    curiosity._agent_spec = SimpleNamespace()
    curiosity._stop_exploration = lambda *_args, **_kwargs: None
    curiosity._stop_patrol = lambda *_args, **_kwargs: None
    curiosity._publish_stop = lambda: None
    curiosity._set_activity = lambda *_args, **_kwargs: None

    curiosity._hold("BATTERY_LOW")
    curiosity._hold("BATTERY_LOW")

    assert calls == ["liedown"]


def test_low_battery_returns_home_before_lie_down(monkeypatch) -> None:
    monkeypatch.setattr(
        "nightwatch.curiosity.ensure_motion_ready", lambda *_args, **_kwargs: True
    )
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._home_xy = (0.0, 0.0)
    curiosity._last_odom = PoseStamped(position=Vector3(4.0, 0.0, 0.0))
    curiosity._returning_home = False
    curiosity._home_goal_sent_at = 0.0
    curiosity._breadcrumbs = []
    curiosity._return_waypoint_index = None
    curiosity._return_waypoint_xy = None
    curiosity._return_waypoint_retries = 0
    curiosity._return_route_blocked = False
    curiosity._home_arrived = False
    curiosity._battery_soc = 5
    curiosity._low_battery_liedown = False
    curiosity._connection = SimpleNamespace(liedown=lambda: pytest.fail("too early"))
    goals: list[PoseStamped] = []
    curiosity._navigation = SimpleNamespace(
        set_goal=lambda goal: goals.append(goal) or True,
        cancel_goal=lambda: True,
    )
    curiosity._interrupt_dog_expression = lambda _reason: None
    curiosity._stop_exploration = lambda *_args, **_kwargs: None
    curiosity._stop_patrol = lambda *_args, **_kwargs: None
    curiosity._sensors_ready = lambda _now: True
    curiosity._publish_stop = lambda: None
    activities: list[tuple] = []
    curiosity._set_activity = lambda *args: activities.append(args)

    curiosity._return_home(100.0, NavigationState.IDLE)

    assert len(goals) == 1
    assert goals[0].position.x == 0.0 and goals[0].position.y == 0.0
    assert curiosity._returning_home is True
    assert activities[-1][0] is BehaviorKind.RETURN_HOME

    liedown: list[bool] = []
    curiosity._connection = SimpleNamespace(
        liedown=lambda: liedown.append(True) or True
    )
    curiosity._last_odom = PoseStamped(position=Vector3(0.2, 0.1, 0.0))
    curiosity._return_home(101.0, NavigationState.FOLLOWING_PATH)
    curiosity._return_home(102.0, NavigationState.IDLE)
    assert liedown == [True]
    assert curiosity._home_arrived is True
    assert activities[-1][-1] == "BATTERY_HOME"


def test_return_home_retraces_nearby_breadcrumbs_in_reverse() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(
        return_home_waypoint_radius_m=0.3,
        return_home_waypoint_max_m=1.25,
    )
    curiosity._breadcrumbs = [
        (0.0, 0.0),
        (1.0, 0.0),
        (2.0, 0.0),
        (3.0, 0.0),
        (4.0, 0.0),
    ]
    curiosity._return_waypoint_index = None
    curiosity._return_waypoint_xy = None
    curiosity._return_waypoint_retries = 0

    first = curiosity._next_return_waypoint((4.1, 0.0))
    assert first == (3.0, 0.0)
    assert curiosity._return_waypoint_index == 3
    assert math.hypot(first[0] - 4.1, first[1]) <= 1.25

    second = curiosity._next_return_waypoint((3.05, 0.0))
    assert second == (2.0, 0.0)
    assert curiosity._return_waypoint_index == 2


def test_breadcrumb_route_persists_and_loads_across_processes(tmp_path) -> None:
    route_path = tmp_path / "breadcrumbs.json"
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(
        breadcrumb_state_path=str(route_path),
        breadcrumb_spacing_m=0.75,
        breadcrumb_save_s=0.0,
    )
    curiosity._home_xy = (0.0, 0.0)
    curiosity._breadcrumbs = [(0.0, 0.0)]
    curiosity._battery_soc = 80
    curiosity._returning_home = False
    curiosity._explicit_hold_reason = None
    curiosity._last_breadcrumb_save_at = 0.0

    curiosity._record_breadcrumb_locked(
        PoseStamped(position=Vector3(0.3, 0.0, 0.0)), 1.0
    )
    curiosity._record_breadcrumb_locked(
        PoseStamped(position=Vector3(0.8, 0.0, 0.0)), 2.0
    )

    assert curiosity._breadcrumbs == [(0.0, 0.0), (0.8, 0.0)]
    loaded = curiosity._load_breadcrumb_state()
    assert loaded == curiosity._breadcrumbs


def test_operator_lie_and_stand_are_explicit_persistent_postures(
    monkeypatch,
) -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._lock = RLock()
    curiosity._enabled = True
    curiosity._explicit_hold_reason = None
    curiosity._preempt_requested = False
    curiosity._operator_liedown = False
    curiosity._returning_home = False
    curiosity._home_arrived = False
    curiosity._retry_not_before = 0.0
    curiosity._last_motion_ts = None
    curiosity._connection = object()

    message = curiosity.lie_down_until_resumed()
    assert "remain held" in message
    assert curiosity._explicit_hold_reason == "OPERATOR_LYING_DOWN"
    assert curiosity._preempt_requested is True

    monkeypatch.setattr(
        "nightwatch.curiosity.ensure_motion_ready", lambda *_args, **_kwargs: True
    )
    message = curiosity.stand_up_and_resume()
    assert "standing" in message
    assert curiosity._explicit_hold_reason is None
    assert curiosity._operator_liedown is False


def test_operator_lie_hold_overrides_low_battery_return_motion() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._lock = RLock()
    curiosity._leases = {}
    curiosity._enabled = True
    curiosity._explicit_hold_reason = "OPERATOR_LYING_DOWN"
    curiosity._preempt_requested = False
    curiosity._movement_intent_tool = None
    curiosity._movement_intent_at = 0.0
    curiosity._agent_busy = False
    curiosity._exploring = False
    curiosity._patrolling = False
    curiosity._battery_soc = 4
    curiosity._refresh_battery = lambda _now: None
    curiosity._is_following = lambda: False
    curiosity._navigation_state = lambda: NavigationState.IDLE
    calls: list[str] = []
    curiosity._hold = lambda reason: calls.append(reason)
    curiosity._return_home = lambda *_args: pytest.fail(
        "operator posture hold must prevent return-home motion"
    )

    curiosity._tick()

    assert calls == ["OPERATOR_LYING_DOWN"]


def test_skin_cover_fraction_and_sustained_hand_trigger() -> None:
    background = Image(
        data=np.full((120, 160, 3), (30, 50, 180), dtype=np.uint8),
        format=ImageFormat.RGB,
    )
    palm = Image(
        data=np.full((120, 160, 3), (190, 125, 90), dtype=np.uint8),
        format=ImageFormat.RGB,
    )
    assert _skin_cover_fraction(background) < 0.1
    assert _skin_cover_fraction(palm) > 0.9

    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(
        hand_cover_sample_s=0.0,
        hand_cover_sustain_frames=3,
        hand_cover_idle_s=0.0,
    )
    curiosity._lock = RLock()
    curiosity._last_hand_scan_at = 0.0
    curiosity._hand_cover_baseline = None
    curiosity._hand_cover_fraction = 0.0
    curiosity._hand_cover_streak = 0
    curiosity._hand_cover_latched = False
    curiosity._hand_cover_idle_since = 1.0
    curiosity._hand_scan_navigation_idle = True
    curiosity._hand_gesture_pending_at = 0.0
    curiosity._hand_gesture_cooldown_until = 0.0
    curiosity._on_image(background)
    curiosity._on_image(palm)
    curiosity._on_image(palm)
    assert curiosity._hand_gesture_pending_at == 0.0
    curiosity._on_image(palm)
    assert curiosity._hand_gesture_pending_at > 0.0
    assert curiosity._hand_cover_latched is True


def test_hand_gesture_waits_for_natural_navigation_idle() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(hand_gesture_max_wait_s=30.0)
    curiosity._dog_expression_name = None
    curiosity._hand_gesture_pending_at = 95.0
    curiosity._last_dog_expression = None
    started: list[str] = []
    stops: list[tuple[str, bool]] = []
    curiosity._stop_exploration = (
        lambda reason, cancel_goal: stops.append((reason, cancel_goal))
    )
    curiosity._publish_stop = lambda: None
    curiosity._start_dog_expression = (
        lambda name, _duration, _now: started.append(name) or True
    )

    assert (
        curiosity._maybe_hand_expression(100.0, NavigationState.FOLLOWING_PATH) is False
    )
    assert started == []
    assert curiosity._hand_gesture_pending_at == 95.0

    assert curiosity._maybe_hand_expression(101.0, NavigationState.IDLE) is True
    assert len(started) == 1
    assert stops == [("hand-cover expression", True)]
    assert curiosity._hand_gesture_pending_at == 0.0


def test_human_input_does_not_stop_curiosity() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._preempt_requested = False
    curiosity._on_human_input("hello")
    assert curiosity._preempt_requested is False


def test_only_actual_movement_tool_intent_preempts_curiosity() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._lock = RLock()
    curiosity._preempt_requested = False
    curiosity._movement_intent_at = 0.0
    curiosity._movement_intent_tool = None

    from langchain_core.messages import AIMessage

    curiosity._on_agent(AIMessage(content="thinking"))
    assert curiosity._preempt_requested is False
    curiosity._on_agent(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "relative_move",
                    "args": {"forward": 1},
                    "id": "move-1",
                }
            ],
        )
    )
    assert curiosity._preempt_requested is True
    assert curiosity._movement_intent_tool == "relative_move"


def test_supervisor_base_motion_start_does_not_preempt_itself() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._lock = RLock()
    curiosity._preempt_requested = False
    curiosity._movement_intent_at = 0.0
    curiosity._movement_intent_tool = None

    from langchain_core.messages import AIMessage

    for tool_name in ("begin_exploration", "start_patrol"):
        curiosity._on_agent(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool_name,
                        "args": {},
                        "id": f"base-{tool_name}",
                    }
                ],
            )
        )

    assert curiosity._preempt_requested is False
    assert curiosity._movement_intent_tool is None


def test_behavior_lease_priority_and_expiry() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._lock = RLock()
    curiosity._leases = {}
    curiosity._preempt_requested = False

    low = curiosity.acquire_behavior("patrol", "patrol", 20, 30.0, "coverage")
    high = curiosity.acquire_behavior("operator", "manual", 100, 30.0, "teleop")
    refused = curiosity.acquire_behavior("patrol2", "patrol", 10, 30.0, "coverage")

    assert low["accepted"] is True
    assert high["accepted"] is True
    assert refused["accepted"] is False
    top = curiosity._top_lease_locked()
    assert isinstance(top, BehaviorLease)
    assert top.behavior is BehaviorKind.MANUAL


def test_web_camera_retains_capture_timestamp_through_jpeg_encoding() -> None:
    interface = object.__new__(LatestFrameRobotWebInterface)
    capture_ts = 1234.5

    jpeg, encoded_ts = interface.process_frame_fastapi(
        (np.zeros((8, 8, 3), dtype=np.uint8), capture_ts)
    )

    assert jpeg.startswith(b"\xff\xd8")
    assert encoded_ts == capture_ts


def test_web_camera_clients_do_not_cancel_each_other() -> None:
    interface = object.__new__(LatestFrameRobotWebInterface)
    frames: Subject[tuple[bytes, float]] = Subject()
    interface.active_streams = {"camera": frames}
    interface.disposables = SimpleNamespace(add=lambda _disposable: None)

    first = interface.stream_generator("camera")()
    second = interface.stream_generator("camera")()
    first_result: Queue[bytes] = Queue()
    second_result: Queue[bytes] = Queue()
    first_reader = Thread(target=lambda: first_result.put(next(first)))
    second_reader = Thread(target=lambda: second_result.put(next(second)))
    first_reader.start()
    second_reader.start()

    deadline = time.monotonic() + 1.0
    while len(frames.observers) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(frames.observers) == 2

    frames.on_next((b"jpeg-one", time.time()))
    first_reader.join(timeout=1.0)
    second_reader.join(timeout=1.0)
    assert b"jpeg-one" in first_result.get_nowait()
    assert b"jpeg-one" in second_result.get_nowait()

    first.close()
    assert len(frames.observers) == 1

    next_result: Queue[bytes] = Queue()
    next_reader = Thread(target=lambda: next_result.put(next(second)))
    next_reader.start()
    frames.on_next((b"jpeg-two", time.time()))
    next_reader.join(timeout=1.0)
    assert b"jpeg-two" in next_result.get_nowait()
    second.close()


def test_operator_status_reports_robot_and_camera_state() -> None:
    operator = object.__new__(NightwatchWebInput)
    operator._curiosity = SimpleNamespace(
        curiosity_status=lambda: {
            "behavior": "explore",
            "owner": "curiosity",
            "battery_soc": 77,
            "map_phase": "EXPLORING",
        }
    )
    operator._navigation = SimpleNamespace(
        get_state=lambda: NavigationState.FOLLOWING_PATH
    )
    operator._last_camera_capture_ts = time.time() - 0.05

    status = operator._operator_status()

    assert status["behavior"] == "explore"
    assert status["battery_soc"] == 77
    assert status["navigation"] == NavigationState.FOLLOWING_PATH.value
    assert 0 <= status["camera_age_ms"] < 1000


def test_cached_operator_camera_age_keeps_advancing_when_refresh_blocks() -> None:
    capture_ts = 10_000.0
    cached = {
        "behavior": "explore",
        "camera_age_ms": 9733.0,
        "_camera_capture_ts": capture_ts,
    }

    first = _project_cached_status(
        cached,
        50.0,
        now_monotonic=55.0,
        now_wall=capture_ts + 14.733,
    )
    later = _project_cached_status(
        cached,
        50.0,
        now_monotonic=58.0,
        now_wall=capture_ts + 17.733,
    )

    assert first["camera_age_ms"] == pytest.approx(14_733.0)
    assert later["camera_age_ms"] == pytest.approx(17_733.0)
    assert later["status_age_ms"] == pytest.approx(8_000.0)
    assert later["status_stale"] is True
    assert "_camera_capture_ts" not in later
    # Projection operates on a copy; repeated HTTP reads do not age the cache
    # in place and therefore cannot double-count elapsed time.
    assert cached["camera_age_ms"] == 9733.0


def test_legacy_cached_camera_age_advances_by_snapshot_age() -> None:
    status = _project_cached_status(
        {"camera_age_ms": 9733.0},
        100.0,
        now_monotonic=104.0,
        now_wall=20_000.0,
    )

    assert status["camera_age_ms"] == pytest.approx(13_733.0)
    assert status["status_stale"] is True


def test_timed_out_status_refresh_allows_a_new_generation() -> None:
    interface = object.__new__(LatestFrameRobotWebInterface)
    interface._operator_status = lambda: {
        "behavior": "patrol",
        "camera_age_ms": 20.0,
    }
    interface._status_lock = RLock()
    interface._status_cache = {"behavior": "explore", "camera_age_ms": 10.0}
    interface._status_cache_at = time.monotonic() - 10.0
    interface._status_refreshing = True
    interface._status_refresh_started_at = time.monotonic() - 10.0
    interface._status_refresh_generation = 1
    interface._status_abandoned_generations = set()
    interface._latest_stream_capture_ts = time.time() - 0.05

    status, ready = interface._cached_operator_status()
    deadline = time.monotonic() + 1.0
    while interface._status_refreshing and time.monotonic() < deadline:
        time.sleep(0.005)

    assert ready is True
    assert status["status_stale"] is True
    assert 0.0 <= status["camera_age_ms"] < 500.0
    assert interface._status_refresh_generation == 2
    assert interface._status_cache["behavior"] == "patrol"
    assert interface._status_refreshing is False


def test_curiosity_rejects_rolling_information_delta_as_map_completion() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._last_exploration_status_at = 0.0
    curiosity._map_phase = "EXPLORING"
    curiosity._exploring = True
    curiosity._explore = SimpleNamespace(
        exploration_status=lambda: {
            "active": False,
            "completion_reason": "NO_INFORMATION_GAIN",
            "explored_goals": 9,
        }
    )

    curiosity._refresh_map_phase(2.0)

    assert curiosity._map_phase == "EXPLORING"
    assert curiosity._exploring is True


def test_curiosity_accepts_sustained_no_frontiers_as_map_completion() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._last_exploration_status_at = 0.0
    curiosity._map_phase = "EXPLORING"
    curiosity._exploring = True
    curiosity._explore = SimpleNamespace(
        exploration_status=lambda: {
            "active": False,
            "completion_reason": "NO_FRONTIERS",
            "map_complete": True,
            "explored_goals": 9,
        }
    )

    curiosity._refresh_map_phase(2.0)

    assert curiosity._map_phase == "MAPPED"
    assert curiosity._exploring is False


def test_curiosity_does_not_restart_before_observing_map_completion() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._map_phase = "EXPLORING"
    curiosity._exploring = True
    curiosity._exploration_capability_bound = True
    curiosity._prior_session_mapped = True
    curiosity._explore = SimpleNamespace(
        is_exploration_active=lambda: False,
        exploration_status=lambda: {
            "active": False,
            "completion_reason": "NO_FRONTIERS",
            "map_complete": True,
            "explored_goals": 6,
        },
    )
    saved: list[bool] = []
    curiosity._save_map_state = lambda: saved.append(True)

    curiosity._ensure_exploring(100.0)

    assert curiosity._map_phase == "MAPPED"
    assert curiosity._exploring is False
    assert curiosity._prior_session_mapped is False
    assert saved == [True]


def test_relocalization_missing_premap_disables_itself(monkeypatch) -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc.config = SimpleNamespace(map_file="/nonexistent/premap.pc2.lcm")
    reloc._premap = None
    calls: list[str] = []
    monkeypatch.setattr(
        "dimos.mapping.relocalization.module.RelocalizationModule.start",
        lambda _self: calls.append("start"),
    )

    reloc.start()

    assert reloc.config.map_file is None
    assert calls == ["start"]


def test_relocalization_status_reports_lock_and_exhaustion() -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc.config = SimpleNamespace(map_file="premap.pc2.lcm", max_failed_attempts=5)
    reloc._premap = object()
    reloc._alignment_locked = False
    reloc._failed_attempts = 5

    status = reloc.relocalization_status()

    assert status["configured"] is True
    assert status["locked"] is False
    assert status["exhausted"] is True

    reloc._alignment_locked = True
    assert reloc.relocalization_status()["exhausted"] is False


def test_mapped_sidecar_requires_explicit_trust_for_fast_start(tmp_path) -> None:
    state_path = tmp_path / "nightwatch_state.json"

    writer = object.__new__(CuriositySupervisor)
    writer.config = SimpleNamespace(map_state_path=str(state_path))
    writer._save_map_state()
    assert json.loads(state_path.read_text())["map_phase"] == "MAPPED"

    safe = object.__new__(CuriositySupervisor)
    safe.config = SimpleNamespace(map_state_path=str(state_path))
    assert safe._load_map_state() is False

    fast = object.__new__(CuriositySupervisor)
    fast.config = SimpleNamespace(
        map_state_path=str(state_path),
        trust_persisted_map_complete=True,
    )
    fast._prior_session_mapped = fast._load_map_state()
    assert fast._prior_session_mapped is True
    fast._map_phase = "EXPLORING"
    fast._last_exploration_status_at = 0.0
    fast._reloc = SimpleNamespace(
        relocalization_status=lambda: {
            "configured": True,
            "locked": True,
            "failed_attempts": 0,
            "exhausted": False,
        }
    )
    stops: list[tuple[str, bool]] = []
    fast._stop_exploration = lambda reason, *, cancel_goal: stops.append(
        (reason, cancel_goal)
    )

    fast._refresh_map_phase(2.0)

    assert fast._map_phase == "MAPPED"
    assert stops == [("premap relocalized", True)]


def test_exhausted_relocalization_falls_back_to_exploration(tmp_path) -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = SimpleNamespace(map_state_path=str(tmp_path / "missing.json"))
    curiosity._prior_session_mapped = True
    curiosity._map_phase = "EXPLORING"
    curiosity._last_exploration_status_at = 0.0
    curiosity._exploring = True
    curiosity._reloc = SimpleNamespace(
        relocalization_status=lambda: {
            "configured": True,
            "locked": False,
            "exhausted": True,
        }
    )
    curiosity._explore = SimpleNamespace(exploration_status=lambda: {"active": True})

    curiosity._refresh_map_phase(2.0)

    assert curiosity._map_phase == "EXPLORING"
    assert curiosity._prior_session_mapped is False


def test_visited_frontiers_are_never_reselected() -> None:
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.config = SimpleNamespace(
        occupancy_threshold=99,
        safe_distance=3.0,
        min_frontier_perimeter=0.5,
        max_explored_distance=10.0,
        lookahead_distance=5.0,
    )
    explorer._coverage_lock = RLock()
    explorer._goal_visits = [(0.8, 0.0, time.time()), (4.0, 0.0, time.time())]
    explorer._failure_zones = []
    explorer.exploration_direction = Vector3()
    costmap = OccupancyGrid(
        grid=np.zeros((200, 200), dtype=np.int8),
        resolution=0.05,
    )

    ranked = explorer._rank_frontiers(
        [Vector3(0.8, 0.0, 0.0), Vector3(4.0, 0.0, 0.0)],
        [20, 20],
        Vector3(0.0, 0.0, 0.0),
        costmap,
    )

    assert ranked == []


def test_near_unvisited_frontier_remains_reachable_at_boot() -> None:
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.config = SimpleNamespace(
        occupancy_threshold=99,
        safe_distance=3.0,
        min_frontier_perimeter=0.5,
        max_explored_distance=10.0,
        lookahead_distance=5.0,
    )
    explorer._coverage_lock = RLock()
    explorer._goal_visits = []
    explorer._failure_zones = []
    explorer.exploration_direction = Vector3()
    costmap = OccupancyGrid(
        grid=np.zeros((200, 200), dtype=np.int8),
        resolution=0.05,
    )

    ranked = explorer._rank_frontiers(
        [Vector3(0.8, 0.0, 0.0)],
        [20],
        Vector3(0.0, 0.0, 0.0),
        costmap,
    )

    assert len(ranked) == 1
    assert ranked[0].x == 0.8


def test_far_frontier_wins_on_equal_unknown_mass() -> None:
    # Premise flipped from the old near-beats-far test. The July 24 evening fix
    # replaced the proximity term with a range term: the user wants the dog
    # ranging outward to find people, so on equal unknown mass the FARTHER
    # frontier now wins the tiebreak.
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.config = SimpleNamespace(
        occupancy_threshold=99,
        safe_distance=3.0,
        min_frontier_perimeter=0.5,
        max_explored_distance=10.0,
        lookahead_distance=5.0,
    )
    explorer._coverage_lock = RLock()
    explorer._goal_visits = []
    explorer._failure_zones = []
    explorer.exploration_direction = Vector3()
    costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8),
        resolution=0.05,
    )

    ranked = explorer._rank_frontiers(
        [Vector3(9.0, 0.0, 0.0), Vector3(2.5, 0.0, 0.0)],
        [60, 15],
        Vector3(0.0, 0.0, 0.0),
        costmap,
    )

    # Equal unknown mass (all-known grid): the far frontier pushes outward.
    assert ranked[0].x == 9.0


def test_patrol_selects_farthest_reachable_candidate() -> None:
    # Fix 3: the wander must not orbit the start area. _select_spread_goal takes
    # the farthest reachable candidate so patrol spreads across the whole floor;
    # new coverage only breaks ties.
    router = object.__new__(NightwatchCoveragePatrolRouter)
    start = (0.0, 0.0)
    reachable = [
        (500, (0.5, 0.0)),  # near, high coverage
        (10, (8.0, 6.0)),  # far, low coverage
        (300, (2.0, 1.0)),  # mid
    ]
    assert router._select_spread_goal(reachable, start) == (8.0, 6.0)
    # Coverage breaks a distance tie.
    tied = [(5, (3.0, 0.0)), (99, (0.0, 3.0))]
    assert router._select_spread_goal(tied, start) == (0.0, 3.0)
    assert router._select_spread_goal([], start) is None


def test_patrol_spreads_away_from_recent_room_not_only_boot() -> None:
    router = object.__new__(NightwatchCoveragePatrolRouter)
    router._session_origin = (0.0, 0.0)
    router._recent_goals = [(9.0, 0.0)]

    selected = router._select_spread_goal(
        [
            (50, (9.0, 3.0)),  # same far-from-boot room
            (20, (1.0, 5.0)),  # different room, farther from recent goal
        ],
        start=(9.0, 0.0),
    )

    assert selected == (1.0, 5.0)


def test_patrol_next_goal_is_not_clustered_near_start(monkeypatch) -> None:
    # End-to-end over next_goal with A* stubbed reachable: with every safe cell a
    # candidate, the published goal is the far corner, never a near cell.
    monkeypatch.setattr(
        "nightwatch.navigation.min_cost_astar",
        lambda *a, **k: object(),  # always reachable
    )
    router = object.__new__(NightwatchCoveragePatrolRouter)
    router._lock = RLock()
    router._candidates_to_consider = 100_000  # consider every safe cell
    grid = np.zeros((20, 20), dtype=np.int8)
    occ = OccupancyGrid(grid=grid, resolution=0.05)
    router._occupancy_grid = occ
    router._costmap = occ
    router._safe_mask = np.ones((20, 20), dtype=bool)
    router._visitation = SimpleNamespace(visited=np.zeros((20, 20), dtype=bool))
    router._sampling_weights = np.ones((20, 20), dtype=np.float64)
    router._pose = PoseStamped(position=Vector3(0.0, 0.0, 0.0))
    router._count_new_coverage = lambda path, visited, occ, safe: 0

    goal = router.next_goal()
    assert goal is not None
    # Farthest cell from (0,0) is grid (19,19) -> world (0.95, 0.95).
    assert goal.position.x > 0.9 and goal.position.y > 0.9


def test_patrol_samples_inside_focus_area_before_candidate_budget(monkeypatch) -> None:
    focus = {
        "id": "focus",
        "kind": "keep_in",
        "points": [(15.0, 0.0), (19.0, 0.0), (19.0, 4.0), (15.0, 4.0)],
    }

    def reachable_path(_costmap, candidate, start, **_kwargs):
        return SimpleNamespace(
            poses=[
                PoseStamped(position=Vector3(*start, 0.0)),
                PoseStamped(position=Vector3(*candidate, 0.0)),
            ]
        )

    monkeypatch.setattr("nightwatch.navigation.min_cost_astar", reachable_path)
    # The old sample-before-zone-filter order chose the first eight cells in
    # the full map, all outside this far-away focus polygon, and returned None.
    monkeypatch.setattr(
        np.random,
        "choice",
        lambda population, size, replace, p: np.arange(size),
    )

    router = object.__new__(NightwatchCoveragePatrolRouter)
    router._lock = RLock()
    router._candidates_to_consider = 8
    grid = np.zeros((200, 200), dtype=np.int8)
    occ = OccupancyGrid(grid=grid, resolution=0.1)
    router._occupancy_grid = occ
    router._costmap = occ
    router._safe_mask = np.ones((200, 200), dtype=bool)
    router._visitation = SimpleNamespace(visited=np.zeros((200, 200), dtype=bool))
    router._sampling_weights = np.ones((200, 200), dtype=np.float64)
    router._pose = PoseStamped(position=Vector3(16.0, 2.0, 0.0))
    router._session_origin = (16.0, 2.0)
    router._origin_exited = False
    router._recent_goals = []
    router._persistent_keep_outs = []
    router._session_failure_zones = []
    router._operator_zones = [focus]
    router._count_new_coverage = lambda path, visited, occ, safe: 0

    goal = router.next_goal()

    assert goal is not None
    assert _point_obeys_operator_zones(
        goal.position.x,
        goal.position.y,
        [focus],
    )


def test_patrol_always_tests_local_legal_exits_when_weighted_sample_is_unreachable(
    monkeypatch,
) -> None:
    def locally_reachable(_costmap, candidate, start, **_kwargs):
        if math.hypot(candidate[0] - start[0], candidate[1] - start[1]) > 0.5:
            return None
        return SimpleNamespace(
            poses=[
                PoseStamped(position=Vector3(*start, 0.0)),
                PoseStamped(position=Vector3(*candidate, 0.0)),
            ]
        )

    monkeypatch.setattr("nightwatch.navigation.min_cost_astar", locally_reachable)
    # Force the weighted sample into the far corner. Before the local fallback,
    # this returned None forever even though safe cells beside the robot were
    # reachable.
    monkeypatch.setattr(
        np.random,
        "choice",
        lambda population, size, replace, p: np.arange(size),
    )

    router = object.__new__(NightwatchCoveragePatrolRouter)
    router._lock = RLock()
    router._candidates_to_consider = 2
    router._local_candidates_to_consider = 8
    grid = np.zeros((20, 20), dtype=np.int8)
    occ = OccupancyGrid(grid=grid, resolution=0.1)
    router._occupancy_grid = occ
    router._costmap = occ
    router._safe_mask = np.ones((20, 20), dtype=bool)
    router._visitation = SimpleNamespace(visited=np.zeros((20, 20), dtype=bool))
    router._sampling_weights = np.ones((20, 20), dtype=np.float64)
    router._pose = PoseStamped(position=Vector3(1.0, 1.0, 0.0))
    router._session_origin = (1.0, 1.0)
    router._origin_exited = False
    router._recent_goals = []
    router._persistent_keep_outs = []
    router._session_failure_zones = []
    router._operator_zones = []
    router._count_new_coverage = lambda path, visited, occ, safe: 0

    goal = router.next_goal()

    assert goal is not None
    assert math.hypot(goal.position.x - 1.0, goal.position.y - 1.0) <= 0.5


def test_patrol_can_make_outward_progress_when_booted_inside_keep_out(
    monkeypatch,
) -> None:
    def reachable_path(_costmap, candidate, start, **_kwargs):
        return SimpleNamespace(
            poses=[
                PoseStamped(position=Vector3(*start, 0.0)),
                PoseStamped(position=Vector3(*candidate, 0.0)),
            ]
        )

    monkeypatch.setattr("nightwatch.navigation.min_cost_astar", reachable_path)
    monkeypatch.setattr(
        np.random,
        "choice",
        lambda population, size, replace, p: np.arange(size),
    )
    router = object.__new__(NightwatchCoveragePatrolRouter)
    router._lock = RLock()
    router._candidates_to_consider = 32
    grid = np.zeros((50, 50), dtype=np.int8)
    occ = OccupancyGrid(grid=grid, resolution=0.1)
    router._occupancy_grid = occ
    router._costmap = occ
    router._safe_mask = np.ones((50, 50), dtype=bool)
    router._visitation = SimpleNamespace(visited=np.zeros((50, 50), dtype=bool))
    router._sampling_weights = np.ones((50, 50), dtype=np.float64)
    router._pose = PoseStamped(position=Vector3(2.0, 2.0, 0.0))
    router._session_origin = (2.0, 2.0)
    router._origin_exited = False
    router._recent_goals = []
    # Every known cell is still inside this exclusion. The router must choose
    # an intermediate endpoint farther from its center instead of deadlocking.
    router._persistent_keep_outs = [(2.0, 2.0, 5.0)]
    router._session_failure_zones = []
    router._operator_zones = []
    router._count_new_coverage = lambda path, visited, occ, safe: 0

    goal = router.next_goal()

    assert goal is not None
    assert math.hypot(goal.position.x - 2.0, goal.position.y - 2.0) > 0.0


def test_patrol_booted_in_keep_out_takes_shortest_complete_astar_exit(
    monkeypatch,
) -> None:
    def reachable_path(_costmap, candidate, start, **_kwargs):
        return SimpleNamespace(
            poses=[
                PoseStamped(position=Vector3(*start, 0.0)),
                PoseStamped(position=Vector3(*candidate, 0.0)),
            ]
        )

    monkeypatch.setattr("nightwatch.navigation.min_cost_astar", reachable_path)
    router = object.__new__(NightwatchCoveragePatrolRouter)
    router._lock = RLock()
    grid = np.zeros((50, 50), dtype=np.int8)
    occ = OccupancyGrid(grid=grid, resolution=0.1)
    router._occupancy_grid = occ
    router._costmap = occ
    router._safe_mask = np.ones((50, 50), dtype=bool)
    router._visitation = SimpleNamespace(visited=np.zeros((50, 50), dtype=bool))
    router._sampling_weights = np.ones((50, 50), dtype=np.float64)
    router._pose = PoseStamped(position=Vector3(2.0, 2.0, 0.0))
    router._session_origin = (2.0, 2.0)
    router._origin_exited = False
    router._recent_goals = []
    router._persistent_keep_outs = [(2.0, 2.0, 0.25)]
    router._session_failure_zones = []
    router._operator_zones = []
    router._count_new_coverage = lambda path, visited, occ, safe: 0

    goal = router.next_goal()

    assert goal is not None
    exit_distance = math.hypot(goal.position.x - 2.0, goal.position.y - 2.0)
    # The exit must clear the circle by MORE than the local planner's 0.2 m
    # arrival tolerance (goal clearance 0.45 m). A goal just past the boundary
    # let the dog "arrive" while its center was still inside the circle, which
    # re-triggered egress forever (live standstill, 2026-07-25).
    assert 0.25 + 0.45 < exit_distance <= 1.0


def test_patrol_uses_one_cell_egress_when_startup_safe_pocket_is_tiny(
    monkeypatch,
) -> None:
    def reachable_path(_costmap, candidate, start, **_kwargs):
        return SimpleNamespace(
            poses=[
                PoseStamped(position=Vector3(*start, 0.0)),
                PoseStamped(position=Vector3(*candidate, 0.0)),
            ]
        )

    monkeypatch.setattr("nightwatch.navigation.min_cost_astar", reachable_path)
    router = object.__new__(NightwatchCoveragePatrolRouter)
    router._lock = RLock()
    grid = np.zeros((50, 50), dtype=np.int8)
    occ = OccupancyGrid(grid=grid, resolution=0.1)
    router._occupancy_grid = occ
    router._costmap = occ
    router._safe_mask = np.zeros((50, 50), dtype=bool)
    # The first LiDAR update has only revealed the boot cell and one safe cell
    # in the outward direction. A fixed 20 cm egress requirement deadlocks.
    router._safe_mask[20, 20] = True
    router._safe_mask[20, 21] = True
    router._visitation = SimpleNamespace(visited=np.zeros((50, 50), dtype=bool))
    router._sampling_weights = np.ones((50, 50), dtype=np.float64)
    router._pose = PoseStamped(position=Vector3(2.0, 2.0, 0.0))
    router._session_origin = (2.0, 2.0)
    router._origin_exited = False
    router._recent_goals = []
    router._persistent_keep_outs = [(2.0, 2.0, 5.0)]
    router._session_failure_zones = []
    router._operator_zones = []
    router._count_new_coverage = lambda path, visited, occ, safe: 0

    goal = router.next_goal()

    assert goal is not None
    assert goal.position.x > 2.0
    assert goal.position.y == pytest.approx(2.0)


def test_egress_near_boundary_never_selects_goal_within_arrival_tolerance(
    monkeypatch,
) -> None:
    # Live standstill 2026-07-25: the intermediate egress accepted any cell a
    # hair farther from the circle center, so outward hops shrank below the
    # local planner's 0.2 m goal tolerance. The dog then "arrived" without
    # moving, was still inside the circle, and re-picked the same goal forever,
    # frozen 0.13 m before freedom. Near the boundary the router must command
    # a real step that clears the circle entirely.
    def reachable_path(_costmap, candidate, start, **_kwargs):
        return SimpleNamespace(
            poses=[
                PoseStamped(position=Vector3(*start, 0.0)),
                PoseStamped(position=Vector3(*candidate, 0.0)),
            ]
        )

    monkeypatch.setattr("nightwatch.navigation.min_cost_astar", reachable_path)
    router = object.__new__(NightwatchCoveragePatrolRouter)
    router._lock = RLock()
    grid = np.zeros((50, 50), dtype=np.int8)
    occ = OccupancyGrid(grid=grid, resolution=0.1)
    router._occupancy_grid = occ
    router._costmap = occ
    router._safe_mask = np.ones((50, 50), dtype=bool)
    router._visitation = SimpleNamespace(visited=np.zeros((50, 50), dtype=bool))
    router._sampling_weights = np.ones((50, 50), dtype=np.float64)
    # 0.1 m inside the boundary of a 1.0 m circle.
    router._pose = PoseStamped(position=Vector3(1.9, 2.0, 0.0))
    router._session_origin = (1.9, 2.0)
    router._origin_exited = False
    router._recent_goals = []
    router._persistent_keep_outs = [(1.0, 2.0, 1.0)]
    router._session_failure_zones = []
    router._operator_zones = []
    router._count_new_coverage = lambda path, visited, occ, safe: 0

    goal = router.next_goal()

    assert goal is not None
    distance_from_center = math.hypot(goal.position.x - 1.0, goal.position.y - 2.0)
    step_from_robot = math.hypot(goal.position.x - 1.9, goal.position.y - 2.0)
    # Fully clears the circle with margin beyond the arrival tolerance…
    assert distance_from_center > 1.0 + 0.45
    # …and commands real motion (beyond the 0.2 m no-motion arrival band).
    assert step_from_robot > 0.2


def test_far_high_unknown_frontier_outranks_near_low_unknown() -> None:
    # Was test_far_frontiers_are_visited_nearest_first. Nearest-first travel is
    # obsolete: the clean tier is now a single score-sorted list dominated by
    # unknown mass, so a far frontier that opens a large unmapped region beats a
    # nearer, bigger one in mostly-known space.
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.config = SimpleNamespace(
        occupancy_threshold=99,
        safe_distance=3.0,
        min_frontier_perimeter=0.5,
        max_explored_distance=10.0,
        lookahead_distance=5.0,
    )
    explorer._coverage_lock = RLock()
    explorer._goal_visits = []
    explorer._failure_zones = []
    explorer.exploration_direction = Vector3()
    grid = np.zeros((400, 400), dtype=np.int8)
    # A large unknown region wrapping the far frontier at (9.0, 5.0): grid cell
    # (col 180, row 100), so its 2 m box is almost entirely unmapped. The near
    # frontier at (2.5, 5.0) sits in fully-known space.
    grid[60:140, 140:220] = -1
    costmap = OccupancyGrid(grid=grid, resolution=0.05)

    ranked = explorer._rank_frontiers(
        [Vector3(9.0, 5.0, 0.0), Vector3(2.5, 5.0, 0.0)],
        [15, 60],
        Vector3(0.0, 5.0, 0.0),
        costmap,
    )

    # The far frontier opens a room; the nearer, bigger one only nibbles known
    # space, so the far one leads despite the extra travel.
    assert [frontier.x for frontier in ranked] == [9.0, 2.5]


def test_unknown_mass_dominates_ranking() -> None:
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.config = SimpleNamespace(
        occupancy_threshold=99,
        safe_distance=3.0,
        min_frontier_perimeter=0.5,
        max_explored_distance=10.0,
        lookahead_distance=5.0,
    )
    explorer._coverage_lock = RLock()
    explorer._goal_visits = []
    explorer._failure_zones = []
    explorer.exploration_direction = Vector3()
    grid = np.zeros((400, 400), dtype=np.int8)
    # Equal frontiers except for what surrounds them. The far one at (8.0, 5.0)
    # = cell (160, 100) is wrapped in a large unknown region; the near one at
    # (2.0, 5.0) sits in fully-known space. Sizes are equal, so unknown mass is
    # the only differentiator.
    grid[60:140, 120:200] = -1
    costmap = OccupancyGrid(grid=grid, resolution=0.05)

    ranked = explorer._rank_frontiers(
        [Vector3(2.0, 5.0, 0.0), Vector3(8.0, 5.0, 0.0)],
        [20, 20],
        Vector3(0.0, 5.0, 0.0),
        costmap,
    )

    # The unknown-adjacent frontier ranks first even though it is farther away.
    assert ranked[0].x == 8.0
    assert [frontier.x for frontier in ranked] == [8.0, 2.0]


def test_integral_image_unknown_score_endpoints() -> None:
    # A centroid in a fully-unknown 2 m box scores ~1.0; one in fully-known
    # space scores 0.0. Exercises _build_unknown_mass + _unknown_mass_score, the
    # integral-image box sum that drives the new ranking.
    explorer = object.__new__(WavefrontFrontierExplorer)
    unknown = OccupancyGrid(
        grid=np.full((200, 200), -1, dtype=np.int8), resolution=0.05
    )
    known = OccupancyGrid(grid=np.zeros((200, 200), dtype=np.int8), resolution=0.05)
    # Cell (100, 100): its 2 m box (40-cell half-side) lies fully inside a
    # 200x200 grid, so no edge clipping muddies the endpoints.
    center = Vector3(5.0, 5.0, 0.0)
    grid_pos = unknown.world_to_grid(center)
    gx, gy = int(grid_pos.x), int(grid_pos.y)

    integral_u, half_u, box_u = explorer._build_unknown_mass(unknown)
    score_unknown = explorer._unknown_mass_score(
        integral_u, half_u, box_u, gx, gy, unknown.width, unknown.height
    )
    integral_k, half_k, box_k = explorer._build_unknown_mass(known)
    score_known = explorer._unknown_mass_score(
        integral_k, half_k, box_k, gx, gy, known.width, known.height
    )

    assert score_unknown == 1.0
    assert score_known == 0.0


class _FakeScan:
    """Special methods are looked up on the type, so SimpleNamespace can't
    fake __len__."""

    pointcloud = object()

    def __len__(self) -> int:
        return 60_000


class _BoundedMatureScan:
    """The responsive rolling mapper plateaus below the stock 50k gate."""

    def __len__(self) -> int:
        return 38_000


def test_nightwatch_relocalization_accepts_bounded_mature_cloud() -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc._last_skip_log = 0.0

    assert reloc._has_enough_points(_BoundedMatureScan()) is True


def test_nightwatch_relocalization_gate_opens_below_bounded_map_plateau() -> None:
    reloc = object.__new__(NightwatchRelocalization)

    assert reloc._has_enough_points([None] * 28_970) is True
    assert reloc._has_enough_points([None] * 24_999) is False


def test_relocalization_state_atomic_write_is_safe_for_concurrent_publishers(
    tmp_path,
) -> None:
    path = tmp_path / "relocalization.json"
    start = Event()
    errors: Queue[BaseException] = Queue()

    def write_state(index: int) -> None:
        start.wait()
        try:
            _atomic_write_json(path, {"writer": index})
        except BaseException as exc:
            errors.put(exc)

    writers = [Thread(target=write_state, args=(index,)) for index in range(12)]
    for writer in writers:
        writer.start()
    start.set()
    for writer in writers:
        writer.join(timeout=2.0)

    assert errors.empty()
    assert json.loads(path.read_text())["writer"] in range(12)
    assert list(tmp_path.glob("*.tmp")) == []


def _persisted_relocalization_fixture(
    tmp_path,
    *,
    epoch: str = "same-epoch",
    map_digest: str = "map-sha",
    now: float = 1_000.0,
) -> NightwatchRelocalization:
    map_path = (tmp_path / "venue" / "nightwatch_map.pc2.lcm").resolve()
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"map")
    epoch_path = tmp_path / "venue" / "odom_epoch.json"
    epoch_path.write_text(
        json.dumps(
            {
                "epoch_id": epoch,
                "updated_at": now - 1.0,
                "last_pose": [4.0, -2.0],
            }
        )
    )
    state_path = tmp_path / "venue" / "relocalization.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": now - 10.0,
                "map_path": str(map_path),
                "map_sha256": map_digest,
                "odom_epoch": epoch,
                "odom_pose": [3.5, -2.0],
                "world_to_map": {"x": 10.0, "y": 5.0, "yaw": math.pi / 2.0},
            }
        )
    )
    reloc = object.__new__(NightwatchRelocalization)
    reloc._premap_path = map_path
    reloc._premap_digest = map_digest
    reloc._odom_epoch_path = epoch_path
    reloc._alignment_state_path = state_path
    reloc._restore_rejection = None
    reloc._alignment_locked = False
    reloc._alignment_persisted = False
    reloc._accepted_world_to_map = None
    reloc._failed_attempts = 4
    reloc._latest_odom_pose = (4.1, -2.1)
    reloc._premap = None
    reloc._published_test_tfs = []
    reloc._world_to_map = SimpleNamespace(
        on_next=lambda tf: reloc._published_test_tfs.append(tf)
    )
    return reloc


def test_matching_odom_epoch_restores_exact_venue_alignment_immediately(
    tmp_path,
) -> None:
    reloc = _persisted_relocalization_fixture(tmp_path)

    assert reloc._try_restore_accepted_alignment((4.1, -2.1), now=1_000.0)
    assert reloc._alignment_locked is True
    assert reloc._failed_attempts == 0
    assert len(reloc._published_test_tfs) == 1
    assert reloc.world_to_map_2d() == pytest.approx(
        {"x": 10.0, "y": 5.0, "yaw": math.pi / 2.0}
    )


@pytest.mark.parametrize("changed", ["epoch", "map_path", "map_digest", "pose"])
def test_saved_alignment_rejects_wrong_epoch_map_or_discontinuous_pose(
    tmp_path,
    changed: str,
) -> None:
    reloc = _persisted_relocalization_fixture(tmp_path)
    pose = (4.1, -2.1)
    if changed == "epoch":
        epoch = json.loads(reloc._odom_epoch_path.read_text())
        epoch["epoch_id"] = "new-epoch"
        reloc._odom_epoch_path.write_text(json.dumps(epoch))
    elif changed == "map_path":
        reloc._premap_path = (tmp_path / "other-map.pc2.lcm").resolve()
    elif changed == "map_digest":
        reloc._premap_digest = "changed-map-sha"
    else:
        pose = (20.0, 20.0)

    assert not reloc._try_restore_accepted_alignment(pose, now=1_000.0)
    assert reloc._alignment_locked is False
    assert reloc._published_test_tfs == []


def test_relocalization_never_accepts_ambiguous_planar_match(monkeypatch) -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc.config = SimpleNamespace(max_failed_attempts=12)
    reloc._premap = SimpleNamespace(pointcloud=object())
    reloc._failed_attempts = 0
    reloc._alignment_locked = False
    monkeypatch.setattr(
        "nightwatch.memory.relocalize_planar",
        lambda _premap, _scan: None,
    )

    first = reloc._try_relocalize(_FakeScan())
    assert first is None
    assert reloc._failed_attempts == 1

    second = reloc._try_relocalize(_FakeScan())
    assert second is None
    assert reloc._failed_attempts == 2
    assert reloc._alignment_locked is False


def test_relocalization_ambiguous_retry_stays_live_map_only(monkeypatch) -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc.config = SimpleNamespace(max_failed_attempts=12)
    reloc._failed_attempts = 0
    reloc._alignment_locked = False
    reloc._premap = SimpleNamespace(pointcloud=object())
    monkeypatch.setattr(
        "nightwatch.memory.relocalize_planar",
        lambda _premap, _scan: None,
    )

    assert reloc._try_relocalize(_FakeScan()) is None
    assert reloc._try_relocalize(_FakeScan()) is None
    assert reloc._alignment_locked is False
    assert reloc._failed_attempts == 2


def test_relocalization_does_not_spin_or_false_lock_on_repeated_rooms(
    monkeypatch,
) -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc.config = SimpleNamespace(max_failed_attempts=12)
    reloc._failed_attempts = 0
    reloc._alignment_locked = False
    reloc._premap = SimpleNamespace(pointcloud=object())
    monkeypatch.setattr(
        "nightwatch.memory.relocalize_planar",
        lambda _premap, _scan: None,
    )

    for _ in range(4):
        assert reloc._try_relocalize(_FakeScan()) is None

    # The inherited 45-second retry gate limits CPU use.  A later, more unique
    # live footprint still gets more chances instead of a special-case abort.
    assert reloc._failed_attempts == 4
    assert reloc._alignment_locked is False


def _synthetic_floor_cloud(structure_xy: np.ndarray) -> SimpleNamespace:
    layers = [
        np.column_stack(
            [structure_xy, np.full(len(structure_xy), height, dtype=float)]
        )
        for height in (0.8, 1.0, 1.2)
    ]
    x_min, y_min = structure_xy.min(axis=0) - 0.5
    x_max, y_max = structure_xy.max(axis=0) + 0.5
    floor_x, floor_y = np.meshgrid(
        np.linspace(x_min, x_max, 36),
        np.linspace(y_min, y_max, 36),
    )
    floor = np.column_stack(
        [floor_x.ravel(), floor_y.ravel(), np.zeros(floor_x.size)]
    )
    return SimpleNamespace(points=np.vstack([floor, *layers]))


def _asymmetric_room_points() -> np.ndarray:
    def segment(start: tuple[float, float], end: tuple[float, float]) -> np.ndarray:
        alpha = np.linspace(0.0, 1.0, 90)
        return (
            np.asarray(start)[None, :] * (1.0 - alpha[:, None])
            + np.asarray(end)[None, :] * alpha[:, None]
        )

    return np.vstack(
        [
            segment((0.0, 0.0), (7.0, 0.0)),
            segment((7.0, 0.0), (7.0, 3.0)),
            segment((7.0, 3.0), (4.5, 3.0)),
            segment((4.5, 3.0), (4.5, 6.0)),
            segment((4.5, 6.0), (0.0, 6.0)),
            segment((0.0, 6.0), (0.0, 0.0)),
            segment((1.2, 1.1), (3.8, 2.4)),
        ]
    )


def test_planar_relocalization_recovers_generic_asymmetric_room() -> None:
    saved_xy = _asymmetric_room_points() + np.array([9.0, -3.0])
    expected_yaw = math.radians(24.0)
    expected_translation = np.array([6.5, -4.25])
    rotation = np.array(
        [
            [math.cos(expected_yaw), -math.sin(expected_yaw)],
            [math.sin(expected_yaw), math.cos(expected_yaw)],
        ]
    )
    # saved = live @ R.T + translation
    live_xy = (saved_xy - expected_translation) @ rotation

    result = relocalize_planar(
        _synthetic_floor_cloud(saved_xy),
        _synthetic_floor_cloud(live_xy),
        yaw_step_deg=6.0,
        min_margin=0.005,
    )

    assert result is not None
    actual_yaw = math.atan2(result.transform[1, 0], result.transform[0, 0])
    yaw_error = math.atan2(
        math.sin(actual_yaw - expected_yaw),
        math.cos(actual_yaw - expected_yaw),
    )
    assert abs(yaw_error) < math.radians(3.0)
    assert np.linalg.norm(
        result.transform[:2, 3] - expected_translation
    ) < 0.35


def test_planar_relocalization_rejects_two_identical_rooms() -> None:
    room = _asymmetric_room_points()
    saved_xy = np.vstack([room, room + np.array([14.0, 0.0])])

    result = relocalize_planar(
        _synthetic_floor_cloud(saved_xy),
        _synthetic_floor_cloud(room),
        yaw_step_deg=6.0,
    )

    assert result is None


def test_wall_shadow_frontiers_are_discarded() -> None:
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.config = SimpleNamespace(
        occupancy_threshold=99,
        safe_distance=3.0,
        min_frontier_perimeter=0.5,
        max_explored_distance=10.0,
        lookahead_distance=5.0,
    )
    explorer._coverage_lock = RLock()
    explorer._goal_visits = []
    explorer._failure_zones = []
    explorer.exploration_direction = Vector3()
    grid = np.zeros((200, 200), dtype=np.int8)
    # A wall along x=3.0 m; the sliver frontier hugs it at 0.15 m clearance.
    grid[:, 60] = 100
    costmap = OccupancyGrid(grid=grid, resolution=0.05)

    ranked = explorer._rank_frontiers(
        [Vector3(2.85, 5.0, 0.0), Vector3(1.5, 5.0, 0.0)],
        [20, 20],
        Vector3(0.5, 5.0, 0.0),
        costmap,
    )

    # The corner-sniffing sliver is gone; the open-space frontier survives.
    assert [frontier.x for frontier in ranked] == [1.5]


def test_resolved_goal_is_abandoned_en_route() -> None:
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer._invalidate_radius_m = 0.9
    goal = Vector3(5.0, 5.0, 0.0)

    unknown = np.full((200, 200), -1, dtype=np.int8)
    resolved = np.zeros((200, 200), dtype=np.int8)
    unknown_map = OccupancyGrid(grid=unknown, resolution=0.05)
    resolved_map = OccupancyGrid(grid=resolved, resolution=0.05)

    assert explorer._unknown_cells_near(unknown_map, goal) > 0
    assert explorer._unknown_cells_near(resolved_map, goal) == 0


def test_abandoned_goal_wakes_exploration_loop() -> None:
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.goal_reached_event = Event()

    explorer._on_goal_reached(SimpleNamespace(data=False))

    # Stock ignored data=False, costing the full goal_timeout of dead
    # waiting after every planner give-up.
    assert explorer.goal_reached_event.is_set()


def test_explorer_stop_never_publishes_current_pose_or_strikes_stale_goal() -> None:
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer._exploration_lock = RLock()
    explorer.exploration_thread = None
    explorer.exploration_active = True
    explorer._completion_reason = None
    explorer._completed_at = None
    explorer.no_gain_counter = 4
    explorer.stop_event = Event()
    explorer.goal_reached_event = Event()
    explorer._active_goal = Vector3(8.0, 9.0, 0.0)
    published: list[PoseStamped] = []
    explorer.goal_request = SimpleNamespace(publish=published.append)

    assert explorer.stop_exploration(reason="stuck escape") is True

    assert published == []
    assert explorer._active_goal is None
    assert explorer.goal_reached_event.is_set()
    assert explorer.exploration_active is False
    assert explorer._completion_reason == "stuck escape"


def test_stopped_explorer_generation_cannot_block_restart() -> None:
    release = Event()
    thread = Thread(target=release.wait, daemon=True)
    thread.start()
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer._exploration_lock = RLock()
    explorer.exploration_thread = thread
    explorer.exploration_active = True
    explorer._completion_reason = None
    explorer._completed_at = None
    explorer.no_gain_counter = 0
    explorer.stop_event = Event()
    explorer.goal_reached_event = Event()
    explorer._active_goal = None

    try:
        assert explorer.stop_exploration("test") is True
        assert explorer.exploration_thread is None
        assert explorer.exploration_active is False
    finally:
        release.set()
        thread.join(timeout=1.0)


def test_active_explorer_gets_longer_liveness_grace() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    now = 100.0
    curiosity._exploring = True
    curiosity._explore_requested_at = now - 10.0
    curiosity._last_motion_ts = now - 5.0
    curiosity._motion_watch_started_at = 0.0
    recoveries: list[bool] = []
    curiosity._recover_from_stall = lambda: recoveries.append(True)

    # Four seconds since explorer start is inside the bounded startup grace.
    curiosity._explore_requested_at = now - 4.0
    curiosity._enforce_liveness(now, NavigationState.IDLE)
    assert recoveries == []

    # Past startup grace, five seconds is still inside the six-second idle
    # budget, but recovery fires before booth-visible ten-second stillness.
    curiosity._explore_requested_at = now - 30.0
    curiosity._enforce_liveness(now, NavigationState.IDLE)
    assert recoveries == []
    curiosity._last_motion_ts = now - 7.0
    curiosity._enforce_liveness(now, NavigationState.IDLE)
    assert recoveries == [True]


def test_explore_gesture_cadence_is_sparser_than_mapped() -> None:
    config = CuriosityConfig()
    assert (
        config.dog_expression_explore_min_interval_s
        > config.dog_expression_max_interval_s
    )


def test_mapping_gesture_waits_for_route_boundary() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._dog_expression_name = None
    curiosity._dog_expression_deadline = 0.0
    curiosity._dog_expression_next_at = 0.0
    curiosity._last_dog_expression = None
    curiosity._exploring = True
    curiosity._explore_started_once = True
    curiosity._patrolling = False
    curiosity._last_motion_ts = 100.0
    curiosity._motion_watch_started_at = 0.0
    stops: list[tuple[str, bool]] = []
    curiosity._stop_exploration = lambda reason, *, cancel_goal: stops.append(
        (reason, cancel_goal)
    )
    curiosity._publish_stop = lambda: None
    started: list[str] = []
    curiosity._start_dog_expression = (
        lambda name, _duration, _now: started.append(name) or True
    )

    # Without break mode an active explorer still suppresses gestures.
    assert curiosity._maybe_dog_expression(100.0, NavigationState.IDLE) is False
    # Before the first exploration leg (mid-boot) breaks must never fire:
    # both July 24 silent boot collapses gestured during module startup.
    curiosity._explore_started_once = False
    assert (
        curiosity._maybe_dog_expression(
            100.0, NavigationState.FOLLOWING_PATH, allow_break=True
        )
        is False
    )
    curiosity._explore_started_once = True
    assert (
        curiosity._maybe_dog_expression(
            100.0, NavigationState.FOLLOWING_PATH, allow_break=True
        )
        is False
    )
    assert stops == []
    assert started == []

    assert (
        curiosity._maybe_dog_expression(
            100.0, NavigationState.IDLE, allow_break=True
        )
        is True
    )
    assert stops == [("dog expression at route boundary", True)]
    assert started


def test_curious_follow_waits_for_route_boundary_during_mapping() -> None:
    now = time.monotonic()
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._lock = RLock()
    curiosity._leases = {}
    curiosity._enabled = True
    curiosity._explicit_hold_reason = None
    curiosity._preempt_requested = False
    curiosity._agent_busy = False
    curiosity._movement_intent_at = 0.0
    curiosity._movement_intent_tool = None
    curiosity._battery_checked_at = now + 100.0
    curiosity._battery_soc = 80
    curiosity._last_odom_at = now
    curiosity._last_costmap_at = now
    curiosity._last_exploration_status_at = now + 100.0
    curiosity._map_phase = "EXPLORING"
    curiosity._exploring = True
    curiosity._patrolling = False
    curiosity._curious_follow_started = 0.0
    curiosity._navigation_state = lambda: NavigationState.FOLLOWING_PATH
    curiosity._is_following = lambda: False
    curiosity._person_is_close = lambda _now: True
    curiosity._interrupt_dog_expression = lambda _reason: None
    curiosity._maybe_eye_contact_response = lambda *_args: False
    curiosity._maybe_wave_response = lambda *_args: False
    curiosity._maybe_face_observation = lambda *_args: False
    curiosity._maybe_dog_expression = lambda *_args, **_kwargs: False
    curiosity._ensure_exploring = lambda _now: None
    curiosity._maybe_escape_stall = lambda _now: False
    curiosity._set_activity = lambda *_args: None
    curiosity._enforce_liveness = lambda *_args: None
    follows: list[bool] = []
    curiosity._start_curious_follow = lambda: follows.append(True)

    curiosity._tick()

    assert follows == []
    curiosity._navigation_state = lambda: NavigationState.IDLE
    curiosity._tick()

    assert follows == [True]


def test_active_explorer_mcp_retry_does_not_rearm_firmware(monkeypatch) -> None:
    rearm_calls: list[bool] = []
    dispatches: list[dict] = []
    monkeypatch.setattr(
        "nightwatch.curiosity.ensure_motion_ready",
        lambda _connection: rearm_calls.append(True),
    )
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._exploring = True
    curiosity._exploration_capability_bound = False
    curiosity._retry_not_before = 0.0
    curiosity._explore_requested_at = 0.0
    curiosity._explore_started_once = True
    curiosity._connection = object()
    curiosity._explore = SimpleNamespace(is_exploration_active=lambda: True)
    curiosity._agent_spec = SimpleNamespace(
        dispatch_continuation=lambda request, _meta: (
            dispatches.append(request) or False
        )
    )
    curiosity._reset_motion_watch = lambda _now: None

    curiosity._ensure_exploring(100.0)

    assert rearm_calls == []
    assert dispatches == [{"tool": "begin_exploration", "args": {}}]


def test_inactive_explorer_waits_for_successful_motion_rearm(monkeypatch) -> None:
    monkeypatch.setattr(
        "nightwatch.curiosity.ensure_motion_ready",
        lambda _connection: False,
    )
    starts: list[bool] = []
    dispatches: list[dict] = []
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(exploration_retry_s=2.0)
    curiosity._exploring = False
    curiosity._exploration_capability_bound = False
    curiosity._retry_not_before = 0.0
    curiosity._explore_requested_at = 0.0
    curiosity._connection = object()
    curiosity._explore = SimpleNamespace(
        is_exploration_active=lambda: False,
        exploration_status=lambda: {"map_complete": False},
        explore=lambda: starts.append(True) or True,
    )
    curiosity._agent_spec = SimpleNamespace(
        dispatch_continuation=lambda request, _meta: (
            dispatches.append(request) or True
        )
    )

    curiosity._ensure_exploring(100.0)

    assert starts == []
    assert dispatches == []
    assert curiosity._retry_not_before == 102.0


def _escape_ready_curiosity(config: CuriosityConfig) -> CuriositySupervisor:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = config
    curiosity._lock = RLock()
    curiosity._stall_anchor = None
    curiosity._stall_events = deque()
    curiosity._escape_events = deque()
    curiosity._escape_help_spoken = False
    curiosity._last_odom = PoseStamped(position=Vector3(0.0, 0.0, 0.0))
    return curiosity


def test_stuck_escape_fires_on_second_stall_not_first() -> None:
    curiosity = _escape_ready_curiosity(CuriosityConfig())
    escapes: list[float] = []
    curiosity._run_escape = lambda now: escapes.append(now)
    curiosity._escape_give_up = lambda now: escapes.append(-now)

    # t=0 sets the anchor; a full stall_window_s (10 s) with no motion is one
    # stall, but the escape only fires on the SECOND stall within the pair
    # window.
    assert curiosity._maybe_escape_stall(0.0) is False
    assert curiosity._maybe_escape_stall(10.0) is False
    assert escapes == []
    assert curiosity._maybe_escape_stall(20.0) is True
    assert escapes == [20.0]


def test_stuck_escape_resets_window_on_real_motion() -> None:
    curiosity = _escape_ready_curiosity(CuriosityConfig())
    escapes: list[float] = []
    curiosity._run_escape = lambda now: escapes.append(now)

    curiosity._maybe_escape_stall(0.0)  # anchor at (0, 0)
    # The dog actually moved: the window resets and no stall is counted.
    curiosity._last_odom = PoseStamped(position=Vector3(0.5, 0.0, 0.0))
    assert curiosity._maybe_escape_stall(10.0) is False
    assert curiosity._stall_events == deque()
    assert escapes == []


def test_stuck_escape_stops_without_costmap_proof(monkeypatch) -> None:
    monkeypatch.setattr(
        "nightwatch.curiosity.ensure_motion_ready", lambda *a, **k: None
    )
    curiosity = _escape_ready_curiosity(
        CuriosityConfig(escape_reverse_s=0.15, escape_hz=40.0)
    )
    curiosity._stop_event = Event()
    curiosity._preempt_requested = False
    curiosity._twist_in_flight = False
    curiosity._retry_not_before = 0.0
    curiosity._connection = object()
    curiosity._last_odom = PoseStamped(position=Vector3(1.0, 2.0, 0.0))
    twists: list[Twist] = []
    stops: list[bool] = []
    curiosity._publish_twist = lambda t: twists.append(t)
    curiosity._publish_stop = lambda: stops.append(True)
    curiosity._stop_exploration = lambda reason, *, cancel_goal: None
    curiosity._stop_patrol = lambda reason: None
    curiosity._person_currently_close = lambda: False
    curiosity._speak_line = lambda text: None

    curiosity._run_escape(100.0)

    assert twists == [], "recovery must not reverse without a proven rear corridor"
    assert stops == [True]
    assert curiosity._stall_anchor is None


def test_stuck_escape_reverses_when_rear_swept_corridor_is_clear(monkeypatch) -> None:
    monkeypatch.setattr(
        "nightwatch.curiosity.ensure_motion_ready", lambda *a, **k: True
    )
    curiosity = _escape_ready_curiosity(
        CuriosityConfig(
            escape_reverse_speed_mps=0.2,
            escape_reverse_s=0.03,
            escape_hz=100.0,
        )
    )
    curiosity._stop_event = Event()
    curiosity._preempt_requested = False
    curiosity._twist_in_flight = False
    curiosity._retry_not_before = 0.0
    curiosity._connection = object()
    curiosity._last_odom = PoseStamped(
        position=Vector3(0.0, 0.0, 0.0),
        orientation=Quaternion.from_euler(Vector3(0.0, 0.0, 0.0)),
    )
    free = np.zeros((80, 80), dtype=np.int8)
    curiosity._latest_costmap = OccupancyGrid(
        grid=free,
        resolution=0.05,
        origin=Pose(position=Vector3(-2.0, -2.0, 0.0)),
    )
    twists: list[Twist] = []
    curiosity._publish_twist = lambda t: twists.append(t)
    curiosity._publish_stop = lambda: None
    curiosity._stop_exploration = lambda reason, *, cancel_goal: None
    curiosity._stop_patrol = lambda reason: None
    curiosity._person_currently_close = lambda: False
    curiosity._speak_line = lambda text: None

    curiosity._run_escape(100.0)

    assert twists
    assert all(twist.linear.x < 0.0 for twist in twists)


def test_stuck_escape_does_not_reverse_into_unknown_or_occupied_cells() -> None:
    curiosity = _escape_ready_curiosity(CuriosityConfig())
    curiosity._last_odom = PoseStamped(
        position=Vector3(0.0, 0.0, 0.0),
        orientation=Quaternion.from_euler(Vector3(0.0, 0.0, 0.0)),
    )
    unknown = np.full((80, 80), -1, dtype=np.int8)
    curiosity._latest_costmap = OccupancyGrid(
        grid=unknown,
        resolution=0.05,
        origin=Pose(position=Vector3(-2.0, -2.0, 0.0)),
    )
    assert curiosity._reverse_escape_is_clear() is False

    occupied = np.zeros((80, 80), dtype=np.int8)
    occupied[35:45, 34:40] = 100
    curiosity._latest_costmap = OccupancyGrid(
        grid=occupied,
        resolution=0.05,
        origin=Pose(position=Vector3(-2.0, -2.0, 0.0)),
    )
    assert curiosity._reverse_escape_is_clear() is False


def test_stuck_escape_caps_at_three_then_quarantines_trap() -> None:
    curiosity = _escape_ready_curiosity(CuriosityConfig())
    # Three escapes already inside the 300 s cap window and one prior stall in
    # the pair window; the second stall now would be the fourth escape.
    curiosity._stall_anchor = (0.0, 0.0, 0.0)
    curiosity._stall_events = deque([50.0])
    curiosity._escape_events = deque([10.0, 20.0, 30.0])
    calls: list[tuple[str, float]] = []
    curiosity._run_escape = lambda now: calls.append(("escape", now))
    curiosity._escape_give_up = lambda now: calls.append(("giveup", now))

    assert curiosity._maybe_escape_stall(60.0) is True
    assert calls == [("giveup", 60.0)]


def test_stuck_escape_cap_resets_without_self_keep_out_and_keeps_autonomy_alive() -> None:
    curiosity = _escape_ready_curiosity(CuriosityConfig())
    curiosity._escape_events = deque([1.0, 2.0, 3.0])
    activities: list[tuple] = []
    curiosity._set_activity = lambda kind, owner, moving, reason: activities.append(
        (kind, owner, moving, reason)
    )
    curiosity._stop_exploration = lambda reason, *, cancel_goal: None
    curiosity._stop_patrol = lambda reason: None
    curiosity._publish_stop = lambda: None
    curiosity._lock = RLock()
    curiosity._explicit_hold_reason = None
    quarantines: list[float] = []
    curiosity._explore = SimpleNamespace(
        mark_keep_out_here=lambda radius_m: quarantines.append(radius_m) or "ok"
    )
    curiosity._map_phase = "MAPPED"
    curiosity._retry_not_before = 0.0
    curiosity._last_motion_ts = None

    curiosity._escape_give_up(100.0)

    # Giving up must NOT persist a keep-out centred on the robot: that made the
    # next startup begin inside its own forbidden circle (the self-poisoning
    # loop). Recovery is now a state reset plus a fresh outward route only.
    assert quarantines == []
    assert activities[-1] == (
        BehaviorKind.EXPLORE,
        "curiosity",
        False,
        None,
    )
    assert curiosity._explicit_hold_reason is None
    assert curiosity._map_phase == "EXPLORING"
    assert curiosity._retry_not_before == 100.5
    assert curiosity._escape_events == deque()


def test_stuck_escape_make_way_only_when_person_close(monkeypatch) -> None:
    monkeypatch.setattr(
        "nightwatch.curiosity.ensure_motion_ready", lambda *a, **k: None
    )

    def run(person_close: bool, follow_enabled: bool) -> list[str]:
        curiosity = _escape_ready_curiosity(
            CuriosityConfig(
                escape_reverse_s=0.02,
                escape_hz=50.0,
                curious_follow_enabled=follow_enabled,
            )
        )
        curiosity._stop_event = Event()
        curiosity._preempt_requested = False
        curiosity._twist_in_flight = False
        curiosity._retry_not_before = 0.0
        curiosity._connection = object()
        curiosity._publish_twist = lambda t: None
        curiosity._publish_stop = lambda: None
        curiosity._stop_exploration = lambda reason, *, cancel_goal: None
        curiosity._stop_patrol = lambda reason: None
        curiosity._person_currently_close = lambda: person_close
        spoken: list[str] = []
        curiosity._speak_line = lambda text: spoken.append(text)
        curiosity._run_escape(100.0)
        return spoken

    assert run(True, True) == [CuriosityConfig().make_way_text]
    assert run(False, True) == []
    # Flag off: the dog ignores people, so no make-way line even if one is close.
    assert run(True, False) == []


def test_stuck_escape_is_gated_by_lease_and_expression() -> None:
    now = time.monotonic()

    def tick_with(*, lease, expression: bool) -> list[float]:
        curiosity = object.__new__(CuriositySupervisor)
        curiosity.config = CuriosityConfig()
        curiosity._lock = RLock()
        curiosity._enabled = True
        curiosity._explicit_hold_reason = None
        curiosity._preempt_requested = False
        curiosity._agent_busy = False
        curiosity._movement_intent_at = 0.0
        curiosity._movement_intent_tool = None
        curiosity._battery_soc = 80
        curiosity._last_odom_at = now
        curiosity._last_costmap_at = now
        curiosity._map_phase = "EXPLORING"
        curiosity._exploring = True
        curiosity._patrolling = False
        curiosity._curious_follow_started = 0.0
        curiosity._refresh_battery = lambda _now: None
        curiosity._is_following = lambda: False
        curiosity._navigation_state = lambda: NavigationState.IDLE
        curiosity._expire_leases_locked = lambda _now: None
        curiosity._top_lease_locked = lambda: lease
        curiosity._interrupt_dog_expression = lambda _reason: None
        curiosity._stop_exploration = lambda reason, *, cancel_goal: None
        curiosity._stop_patrol = lambda reason: None
        curiosity._set_activity = lambda *a: None
        curiosity._refresh_map_phase = lambda _now: None
        curiosity._person_is_close = lambda _now: False
        curiosity._ensure_exploring = lambda _now: None
        curiosity._enforce_liveness = lambda _now, _state: None
        curiosity._maybe_dog_expression = lambda *a, **k: expression
        escapes: list[float] = []
        curiosity._maybe_escape_stall = lambda now: escapes.append(now) or True
        curiosity._tick()
        return escapes

    lease = SimpleNamespace(
        owner="intervene", behavior=BehaviorKind.HOLD, reason="held"
    )
    # A higher-priority lease owns motion: escape must not run.
    assert tick_with(lease=lease, expression=False) == []
    # A dog expression owns the body: escape must not run.
    assert tick_with(lease=None, expression=True) == []


def test_frontier_scoring_while_navigation_idle_is_not_a_stall() -> None:
    now = time.monotonic()
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(curious_follow_enabled=False)
    curiosity._lock = RLock()
    curiosity._enabled = True
    curiosity._explicit_hold_reason = None
    curiosity._preempt_requested = False
    curiosity._agent_busy = False
    curiosity._movement_intent_at = 0.0
    curiosity._movement_intent_tool = None
    curiosity._battery_soc = 80
    curiosity._last_odom_at = now
    curiosity._last_costmap_at = now
    curiosity._map_phase = "EXPLORING"
    curiosity._exploring = True
    curiosity._patrolling = False
    curiosity._curious_follow_started = 0.0
    curiosity._refresh_battery = lambda _now: None
    curiosity._is_following = lambda: False
    curiosity._navigation_state = lambda: NavigationState.IDLE
    curiosity._expire_leases_locked = lambda _now: None
    curiosity._top_lease_locked = lambda: None
    curiosity._refresh_map_phase = lambda _now: None
    curiosity._maybe_dog_expression = lambda *a, **k: False
    curiosity._ensure_exploring = lambda _now: None
    escapes: list[float] = []
    curiosity._maybe_escape_stall = lambda tick: escapes.append(tick) or True
    curiosity._enforce_liveness = lambda _now, _state: None
    activities: list[tuple] = []
    curiosity._set_activity = lambda *args: activities.append(args)

    curiosity._tick()

    assert escapes == []
    assert activities[-1] == (
        BehaviorKind.EXPLORE,
        "curiosity",
        False,
        None,
    )


def test_patrol_waiting_for_goal_is_not_reported_as_physical_motion() -> None:
    now = time.monotonic()
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(curious_follow_enabled=False)
    curiosity._lock = RLock()
    curiosity._enabled = True
    curiosity._explicit_hold_reason = None
    curiosity._preempt_requested = False
    curiosity._agent_busy = False
    curiosity._movement_intent_at = 0.0
    curiosity._movement_intent_tool = None
    curiosity._battery_soc = 80
    curiosity._last_odom_at = now
    curiosity._last_costmap_at = now
    curiosity._map_phase = "MAPPED"
    curiosity._exploring = False
    curiosity._patrolling = True
    curiosity._curious_follow_started = 0.0
    curiosity._refresh_battery = lambda _now: None
    curiosity._is_following = lambda: False
    curiosity._navigation_state = lambda: NavigationState.IDLE
    curiosity._expire_leases_locked = lambda _now: None
    curiosity._top_lease_locked = lambda: None
    curiosity._refresh_map_phase = lambda _now: None
    curiosity._maybe_eye_contact_response = lambda *_args: False
    curiosity._maybe_wave_response = lambda *_args: False
    curiosity._maybe_face_observation = lambda *_args: False
    curiosity._maybe_hand_expression = lambda *_args: False
    curiosity._maybe_dog_expression = lambda *_args, **_kwargs: False
    curiosity._person_is_close = lambda _now: False
    curiosity._ensure_patrolling = lambda _now: None
    escapes: list[float] = []
    curiosity._maybe_escape_stall = lambda tick: escapes.append(tick) or True
    curiosity._enforce_liveness = lambda _now, _state: None
    activities: list[tuple] = []
    curiosity._set_activity = lambda *args: activities.append(args)

    curiosity._tick()

    assert escapes == []
    assert activities[-1] == (
        BehaviorKind.PATROL,
        "curiosity",
        False,
        None,
    )


def test_curious_follow_dispatch_speaks_greeting_once() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._close_streak = 3
    curiosity._follow_block_until = 0.0
    curiosity._close_bbox = [0.0, 0.0, 1.0, 1.0]
    curiosity._close_track_id = 1
    curiosity._close_person_id = None
    curiosity._curious_follow_started = 0.0
    curiosity._stop_exploration = lambda reason, *, cancel_goal: None
    curiosity._stop_patrol = lambda reason: None
    curiosity._set_activity = lambda *a: None
    curiosity._agent_spec = SimpleNamespace(dispatch_continuation=lambda *a, **k: True)
    spoken: list[str] = []
    curiosity._speak = SimpleNamespace(
        speak=lambda text, blocking=True: spoken.append(text)
    )

    curiosity._start_curious_follow()

    assert spoken == [CuriosityConfig().follow_greeting_text]


def test_curious_follow_greeting_failure_is_swallowed() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._close_streak = 3
    curiosity._follow_block_until = 0.0
    curiosity._close_bbox = [0.0, 0.0, 1.0, 1.0]
    curiosity._close_track_id = 1
    curiosity._close_person_id = None
    curiosity._curious_follow_started = 0.0
    curiosity._stop_exploration = lambda reason, *, cancel_goal: None
    curiosity._stop_patrol = lambda reason: None
    activities: list[tuple] = []
    curiosity._set_activity = lambda *a: activities.append(a)
    curiosity._agent_spec = SimpleNamespace(dispatch_continuation=lambda *a, **k: True)

    def boom(text: str, blocking: bool = True) -> str:
        raise RuntimeError("speaker offline")

    curiosity._speak = SimpleNamespace(speak=boom)

    # A failing speaker never breaks the follow dispatch.
    curiosity._start_curious_follow()
    assert curiosity._curious_follow_started > 0.0
    assert activities  # follow activity was still set


def test_curious_follow_disabled_makes_no_observation_and_keeps_exploring() -> None:
    now = time.monotonic()
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(curious_follow_enabled=False)
    curiosity._lock = RLock()
    curiosity._enabled = True
    curiosity._explicit_hold_reason = None
    curiosity._preempt_requested = False
    curiosity._agent_busy = False
    curiosity._movement_intent_at = 0.0
    curiosity._movement_intent_tool = None
    curiosity._battery_soc = 80
    curiosity._last_odom_at = now
    curiosity._last_costmap_at = now
    curiosity._map_phase = "EXPLORING"
    curiosity._exploring = True
    curiosity._patrolling = False
    curiosity._curious_follow_started = 0.0
    curiosity._refresh_battery = lambda _now: None
    curiosity._is_following = lambda: False
    curiosity._navigation_state = lambda: NavigationState.IDLE
    curiosity._expire_leases_locked = lambda _now: None
    curiosity._top_lease_locked = lambda: None
    curiosity._refresh_map_phase = lambda _now: None
    curiosity._maybe_dog_expression = lambda *a, **k: False
    curiosity._maybe_escape_stall = lambda _now: False
    curiosity._enforce_liveness = lambda _now, _state: None
    observations: list[bool] = []
    curiosity._person_is_close = lambda _now: observations.append(True) or True
    follows: list[bool] = []
    curiosity._start_curious_follow = lambda: follows.append(True)
    explores: list[bool] = []
    curiosity._ensure_exploring = lambda _now: explores.append(True)
    curiosity._set_activity = lambda *a: None

    curiosity._tick()

    # Disabled: no observe polling, no follow dispatch, exploration continues.
    assert observations == []
    assert follows == []
    assert explores == [True]


def test_face_observation_frames_person_only_during_patrol_idle(monkeypatch) -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(
        face_observation_enabled=True,
        face_observation_streak=1,
        face_observation_s=15.0,
    )
    curiosity._map_phase = "MAPPED"
    curiosity._retry_not_before = 0.0
    curiosity._face_observation_until = 0.0
    curiosity._face_observation_last_poll = 0.0
    curiosity._face_observation_candidate = None
    curiosity._face_observation_streak = 0
    curiosity._face_observation_cooldowns = {}
    curiosity._face_observation_pitch_active = False
    curiosity._face_observation_key = None
    curiosity._follow = SimpleNamespace(
        observe_person=lambda _min_height: {
            "person_id": "visitor-a",
            "track_id": 7,
            "offset": 0.0,
        }
    )
    curiosity._connection = SimpleNamespace()
    curiosity._stop_event = Event()
    curiosity._stop_patrol = lambda _reason: None
    curiosity._stop_exploration = lambda _reason, *, cancel_goal: None
    curiosity._publish_stop = lambda: None
    spoken: list[str] = []
    curiosity._speak_line = spoken.append
    activities: list[tuple] = []
    curiosity._set_activity = lambda *args: activities.append(args)
    monkeypatch.setattr("nightwatch.curiosity.ensure_motion_ready", lambda _c: True)

    now = 100.0
    assert curiosity._maybe_face_observation(now, NavigationState.IDLE) is True
    assert curiosity._face_observation_key == "person:visitor-a"
    assert curiosity._face_observation_until >= now + 14.0
    assert curiosity._face_observation_pitch_active is False
    assert spoken == [curiosity.config.face_observation_prompt]
    assert activities[-1][1] == "face_observation"

    # An active path is never interrupted to acquire another face.
    curiosity._stop_face_observation("test complete")
    assert (
        curiosity._maybe_face_observation(now + 1.0, NavigationState.FOLLOWING_PATH)
        is False
    )


def test_face_observation_cannot_starve_first_exploration_motion() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(
        face_observation_enabled=True,
        face_observation_streak=1,
    )
    curiosity._map_phase = "EXPLORING"
    curiosity._explore_started_once = True
    curiosity._last_motion_ts = None
    curiosity._retry_not_before = 0.0
    curiosity._face_observation_until = 0.0
    curiosity._face_observation_last_poll = 0.0
    observations: list[float] = []
    curiosity._follow = SimpleNamespace(
        observe_person=lambda minimum: observations.append(minimum)
    )

    assert curiosity._maybe_face_observation(100.0, NavigationState.IDLE) is False
    assert observations == []


def test_pose_wave_trigger_requires_confident_wrist_above_elbow() -> None:
    keypoints = np.zeros((17, 2), dtype=np.float32)
    scores = np.ones(17, dtype=np.float32)
    # Left shoulder, elbow, wrist rise in image coordinates.
    keypoints[5] = [100, 200]
    keypoints[7] = [95, 160]
    keypoints[9] = [90, 105]
    detection = SimpleNamespace(keypoints=keypoints, keypoint_scores=scores)

    assert _raised_hand(detection) == (True, "left")
    keypoints[9, 1] = 220
    assert _raised_hand(detection) == (False, None)
    keypoints[9, 1] = 105
    scores[9] = 0.1
    assert _raised_hand(detection) == (False, None)


def test_attention_gate_requires_both_eyes_with_nose_between() -> None:
    keypoints = np.zeros((17, 2), dtype=np.float32)
    scores = np.ones(17, dtype=np.float32)
    keypoints[0] = [100, 80]
    keypoints[1] = [90, 75]
    keypoints[2] = [110, 75]
    detection = SimpleNamespace(keypoints=keypoints, keypoint_scores=scores)

    assert _frontal_face_looking(detection) is True
    keypoints[0, 0] = 120
    assert _frontal_face_looking(detection) is False
    keypoints[0, 0] = 100
    scores[2] = 0.2
    assert _frontal_face_looking(detection) is False


def test_wave_response_waits_for_navigation_gap_and_uses_hello_once() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(wave_response_poll_s=0.0)
    curiosity._wave_response_last_poll = 0.0
    curiosity._wave_response_cooldowns = {}
    curiosity._wave_response_global_until = 0.0
    curiosity._wave_response_pending = None
    curiosity._wave_response_pending_at = 0.0
    curiosity._dog_expression_name = None
    curiosity._follow = SimpleNamespace(
        observe_waving_person=lambda _minimum: {
            "track_id": 8,
            "person_id": None,
            "raised_hand_side": "right",
        }
    )
    calls: list[tuple] = []
    curiosity._stop_face_observation = lambda reason: calls.append(("face", reason))
    curiosity._stop_exploration = (
        lambda reason, *, cancel_goal: calls.append(
            ("explore", reason, cancel_goal)
        )
    )
    curiosity._stop_patrol = lambda reason: calls.append(("patrol", reason))
    curiosity._publish_stop = lambda: calls.append(("stop",))
    curiosity._speak_line = lambda text: calls.append(("say", text))
    curiosity._start_dog_expression = (
        lambda name, duration, now: calls.append(
            ("expression", name, duration, now)
        )
        or True
    )

    # The active route is never cancelled; the acknowledgement is queued.
    assert (
        curiosity._maybe_wave_response(100.0, NavigationState.FOLLOWING_PATH)
        is False
    )
    assert not any(call[0] in {"explore", "stop", "say", "expression"} for call in calls)

    assert curiosity._maybe_wave_response(101.0, NavigationState.IDLE) is True
    assert ("expression", "Hello", 3.0, 101.0) in calls
    assert not any(call[0] in {"explore", "stop", "say"} for call in calls)
    assert curiosity._maybe_wave_response(102.0, NavigationState.IDLE) is False


def test_wave_observation_consumes_cached_event_exactly_once() -> None:
    follow = object.__new__(NightwatchPersonFollow)
    follow._lock = RLock()
    follow._acquisition_stale_s = 0.8
    follow._pending_wave_events = deque(
        [
            {
                "track_id": 8,
                "height_frac": 0.4,
                "offset": 0.1,
                "_wave_event_at": time.monotonic(),
            }
        ],
        maxlen=8,
    )

    assert follow.observe_waving_person(0.1)["track_id"] == 8
    assert follow.observe_waving_person(0.1) is None


def test_route_hazard_discards_social_reactions_before_escape() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._lock = RLock()
    curiosity._leases = {}
    curiosity._enabled = True
    curiosity._explicit_hold_reason = None
    curiosity._preempt_requested = False
    curiosity._movement_intent_tool = None
    curiosity._movement_intent_at = 0.0
    curiosity._agent_busy = False
    curiosity._exploring = True
    curiosity._battery_soc = 80
    curiosity._route_hazard_escape_requested = True
    curiosity._wave_response_pending = {"track_id": 8}
    curiosity._wave_response_pending_at = 1.0
    curiosity._hand_gesture_pending_at = 1.0
    curiosity._attention_follow_until = 10.0
    curiosity.config = CuriosityConfig()
    curiosity._refresh_battery = lambda _now: None
    curiosity._navigation_state = lambda: NavigationState.IDLE
    curiosity._sensors_ready = lambda _now: True
    curiosity._expire_leases_locked = lambda _now: None
    curiosity._top_lease_locked = lambda: None
    curiosity._is_following = lambda: True
    calls: list[str] = []
    curiosity._follow = SimpleNamespace(
        stop_following=lambda: calls.append("stop_follow")
    )
    curiosity._interrupt_dog_expression = (
        lambda reason: calls.append(f"interrupt:{reason}")
    )
    curiosity._publish_stop = lambda: calls.append("stop")
    curiosity._run_escape = lambda _now: calls.append("escape")

    curiosity._tick()

    assert calls == [
        "interrupt:route-boundary hazard",
        "stop_follow",
        "stop",
        "escape",
    ]
    assert curiosity._wave_response_pending is None
    assert curiosity._hand_gesture_pending_at == 0.0
    assert curiosity._attention_follow_until == 0.0


def test_failed_expression_releases_autonomy_retry_immediately(monkeypatch) -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._exploring = True
    curiosity._patrolling = False
    curiosity._retry_not_before = 999.0
    curiosity._dog_expression_name = None
    curiosity._last_dog_expression = None
    curiosity._dog_expression_next_at = 999.0
    curiosity._connection = SimpleNamespace()
    curiosity._stop_exploration = lambda *_args, **_kwargs: None
    curiosity._stop_patrol = lambda *_args, **_kwargs: None
    monkeypatch.setattr("nightwatch.curiosity.wave_hello", lambda _connection: False)

    assert curiosity._start_dog_expression("Hello", 3.0, 10.0) is False
    assert curiosity._retry_not_before == 0.0


def test_dog_first_defaults_are_enabled() -> None:
    config = CuriosityConfig()
    assert config.dog_expressions_while_exploring is True
    assert config.map_state_path.endswith("nightwatch_state.json")


def test_frontier_goal_is_recorded_once() -> None:
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.explored_goals = []
    explorer._goal_visits = []
    explorer._coverage_lock = RLock()
    goal = Vector3(1.0, 2.0, 0.0)

    explorer.mark_explored_goal(goal)

    assert explorer.explored_goals == [goal]
    assert len(explorer._goal_visits) == 1


def test_custom_patrol_does_not_reset_visit_history() -> None:
    import inspect

    source = inspect.getsource(PatrollingModule.start_patrol)
    assert "_router.reset" not in source


def test_world_memory_threshold_matches_normalized_sharpness() -> None:
    assert 0.0 <= WorldModelConfig().keyframe_min_sharpness <= 1.0
    path, displacement = NightwatchWorldModel._path_metrics(
        [(0.0, 0.0, 0.0), (1.0, 1.0, 0.0), (2.0, 0.0, 0.0)]
    )
    assert path == 2.0
    assert displacement == 0.0


def test_anonymous_person_memory_refuses_ambiguous_matches(tmp_path) -> None:
    memory = AnonymousPersonMemory(path=str(tmp_path / "people.sqlite3"))
    image = Image(
        data=np.zeros((80, 40, 3), dtype=np.uint8),
        format=ImageFormat.BGR,
        ts=time.time(),
    )
    vector = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    memory._embed = lambda _image, _bbox: vector

    person_id, _ = memory.identify(image, (0.0, 0.0, 40.0, 80.0), 1)
    assert person_id is not None
    memory._galleries["ambiguous_person"] = [vector.copy()]

    matched, score = memory.identify(image, (0.0, 0.0, 40.0, 80.0), 2, allow_new=False)

    assert matched is None
    assert score == 1.0
    assert memory.forget(person_id) is True
    assert memory.list_people() == []
    memory.close()


def test_follow_loop_processes_each_camera_frame_only_once() -> None:
    follow = object.__new__(NightwatchPersonFollow)
    follow._frequency = 30.0
    follow._max_frame_stale_seconds = 0.08
    follow._max_lost_seconds = 2.0
    follow._should_stop = Event()
    follow._lock = RLock()
    follow._latest_image = Image(
        data=np.zeros((32, 48, 3), dtype=np.uint8),
        format=ImageFormat.BGR,
        ts=time.time(),
    )
    published: list[object] = []
    follow.cmd_vel = SimpleNamespace(publish=published.append)
    follow._follow_frames = 0
    follow._duplicate_frames = 0
    follow._last_inference_ms = None
    follow._last_frame_age_ms = None
    follow._last_follow_bbox = None
    follow._last_follow_twist = None
    follow._last_follow_reason = None
    follow._person_memory = None
    reasons: list[str] = []
    follow._send_stop_reason = lambda _query, reason: reasons.append(reason)

    calls = 0

    # Python special methods are looked up on the type, not the instance.
    class EmptyDetections:
        def __len__(self):
            return 0

    class EmptyTracker:
        def process_image(self, _image):
            nonlocal calls
            calls += 1
            return EmptyDetections()

    tracker = EmptyTracker()
    thread = Thread(target=follow._follow_loop, args=(tracker, "person"), daemon=True)
    thread.start()
    time.sleep(0.24)
    follow._should_stop.set()
    thread.join(timeout=1.0)

    assert calls == 1
    assert follow._duplicate_frames > 0
    assert reasons == ["it was requested to stop following"]
    assert published  # stale-frame safety emitted zero velocity


def test_tracker_reacquires_only_matching_nearby_appearance() -> None:
    tracker = object.__new__(YoloFollowTracker)
    tracker._lock = RLock()
    tracker._last_bbox = (15.0, 10.0, 45.0, 70.0)
    tracker._target_id = 4
    tracker._reacquired_count = 0

    frame = np.zeros((100, 140, 3), dtype=np.uint8)
    frame[10:70, 15:45] = (20, 40, 220)
    frame[10:70, 90:120] = (220, 40, 20)
    image = Image(data=frame, format=ImageFormat.BGR, ts=time.time())
    tracker._appearance = _appearance_descriptor(image, tracker._last_bbox)

    same_person_new_id = SimpleNamespace(track_id=9, bbox=(16.0, 11.0, 46.0, 71.0))
    different_person = SimpleNamespace(track_id=12, bbox=(90.0, 10.0, 120.0, 70.0))
    replacement = tracker._reacquire(
        image,
        SimpleNamespace(detections=[different_person, same_person_new_id]),
    )

    assert replacement is same_person_new_id


def _world_model_with_memory_db() -> NightwatchWorldModel:
    """A world model backed by an in-memory world DB, no live ports."""
    model = object.__new__(NightwatchWorldModel)
    model._lock = RLock()
    model._db = sqlite3.connect(":memory:")
    model.config = SimpleNamespace(
        area_radius_m=2.75, area_merge_radius_m=None, object_min_evidence=3
    )
    model._session_area_ids = set()
    model._session_object_ids = set()
    model._world_to_map_cache = None
    model._world_to_map_cache_until = 0.0
    model._create_schema()
    # These merges never create tags; stub the tag paths so the model needs no
    # live spatial-memory port.
    model._tag_location = lambda *args, **kwargs: True
    return model


def _insert_area(
    model: NightwatchWorldModel,
    area_id: str,
    x: float,
    y: float,
    samples: int,
    area_type: str,
    auto_tag: str | None,
    votes: int,
    first_seen: float = 0.0,
) -> None:
    model._db.execute(
        """
        INSERT INTO areas
        (area_id,center_x,center_y,samples,area_type,confidence,auto_tag,first_seen,last_seen)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (area_id, x, y, samples, area_type, 0.8, auto_tag, first_seen, first_seen),
    )
    model._db.execute(
        "INSERT INTO area_votes(area_id,area_type,votes) VALUES(?,?,?)",
        (area_id, area_type, votes),
    )
    model._db.commit()


def test_same_type_area_chain_collapses_to_one_summed_tag() -> None:
    model = _world_model_with_memory_db()
    # Three workspace areas in a line, each within the 5.5 m merge radius of
    # the next (2.0 * area_radius_m). One open room, not three.
    _insert_area(
        model, "a", 0.0, 0.0, 10, "workspace", "workspace_1", 2, first_seen=1.0
    )
    _insert_area(
        model, "b", 3.0, 0.0, 30, "workspace", "workspace_2", 5, first_seen=2.0
    )
    _insert_area(
        model, "c", 6.0, 0.0, 20, "workspace", "workspace_3", 3, first_seen=3.0
    )
    # An object anchored to a loser must survive and re-point.
    model._db.execute(
        """
        INSERT INTO objects
        (object_id,label,center_x,center_y,center_z,evidence,stable,area_id,first_seen,last_seen)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        ("obj1", "desk", 0.0, 0.0, 0.0, 3, 1, "a", 0.0, 0.0),
    )
    model._db.commit()

    merges = model._merge_adjacent_areas()

    rows = model._db.execute("SELECT area_id,samples,auto_tag FROM areas").fetchall()
    assert merges == 2
    assert len(rows) == 1
    survivor_id, samples, auto_tag = rows[0]
    # Survivor keeps the most-sampled area's identity; samples are summed.
    assert survivor_id == "b"
    assert samples == 60
    assert auto_tag == "workspace_2"
    # Per-type votes are summed onto the survivor.
    total_votes = model._db.execute(
        "SELECT votes FROM area_votes WHERE area_id='b' AND area_type='workspace'"
    ).fetchone()[0]
    assert total_votes == 10
    # The object followed its area into the survivor.
    assert (
        model._db.execute(
            "SELECT area_id FROM objects WHERE object_id='obj1'"
        ).fetchone()[0]
        == "b"
    )


def test_different_type_neighbors_are_not_merged() -> None:
    model = _world_model_with_memory_db()
    _insert_area(model, "w", 0.0, 0.0, 20, "workspace", "workspace_1", 4)
    _insert_area(model, "k", 1.0, 0.0, 20, "kitchen", "kitchen_1", 4)

    merges = model._merge_adjacent_areas()

    assert merges == 0
    assert model._db.execute("SELECT COUNT(*) FROM areas").fetchone()[0] == 2


def test_resolving_adjacent_same_type_merges_instead_of_new_tag() -> None:
    model = _world_model_with_memory_db()
    tag_calls: list[str] = []
    model._tag_location = lambda name, *args, **kwargs: tag_calls.append(name) or True
    # An already-tagged workspace, and a new nearby area resolving to workspace.
    _insert_area(model, "existing", 0.0, 0.0, 30, "workspace", "workspace_1", 6)
    model._db.execute(
        """
        INSERT INTO areas
        (area_id,center_x,center_y,samples,area_type,confidence,auto_tag,first_seen,last_seen)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        ("fresh", 2.0, 0.0, 3, "unknown", 0.0, None, 5.0, 5.0),
    )
    model._db.executemany(
        "INSERT INTO area_votes(area_id,area_type,votes) VALUES(?,?,?)",
        [("fresh", "unknown", 1), ("fresh", "workspace", 3)],
    )
    model._db.commit()

    model._resolve_area("fresh")

    workspace_rows = model._db.execute(
        "SELECT area_id,samples,auto_tag FROM areas WHERE area_type='workspace'"
    ).fetchall()
    # No workspace_2 was minted; the fresh area folded into workspace_1.
    assert len(workspace_rows) == 1
    survivor_id, samples, auto_tag = workspace_rows[0]
    assert survivor_id == "existing"
    assert auto_tag == "workspace_1"
    assert samples == 33
    assert not any(row[2] == "workspace_2" for row in workspace_rows)
    assert tag_calls == []


def test_vision_auto_mode_disables_local_model_without_touching_mps(
    monkeypatch,
) -> None:
    import nightwatch.vision as vision

    # Default (env unset) must be treated as "auto".
    monkeypatch.delenv("NIGHTWATCH_MOONDREAM_DEVICE", raising=False)

    def _forbidden(*_args, **_kwargs):
        # The auto path aborted the whole stack when it probed MPS in a
        # forkserver worker (native SIGABRT). It must now construct no model
        # at all: instantiating either class here is a regression.
        raise AssertionError("auto mode must not construct a local moondream")

    monkeypatch.setattr(vision, "MoondreamMps", _forbidden)
    monkeypatch.setattr(vision, "MoondreamCpu", _forbidden)

    service = object.__new__(vision.VisionService)
    service._model = None
    service._model_unavailable = False
    service._model_lock = RLock()

    assert service._get_model() is None
    assert service._model_unavailable is True
    # Still disabled on a second call, and still constructs nothing.
    assert service._get_model() is None


def test_rerun_bridge_listens_on_both_zenoh_and_lcm() -> None:
    # color_image rides JpegLcmTransport while the stack transport is zenoh, so
    # the bridge must be told to listen on both backends or the Camera panel
    # stays empty.
    pubsubs = _scout_rerun_config["pubsubs"]
    assert len(pubsubs) == 2
    assert sum(isinstance(pubsub, Zenoh) for pubsub in pubsubs) == 1
    assert sum(isinstance(pubsub, LCM) for pubsub in pubsubs) == 1


def _ranking_explorer() -> WavefrontFrontierExplorer:
    """A bare explorer wired for _rank_frontiers, no live ports."""
    explorer = object.__new__(WavefrontFrontierExplorer)
    explorer.config = SimpleNamespace(
        occupancy_threshold=99,
        safe_distance=3.0,
        min_frontier_perimeter=0.5,
        max_explored_distance=10.0,
        lookahead_distance=5.0,
    )
    explorer._coverage_lock = RLock()
    explorer._starved_since = None
    explorer._goal_visits = []
    explorer._failure_zones = []
    explorer._region_strikes = {}
    explorer.exploration_direction = Vector3()
    return explorer


def test_bare_frontier_ranker_does_not_read_cwd_operator_zones(
    tmp_path,
    monkeypatch,
) -> None:
    """Synthetic selectors are hermetic; initialized instances opt into zones."""
    zone_path = tmp_path / "assets/output/maps/nightwatch_zones.json"
    zone_path.parent.mkdir(parents=True)
    zone_path.write_text(
        json.dumps(
            {
                "zones": [
                    {
                        "id": "ambient-user-focus",
                        "kind": "keep_in",
                        "active": True,
                        "points": [[50, 50], [60, 50], [60, 60], [50, 60]],
                        "world_points": [
                            [50, 50],
                            [60, 50],
                            [60, 60],
                            [50, 60],
                        ],
                    }
                ]
            }
        )
    )
    monkeypatch.chdir(tmp_path)
    explorer = _ranking_explorer()
    costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8),
        resolution=0.05,
    )
    frontier = [Vector3(4.0, 0.0, 0.0)]
    pose = Vector3(0.0, 0.0, 0.0)

    assert explorer._rank_frontiers(frontier, [20], pose, costmap) == frontier

    # Production initialization assigns its venue path explicitly; exercising
    # that instance field proves containment itself was not weakened.
    explorer._operator_zone_path = str(zone_path)
    assert explorer._rank_frontiers(frontier, [20], pose, costmap) == []


def test_expired_goal_visit_no_longer_blocks_a_frontier(monkeypatch) -> None:
    explorer = _ranking_explorer()
    clock = [1000.0]
    monkeypatch.setattr("nightwatch.navigation.time.time", lambda: clock[0])
    # A single old visit sits on the only doorway to unexplored terrain.
    explorer._goal_visits = [(4.0, 0.0, clock[0])]
    costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8),
        resolution=0.05,
    )
    frontier = [Vector3(4.0, 0.0, 0.0)]
    pose = Vector3(0.0, 0.0, 0.0)

    # The fresh visit still blocks the co-located frontier.
    assert explorer._rank_frontiers(frontier, [20], pose, costmap) == []

    # Past the visit TTL the stale visit is pruned and the frontier is free.
    clock[0] += explorer._visit_ttl_s + 1.0
    ranked = explorer._rank_frontiers(frontier, [20], pose, costmap)
    assert [item.x for item in ranked] == [4.0]
    assert explorer._goal_visits == []


def test_starvation_override_releases_after_release_window(monkeypatch) -> None:
    explorer = _ranking_explorer()
    clock = [1000.0]
    monkeypatch.setattr("nightwatch.navigation.time.time", lambda: clock[0])
    # Both frontiers are blacklisted by visits; the venue has sealed itself in.
    explorer._goal_visits = [(2.5, 0.0, clock[0]), (6.0, 0.0, clock[0])]
    # A large unknown mass beyond these already-visited doorway candidates is
    # the only evidence strong enough to justify one retry.
    costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8),
        resolution=0.05,
    )
    frontiers = [Vector3(2.5, 0.0, 0.0), Vector3(6.0, 0.0, 0.0)]
    pose = Vector3(0.0, 0.0, 0.0)

    # Starvation begins; nothing is released on the first tick.
    assert explorer._rank_frontiers(frontiers, [20, 20], pose, costmap) == []
    assert explorer._starved_since == 1000.0

    # Still inside the release window.
    clock[0] += explorer._starvation_release_s - 5.0
    assert explorer._rank_frontiers(frontiers, [20, 20], pose, costmap) == []

    # Past the window: exactly one frontier is released and starvation resets.
    clock[0] += 6.0
    released = explorer._rank_frontiers(frontiers, [20, 20], pose, costmap)
    assert len(released) == 1
    assert released[0].x in (2.5, 6.0)
    assert explorer._starved_since is None


def test_starvation_override_does_not_reopen_scanned_room(monkeypatch) -> None:
    explorer = _ranking_explorer()
    clock = [1000.0]
    monkeypatch.setattr("nightwatch.navigation.time.time", lambda: clock[0])
    explorer._goal_visits = [(6.0, 0.0, clock[0])]
    costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8),
        resolution=0.05,
    )
    frontier = [Vector3(6.0, 0.0, 0.0)]
    pose = Vector3(0.0, 0.0, 0.0)

    assert explorer._rank_frontiers(frontier, [20], pose, costmap) == []
    clock[0] += explorer._starvation_release_s + 1.0
    assert explorer._rank_frontiers(frontier, [20], pose, costmap) == []
    assert explorer._starved_since is None


def test_starvation_override_prefers_visit_block_over_failure_zone(monkeypatch) -> None:
    explorer = _ranking_explorer()
    clock = [1000.0]
    monkeypatch.setattr("nightwatch.navigation.time.time", lambda: clock[0])
    # A at 2.5 is blocked by a visit only; B at 6.0 sits in a failure zone.
    explorer._goal_visits = [(2.5, 0.0, clock[0])]
    explorer._failure_zones = [(6.0, 0.0, clock[0], "stuck")]
    costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8),
        resolution=0.05,
    )
    frontiers = [Vector3(2.5, 0.0, 0.0), Vector3(6.0, 0.0, 0.0)]
    pose = Vector3(0.0, 0.0, 0.0)

    assert explorer._rank_frontiers(frontiers, [20, 20], pose, costmap) == []
    clock[0] += explorer._starvation_release_s + 1.0
    released = explorer._rank_frontiers(frontiers, [20, 20], pose, costmap)
    # A visits-only block is safer to retry than a failure zone.
    assert [item.x for item in released] == [2.5]


def test_starvation_override_never_releases_failure_zone_when_only_option(
    monkeypatch,
) -> None:
    explorer = _ranking_explorer()
    clock = [1000.0]
    monkeypatch.setattr("nightwatch.navigation.time.time", lambda: clock[0])
    explorer._failure_zones = [(6.0, 0.0, clock[0], "stuck")]
    costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8),
        resolution=0.05,
    )
    frontiers = [Vector3(6.0, 0.0, 0.0)]
    pose = Vector3(0.0, 0.0, 0.0)

    assert explorer._rank_frontiers(frontiers, [20], pose, costmap) == []
    clock[0] += explorer._starvation_release_s + 1.0
    released = explorer._rank_frontiers(frontiers, [20], pose, costmap)
    # A physical failure is evidence to stop, even if it is the only raw edge.
    assert released == []
    assert explorer._starved_since is None


def test_starvation_override_never_fires_without_raw_frontiers(monkeypatch) -> None:
    explorer = _ranking_explorer()
    # Pretend the explorer was already starving.
    explorer._starved_since = 500.0
    clock = [1000.0]
    monkeypatch.setattr("nightwatch.navigation.time.time", lambda: clock[0])
    costmap = OccupancyGrid(
        grid=np.zeros((200, 200), dtype=np.int8),
        resolution=0.05,
    )

    # Zero raw frontiers is genuine NO_FRONTIERS convergence; the override must
    # never manufacture a goal, and starvation resets.
    assert explorer._rank_frontiers([], [], Vector3(0.0, 0.0, 0.0), costmap) == []
    assert explorer._starved_since is None


def test_repeated_failures_strike_out_a_region_past_the_ttls(monkeypatch) -> None:
    explorer = _ranking_explorer()
    clock = [1000.0]
    monkeypatch.setattr("nightwatch.navigation.time.time", lambda: clock[0])
    costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8),
        resolution=0.05,
    )
    frontier = [Vector3(6.0, 0.0, 0.0)]
    pose = Vector3(0.0, 0.0, 0.0)

    # Three spatial failures in the same region (a glass window or chair trap):
    # this is the regression the time-based blacklists cannot cover.
    for _ in range(3):
        explorer.report_navigation_failure(6.0, 0.0, "stuck")

    # Advance well past both the failure TTL and the visit TTL, so the temporary
    # failure zone has fully expired. Only the session-permanent strike remains,
    # and it never expires: the honeypot stays hard-discarded.
    clock[0] += max(explorer._failure_ttl_s, explorer._visit_ttl_s) + 100.0
    assert explorer._rank_frontiers(frontier, [20], pose, costmap) == []


def test_repeated_goal_timeouts_retire_only_frontier_selection(monkeypatch) -> None:
    explorer = _ranking_explorer()
    explorer._keep_outs = []
    clock = [1000.0]
    monkeypatch.setattr("nightwatch.navigation.time.time", lambda: clock[0])
    goal = Vector3(6.0, 0.0, 0.0)

    for _ in range(explorer._timeout_strike_limit):
        explorer._active_goal = goal
        explorer._retire_timed_out_active_goal()
        clock[0] += 1.0

    assert explorer._active_goal is None
    assert explorer._timeout_strikes[explorer._strike_region_key(6.0, 0.0)] == 2
    # Timeout retirement is selection-only: it must never create a permanent
    # physical keep-out that could also block routes through the region.
    assert explorer._keep_outs == []

    clock[0] += max(explorer._failure_ttl_s, explorer._visit_ttl_s) + 100.0
    costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8),
        resolution=0.05,
    )
    assert explorer._rank_frontiers([goal], [20], Vector3(0.0, 0.0, 0.0), costmap) == []


def test_two_strikes_leave_a_region_eligible_after_the_failure_ttl(monkeypatch) -> None:
    explorer = _ranking_explorer()
    clock = [1000.0]
    monkeypatch.setattr("nightwatch.navigation.time.time", lambda: clock[0])
    costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8),
        resolution=0.05,
    )
    frontier = [Vector3(6.0, 0.0, 0.0)]
    pose = Vector3(0.0, 0.0, 0.0)

    explorer.report_navigation_failure(6.0, 0.0, "stuck")
    explorer.report_navigation_failure(6.0, 0.0, "stuck")

    # Two strikes are below the limit. While the failure zone is still fresh the
    # frontier is tier-blocked by the ordinary failure logic, not struck out.
    assert explorer._rank_frontiers(frontier, [20], pose, costmap) == []

    # Once the failure zone expires, two strikes do NOT discard it: the region
    # is selectable again, unlike a struck-out region, which never returns.
    clock[0] += explorer._failure_ttl_s + 1.0
    ranked = explorer._rank_frontiers(frontier, [20], pose, costmap)
    assert [item.x for item in ranked] == [6.0]


def test_starvation_override_strikes_out_a_region_after_three_releases(
    monkeypatch,
) -> None:
    explorer = _ranking_explorer()
    clock = [1000.0]
    monkeypatch.setattr("nightwatch.navigation.time.time", lambda: clock[0])
    # One frontier, permanently visit-blocked, so the override stays the only
    # way it can be selected. Each release is another unproductive attempt.
    explorer._goal_visits = [(6.0, 0.0, clock[0])]
    costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8),
        resolution=0.05,
    )
    frontier = [Vector3(6.0, 0.0, 0.0)]
    pose = Vector3(0.0, 0.0, 0.0)

    for _ in range(3):
        # Begin starvation, then cross the release window so the valve fires.
        assert explorer._rank_frontiers(frontier, [20], pose, costmap) == []
        clock[0] += explorer._starvation_release_s + 1.0
        released = explorer._rank_frontiers(frontier, [20], pose, costmap)
        assert [item.x for item in released] == [6.0]

    # The third release struck the region out. It no longer appears even via the
    # override, and with no other frontiers ranking converges to empty
    # (NO_FRONTIERS), the correct end state when only a honeypot remains.
    clock[0] += explorer._starvation_release_s + 1.0
    assert explorer._rank_frontiers(frontier, [20], pose, costmap) == []
    assert explorer._region_strikes[explorer._strike_region_key(6.0, 0.0)] == 3


def test_follow_failure_does_not_reach_explorer_feedback() -> None:
    model = object.__new__(NightwatchWorldModel)
    model._lock = RLock()
    model._latest_odom = None
    model._failure_last = {}
    model.config = SimpleNamespace(failure_cooldown_s=30.0)
    model._event = lambda *args, **kwargs: None
    model._maybe_record_keyframe = lambda **kwargs: None
    reported: list[tuple[float, float, str]] = []
    model._explore = SimpleNamespace(
        report_navigation_failure=lambda x, y, reason: reported.append((x, y, reason))
        or True
    )

    # Follow standing still (spatial=False): logged, but the explorer is spared.
    model._record_failure("stuck", "detail", "rec", x=1.0, y=2.0, spatial=False)
    assert reported == []

    # Goal-driven navigation (spatial=True): the region is penalized.
    model._failure_last = {}
    model._record_failure("stuck", "detail", "rec", x=1.0, y=2.0, spatial=True)
    assert reported == [(1.0, 2.0, "stuck")]


def test_person_follow_does_not_poison_failure_map_through_health_check() -> None:
    model = object.__new__(NightwatchWorldModel)
    model._lock = RLock()
    model.config = SimpleNamespace(
        stuck_window_s=8.0,
        stuck_displacement_m=0.12,
        loop_window_s=30.0,
        loop_path_m=3.0,
        loop_displacement_m=0.8,
        failure_cooldown_s=30.0,
    )
    model._last_camera_wall = time.time()
    model._latest_odom = PoseStamped(position=Vector3(1.0, 2.0, 0.0))
    model._event = lambda *args, **kwargs: None
    model._maybe_record_keyframe = lambda **kwargs: None
    reported: list[tuple[float, float, str]] = []
    model._explore = SimpleNamespace(
        report_navigation_failure=lambda x, y, reason: reported.append((x, y, reason))
        or True
    )
    base = time.monotonic()
    # Barely moved over the stuck window: the stuck detector trips.
    model._odom_history = deque(
        [(base, 0.0, 0.0), (base, 0.01, 0.0), (base, 0.0, 0.01)], maxlen=600
    )

    def _activity(behavior: BehaviorKind) -> RobotActivity:
        return RobotActivity(
            ts=time.time(),
            behavior=behavior,
            owner="test",
            moving_expected=True,
            last_motion_ts=None,
            hold_reason=None,
            map_phase="EXPLORING",
            battery_soc=80,
        )

    # Standing still at follow distance is correct, not a navigation failure.
    model._failure_last = {}
    model._latest_activity = _activity(BehaviorKind.FOLLOW)
    model._motion_expected_since = base - 9.0
    model._check_experience_health()
    assert reported == []

    # The identical stall while exploring must penalize the region.
    model._failure_last = {}
    model._latest_activity = _activity(BehaviorKind.EXPLORE)
    model._motion_expected_since = base - 9.0
    model._check_experience_health()
    assert reported == [(1.0, 2.0, "stuck")]


def test_new_motion_epoch_does_not_reuse_stationary_odom_as_stuck() -> None:
    model = object.__new__(NightwatchWorldModel)
    model._lock = RLock()
    model._latest_activity = RobotActivity(
        ts=time.time(),
        behavior=BehaviorKind.EXPRESS,
        owner="curiosity:WiggleHips",
        moving_expected=False,
        last_motion_ts=None,
        hold_reason=None,
        map_phase="EXPLORING",
        battery_soc=80,
    )
    model._motion_expected_since = None
    model._on_activity(
        RobotActivity(
            ts=time.time(),
            behavior=BehaviorKind.EXPLORE,
            owner="curiosity",
            moving_expected=True,
            last_motion_ts=None,
            hold_reason=None,
            map_phase="EXPLORING",
            battery_soc=80,
        )
    )

    assert model._motion_expected_since is not None
    assert model._motion_expected_since > time.monotonic() - 1.0


def test_new_patrol_owner_clears_stall_pair_from_explorer() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._lock = RLock()
    curiosity._patrolling = False
    curiosity._retry_not_before = 0.0
    curiosity._patrol_requested_at = 0.0
    curiosity._last_odom = PoseStamped(position=Vector3(3.0, 4.0, 0.0))
    curiosity._stall_anchor = (0.0, 3.0, 4.0)
    curiosity._stall_events = deque([5.0])
    curiosity._escape_events = deque()
    curiosity._connection = SimpleNamespace(sport_command=lambda _api_id: True)
    curiosity._patrol = SimpleNamespace(
        is_patrolling=lambda: False,
        ensure_patrolling=lambda: True,
    )

    curiosity._ensure_patrolling(10.0)

    assert curiosity._stall_anchor is None
    assert curiosity._stall_events == deque()
    assert curiosity._motion_watch_started_at == 10.0


def test_doorway_clearance_frontier_is_kept_but_ranks_below_open_space() -> None:
    explorer = _ranking_explorer()
    grid = np.zeros((400, 400), dtype=np.int8)
    # A short vertical wall 0.30 m (6 cells) to the side of the doorway
    # frontier at (5.0, 7.0): its centroid clearance lands in the penalty band
    # [0.22, 0.40). The open-space frontier at (7.0, 5.0) is 2.75 m clear.
    grid[130:150, 94] = 100
    costmap = OccupancyGrid(grid=grid, resolution=0.05)
    robot = Vector3(5.0, 5.0, 0.0)
    open_frontier = Vector3(7.0, 5.0, 0.0)
    doorway_frontier = Vector3(5.0, 7.0, 0.0)

    # Alone, the doorway frontier is NOT discarded: rooms must stay enterable.
    alone = explorer._rank_frontiers([doorway_frontier], [20], robot, costmap)
    assert [frontier.x for frontier in alone] == [5.0]

    # With both present, the doorway frontier survives but ranks below the
    # otherwise-equal open-space frontier.
    both = explorer._rank_frontiers(
        [open_frontier, doorway_frontier], [20, 20], robot, costmap
    )
    assert [frontier.x for frontier in both] == [7.0, 5.0]


def test_big_map_cloud_is_subsampled_before_viewer_conversion(monkeypatch) -> None:
    from nightwatch import blueprints

    from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2

    received: list = []
    monkeypatch.setattr(
        blueprints,
        "_convert_global_map",
        lambda msg: received.append(msg) or "archetype",
    )

    big = PointCloud2.from_numpy(
        np.random.rand(400_000, 3).astype(np.float32), timestamp=1.0
    )
    assert blueprints._convert_big_cloud(big) == "archetype"
    # Conversion sees a reduced copy bounded by the cap, not the giant original.
    assert received[-1] is not big
    assert len(received[-1]) <= _MAX_VIEWER_CLOUD_POINTS

    small = PointCloud2.from_numpy(
        np.random.rand(1_000, 3).astype(np.float32), timestamp=1.0
    )
    blueprints._convert_big_cloud(small)
    # Under the cap the original message flows through untouched.
    assert received[-1] is small


def test_saved_map_viewer_keeps_floor_points_and_uses_distinct_style() -> None:
    calls: list[dict] = []
    saved_map = SimpleNamespace(
        points_f32=lambda: np.array(
            [[0.0, 0.0, -0.5], [1.0, 1.0, 0.5]],
            dtype=np.float32,
        ),
        to_rerun=lambda **kwargs: calls.append(kwargs) or "saved-map-archetype",
    )

    assert _convert_saved_map(saved_map) == "saved-map-archetype"
    assert calls == [
        {
            "voxel_size": 0.025,
            "colors": [72, 166, 255],
            "mode": "points",
            "bottom_cutoff": None,
        }
    ]

    calls.clear()
    assert _convert_saved_map_preview(saved_map) == "saved-map-archetype"
    assert calls == [
        {
            "voxel_size": 0.02,
            "colors": [245, 166, 35],
            "mode": "points",
            "bottom_cutoff": None,
        }
    ]


def test_growing_map_clouds_are_throttled_and_bounded_for_the_viewer() -> None:
    max_hz = _scout_rerun_config["max_hz"]
    assert max_hz["world/merged_map"] == 0.2
    assert max_hz["world/global_map"] == 1
    assert max_hz["world/aligned_loaded_map"] == 0.5
    assert max_hz["world/preview_loaded_map"] == 0.5

    override = _scout_rerun_config["visual_override"]
    for path in (
        "world/global_map",
        "world/merged_map",
    ):
        assert override[path] is _convert_big_cloud
    assert override["world/aligned_loaded_map"] is _convert_saved_map
    assert override["world/preview_loaded_map"] is _convert_saved_map_preview
    assert override["world/loaded_map"] is None


def test_map_streamer_sends_saved_premap_in_world_after_alignment(
    monkeypatch,
) -> None:
    streamer = object.__new__(MapStreamer)
    streamer.config = SimpleNamespace(max_points=150_000)
    streamer._clients = set()
    streamer._premap_seq = 0
    streamer._reloc_transform = {"x": 10.0, "y": 20.0, "yaw": 0.0}
    streamer._reloc = SimpleNamespace(
        world_to_map_2d=lambda: {"x": 10.0, "y": 20.0, "yaw": 0.0}
    )
    sent: list[bytes | str] = []
    monkeypatch.setattr(
        "nightwatch.map_stream.ws_server.broadcast",
        lambda _clients, payload: sent.append(payload),
    )
    premap = PointCloud2.from_numpy(
        np.array([[10.0, 20.0, 0.25]], dtype=np.float32),
        frame_id="map",
        timestamp=123.0,
    )

    asyncio.run(streamer.handle_loaded_map(premap))

    magic, seq, timestamp, count = struct.unpack("<IIdI", streamer._latest_premap[:20])
    points = np.frombuffer(streamer._latest_premap, dtype="<f4", offset=20).reshape(
        -1, 3
    )
    assert (magic, seq, timestamp, count) == (PREMAP_MAGIC, 1, 123.0, 1)
    assert points[0].tolist() == pytest.approx([0.0, 0.0, 0.25])
    assert json.loads(streamer._latest_premap_status) == {
        "type": "premap",
        "aligned": True,
        "world_to_map": {"x": 10.0, "y": 20.0, "yaw": 0.0},
    }
    assert sent == [streamer._latest_premap, streamer._latest_premap_status]


def test_tent_object_votes_sleeping_area_once_on_becoming_stable() -> None:
    model = _world_model_with_memory_db()
    now = 1000.0
    area_id = model._ensure_area(0.0, 0.0, now)

    # Re-observe the same tent until it crosses the stability threshold (3),
    # then keep observing. The heavy sleeping_area vote must fire exactly once.
    for _ in range(5):
        model._upsert_object("tent", 0.0, 0.0, 0.0, area_id, now)

    sleeping_votes = model._db.execute(
        "SELECT votes FROM area_votes WHERE area_id=? AND area_type='sleeping_area'",
        (area_id,),
    ).fetchone()
    assert sleeping_votes is not None
    # A single decisive vote of 8, not 8 per re-observation.
    assert sleeping_votes[0] == 8

    # The heavy vote resolves the area to sleeping_area over the routine
    # "unknown" seed vote _ensure_area planted.
    model._resolve_area(area_id)
    resolved = model._db.execute(
        "SELECT area_type,auto_tag FROM areas WHERE area_id=?", (area_id,)
    ).fetchone()
    assert resolved[0] == "sleeping_area"
    assert resolved[1] == "sleeping_area"


def test_diverse_sleeping_furniture_corrects_early_workspace_tag() -> None:
    model = _world_model_with_memory_db()
    tag_calls: list[str] = []
    model._tag_location = lambda name, *args, **kwargs: tag_calls.append(name) or True
    now = 1000.0
    area_id = model._ensure_area(0.0, 0.0, now)
    model._vote_area(area_id, "workspace", 16)
    model._resolve_area(area_id)
    assert model._area_row(area_id)["area_type"] == "workspace"

    # Large tents produce scattered 3-D centers, so none of these individual
    # objects needs to reach the per-object stability threshold. Independent
    # room-level cues are decisive, including the "camping tent" variant.
    model._upsert_object("camping tent", 0.0, 0.0, 0.0, area_id, now)
    model._upsert_object("sleeping bag", 2.0, 0.0, 0.0, area_id, now)
    model._upsert_object("camp bed", -2.0, 0.0, 0.0, area_id, now)

    assert model._reinforce_sleeping_area_from_objects(area_id) is True
    model._resolve_area(area_id)

    resolved = model._area_row(area_id)
    assert resolved["area_type"] == "sleeping_area"
    assert resolved["auto_tag"].startswith("sleeping_area")
    assert tag_calls[-1].startswith("sleeping_area")


def test_resolved_room_fragments_stay_suppressed_but_new_doorway_reopens() -> None:
    explorer = _ranking_explorer()
    explorer._resolved_goal_visits = [(4.0, 0.0)]
    pose = Vector3(0.0, 0.0, 0.0)
    frontier = [Vector3(4.0, 0.0, 0.0)]

    covered_room = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8), resolution=0.05
    )
    assert explorer._rank_frontiers(frontier, [20], pose, covered_room) == []

    doorway_to_unknown = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8), resolution=0.05
    )
    assert (
        explorer._rank_frontiers(frontier, [20], pose, doorway_to_unknown) == frontier
    )


def test_world_to_map_2d_maps_world_into_map_frame() -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc._alignment_locked = False
    reloc._accepted_world_to_map = None
    # Unlocked: nothing to report.
    assert reloc.world_to_map_2d() is None

    # world->map: rotate +90 degrees about z, then translate by (10, 5).
    yaw = math.pi / 2.0
    T = np.eye(4)
    T[:3, :3] = np.array(
        [
            [math.cos(yaw), -math.sin(yaw), 0.0],
            [math.sin(yaw), math.cos(yaw), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    T[:3, 3] = [10.0, 5.0, 0.0]
    reloc._alignment_locked = True
    reloc._accepted_world_to_map = T

    result = reloc.world_to_map_2d()
    assert result is not None
    assert abs(result["x"] - 10.0) < 1e-9
    assert abs(result["y"] - 5.0) < 1e-9
    assert abs(result["yaw"] - yaw) < 1e-9

    # A known world point projected with the returned 2D transform must equal
    # the full 4x4 application: world (2, 0) -> rotate 90deg -> (0, 2) -> +t.
    wx, wy = 2.0, 0.0
    c, s = math.cos(result["yaw"]), math.sin(result["yaw"])
    map_x = c * wx - s * wy + result["x"]
    map_y = s * wx + c * wy + result["y"]
    assert abs(map_x - 10.0) < 1e-9
    assert abs(map_y - 7.0) < 1e-9
    expected = T @ np.array([wx, wy, 0.0, 1.0])
    assert abs(map_x - expected[0]) < 1e-9
    assert abs(map_y - expected[1]) < 1e-9


def test_operator_initial_pose_locks_and_publishes_world_to_map() -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc._alignment_locked = False
    reloc._accepted_world_to_map = None
    reloc._failed_attempts = 12
    published: list[object] = []
    reloc._world_to_map = SimpleNamespace(on_next=published.append)

    result = reloc.set_initial_pose_2d(17.5, -4.75, math.pi / 6.0)

    assert result == {
        "accepted": True,
        "x": 17.5,
        "y": -4.75,
        "yaw": math.pi / 6.0,
    }
    assert reloc._alignment_locked is True
    assert reloc._failed_attempts == 0
    assert len(published) == 1
    projected = reloc.world_to_map_2d()
    assert projected is not None
    assert abs(projected["x"] - 17.5) < 1e-9
    assert abs(projected["y"] + 4.75) < 1e-9
    assert abs(projected["yaw"] - math.pi / 6.0) < 1e-9


def test_operator_initial_pose_rejects_non_finite_values() -> None:
    reloc = object.__new__(NightwatchRelocalization)
    with pytest.raises(ValueError, match="must be finite"):
        reloc.set_initial_pose_2d(float("nan"), 0.0, 0.0)


def test_map_coord_migration_adds_columns_to_legacy_db() -> None:
    model = object.__new__(NightwatchWorldModel)
    model._lock = RLock()
    db = sqlite3.connect(":memory:")
    model._db = db
    # Legacy schema: areas/objects without map_x/map_y (a pre-migration DB).
    db.executescript(
        """
        CREATE TABLE areas (
            area_id TEXT PRIMARY KEY, center_x REAL NOT NULL, center_y REAL NOT NULL,
            samples INTEGER NOT NULL, area_type TEXT NOT NULL, confidence REAL NOT NULL,
            auto_tag TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL
        );
        CREATE TABLE objects (
            object_id TEXT PRIMARY KEY, label TEXT NOT NULL, center_x REAL NOT NULL,
            center_y REAL NOT NULL, center_z REAL NOT NULL, evidence INTEGER NOT NULL,
            stable INTEGER NOT NULL, area_id TEXT, first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        );
        """
    )
    db.commit()
    assert "map_x" not in {
        row[1] for row in db.execute("PRAGMA table_info(areas)").fetchall()
    }

    # The create+migrate path is additive and idempotent on an existing DB.
    model._create_schema()

    db.execute(
        """
        INSERT INTO areas
        (area_id,center_x,center_y,samples,area_type,confidence,auto_tag,
         first_seen,last_seen,map_x,map_y)
        VALUES('a',0,0,1,'unknown',0.0,NULL,0,0,1.5,2.5)
        """
    )
    db.execute(
        """
        INSERT INTO objects
        (object_id,label,center_x,center_y,center_z,evidence,stable,area_id,
         first_seen,last_seen,map_x,map_y)
        VALUES('o','tent',0,0,0,1,0,'a',0,0,3.5,4.5)
        """
    )
    db.commit()

    assert db.execute("SELECT map_x,map_y FROM areas WHERE area_id='a'").fetchone() == (
        1.5,
        2.5,
    )
    assert db.execute(
        "SELECT map_x,map_y FROM objects WHERE object_id='o'"
    ).fetchone() == (3.5, 4.5)


def _insert_area_with_map(
    model: NightwatchWorldModel,
    area_id: str,
    center_x: float,
    center_y: float,
    area_type: str,
    map_x: float | None,
    map_y: float | None,
) -> None:
    model._db.execute(
        """
        INSERT INTO areas
        (area_id,center_x,center_y,samples,area_type,confidence,auto_tag,
         first_seen,last_seen,map_x,map_y)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            area_id,
            center_x,
            center_y,
            5,
            area_type,
            0.9,
            area_type,
            0.0,
            0.0,
            map_x,
            map_y,
        ),
    )
    model._db.commit()


def test_nearest_area_session_prior_and_legacy_across_sessions() -> None:
    model = _world_model_with_memory_db()
    # This-session area, world frame (already correct in the current frame).
    _insert_area_with_map(model, "sess", 1.0, 0.0, "sleeping_area", None, None)
    # Prior-session area with map coords only. world->map is identity rotation +
    # (100, 100), so a prior area at map (102, 100) sits at world (2, 0) now.
    _insert_area_with_map(model, "prior", 99.0, 99.0, "sleeping_area", 102.0, 100.0)
    # Legacy prior-session row: NULL map coords, different session.
    _insert_area_with_map(model, "legacy", 55.0, 55.0, "sleeping_area", None, None)
    model._session_area_ids = {"sess"}

    tf_state: dict[str, dict | None] = {"tf": None}
    model._reloc = SimpleNamespace(world_to_map_2d=lambda: tf_state["tf"])

    # Transform unavailable: only the this-session area is considered. The prior
    # area cannot be reprojected and the legacy row is unusable.
    model._world_to_map_cache = None
    model._world_to_map_cache_until = 0.0
    result = model.nearest_area("sleeping_area", 2.0, 0.0)
    assert result is not None
    assert result["area_id"] == "sess"
    assert result["source"] == "session"

    # Transform available: the prior-session area reprojects to world (2, 0),
    # which is nearer to the query (1.9, 0) than the session area at (1, 0).
    tf_state["tf"] = {"x": 100.0, "y": 100.0, "yaw": 0.0}
    model._world_to_map_cache = None
    model._world_to_map_cache_until = 0.0
    result = model.nearest_area("sleeping_area", 1.9, 0.0)
    assert result is not None
    assert result["area_id"] == "prior"
    assert result["source"] == "persistent"
    assert abs(result["x"] - 2.0) < 1e-9
    assert abs(result["y"] - 0.0) < 1e-9

    # The legacy row (NULL map coords, other session) is never returned, even
    # though it is the sole candidate here.
    assert model.nearest_area("kitchen", 0.0, 0.0) is None


def test_nearest_area_backfill_projects_session_rows_into_map_frame() -> None:
    model = _world_model_with_memory_db()
    now = 500.0
    area_id = model._ensure_area(3.0, 4.0, now)
    model._reloc = SimpleNamespace(
        world_to_map_2d=lambda: {"x": 100.0, "y": 100.0, "yaw": 0.0}
    )
    model._world_to_map_cache = None
    model._world_to_map_cache_until = 0.0

    model._backfill_map_coords()

    row = model._db.execute(
        "SELECT map_x,map_y FROM areas WHERE area_id=?", (area_id,)
    ).fetchone()
    # Identity rotation + (100, 100): world (3, 4) -> map (103, 104).
    assert row is not None
    assert abs(row[0] - 103.0) < 1e-9
    assert abs(row[1] - 104.0) < 1e-9


# ---------------------------------------------------------------------------
# Suspicious protocol (potential_detected): firmware waves, gaze, lease gating.
# ---------------------------------------------------------------------------


def test_wave_hello_is_real_only_when_firmware_acknowledges(monkeypatch) -> None:
    monkeypatch.setattr(nightwatch_unitree.time, "sleep", lambda _seconds: None)
    nightwatch_unitree._last_ready[0] = 0.0
    requests: list[dict] = []
    connection = SimpleNamespace(
        publish_request=lambda _topic, request: requests.append(request.copy())
        or {"status": "ok"}
    )
    assert nightwatch_unitree.wave_hello(connection) is True
    # Re-arm ran first, then the parameterless Hello routine was published last.
    assert requests[-1]["api_id"] == SPORT_CMD["Hello"]
    assert "parameter" not in requests[-1]

    # A firmware-rejected Hello (the "wave got blocked" case) is not a real wave.
    nightwatch_unitree._last_ready[0] = 0.0

    def publish_blocked(_topic, request):
        if request["api_id"] == SPORT_CMD["Hello"]:
            return {"status": "error", "code": 3}
        return {"status": "ok"}

    assert (
        nightwatch_unitree.wave_hello(SimpleNamespace(publish_request=publish_blocked))
        is False
    )


def test_body_pitch_is_refused_without_sending_a_posture_command() -> None:
    # Lowering/tilting the hips caused the live locomotion lock. Nightwatch no
    # longer emits Pose or Euler commands for sleep scans.
    requests: list[dict] = []
    connection = SimpleNamespace(
        publish_request=lambda _topic, request: requests.append(request.copy())
        or {"status": "ok"}
    )
    assert nightwatch_unitree.set_body_pitch(connection, 0.35) is False
    assert requests == []

    # A legacy zero/restore call uses normal MCF recovery controls only.
    assert nightwatch_unitree.set_body_pitch(connection, 0.0) is True
    assert [request["api_id"] for request in requests] == [
        SPORT_CMD["StopMove"],
        SPORT_CMD["BalanceStand"],
    ]


def test_body_pitch_cleanup_reports_rejected_stopmove() -> None:
    requests: list[dict] = []

    def publish(_topic, request):
        requests.append(request.copy())
        if request["api_id"] == SPORT_CMD["StopMove"]:
            return {"status": "error", "code": 3}
        return {"status": "ok"}

    connection = SimpleNamespace(publish_request=publish)
    assert nightwatch_unitree.set_body_pitch(connection, 0.0) is False
    assert [request["api_id"] for request in requests] == [
        SPORT_CMD["StopMove"],
    ]


def test_frontal_face_looking_requires_nose_between_confident_eyes() -> None:
    # nose (100) sits between left_eye (80) and right_eye (120): frontal.
    keypoints = [[100.0, 50.0], [80.0, 45.0], [120.0, 45.0]]
    scores = [0.9, 0.9, 0.9]
    assert frontal_face_looking(keypoints, scores, 0.5) is True

    # Nose pushed outside the eye span: face turned away.
    turned = [[135.0, 50.0], [80.0, 45.0], [120.0, 45.0]]
    assert frontal_face_looking(turned, scores, 0.5) is False

    # An eye below the confidence threshold disqualifies the frame.
    assert frontal_face_looking(keypoints, [0.9, 0.2, 0.9], 0.5) is False

    # Missing keypoints never raise; they simply are not "looking".
    assert frontal_face_looking([], [], 0.5) is False


def test_check_gaze_needs_consecutive_frontal_frames() -> None:
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    def advance(_dt: float) -> None:
        clock["t"] += 1.0

    reached = iter([True, False, True, True, True])
    assert (
        check_gaze(
            lambda: next(reached),
            clock=now,
            sleep=advance,
            window_s=100.0,
            frames_required=3,
            poll_s=1.0,
        )
        is True
    )

    clock["t"] = 0.0
    never = iter([True, False, True, False, True, False])
    assert (
        check_gaze(
            lambda: next(never),
            clock=now,
            sleep=advance,
            window_s=5.0,
            frames_required=3,
            poll_s=1.0,
        )
        is False
    )


def test_wave_protocol_counts_only_real_waves() -> None:
    # Attempts 1 and 3 are blocked by firmware; the rest are real waves.
    outcomes = iter([False, True, False, True, True, True])
    attempted: list[bool] = []
    rearms: list[bool] = []
    renews: list[bool] = []

    def waver() -> bool:
        value = next(outcomes)
        attempted.append(value)
        return value

    waves, attempts, looked = run_wave_protocol(
        waver=waver,
        rearm=lambda: rearms.append(True),
        gaze_check=lambda: False,
        renew_lease=lambda: renews.append(True),
        max_waves=4,
        max_attempts=8,
    )

    assert waves == 4
    assert attempts == 6
    assert looked is False
    assert len(attempted) == 6
    # Only the two blocked attempts triggered a re-arm; blocks never count.
    assert rearms == [True, True]
    # The lease is renewed once per attempt so it cannot expire mid-protocol.
    assert len(renews) == 6


def test_wave_protocol_does_one_final_wave_when_subject_looks() -> None:
    gaze_calls = {"n": 0}

    def gaze_check() -> bool:
        gaze_calls["n"] += 1
        # Subject looks back after the second real wave.
        return gaze_calls["n"] >= 2

    waves, attempts, looked = run_wave_protocol(
        waver=lambda: True,
        rearm=lambda: None,
        gaze_check=gaze_check,
        renew_lease=lambda: None,
        max_waves=4,
        max_attempts=8,
    )

    assert looked is True
    # Exactly one final wave after the look: three real waves total.
    assert waves == 3
    assert attempts == 3
    assert gaze_calls["n"] == 2


def _bare_intervention() -> InterventionSkill:
    skill_obj = object.__new__(InterventionSkill)
    skill_obj.config = SimpleNamespace(
        body_pitch_rad=0.35,
        max_waves=4,
        max_wave_attempts=8,
        arc_max_hops=3,
        arc_timeout_s=30.0,
    )
    return skill_obj


def test_protocol_gives_up_after_attempt_cap_and_cleans_up() -> None:
    skill_obj = _bare_intervention()
    pitch: list[float] = []
    released: list[bool] = []

    result = skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _subject: True,
        set_pitch=lambda value: pitch.append(value),
        waver=lambda: False,  # every wave blocked
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: released.append(True),
        clock=lambda: 0.0,
    )

    assert result.found is True
    assert result.waves_performed == 0
    assert result.blocked_attempts == 8
    assert result.subject_looked is False
    # Pitch was raised then restored; the lease was released.
    assert pitch == [0.35, 0.0]
    assert released == [True]


def test_protocol_restores_pitch_and_releases_lease_when_wave_loop_raises() -> None:
    skill_obj = _bare_intervention()
    pitch: list[float] = []
    released: list[bool] = []

    def exploding_waver() -> bool:
        raise RuntimeError("wave loop blew up")

    with pytest.raises(RuntimeError):
        skill_obj._run_protocol(
            find_subject=lambda: object(),
            approach=lambda _subject: True,
            set_pitch=lambda value: pitch.append(value),
            waver=exploding_waver,
            gaze_check=lambda: False,
            rearm=lambda: None,
            renew=lambda: True,
            release=lambda: released.append(True),
            clock=lambda: 0.0,
        )

    # finally path ran despite the exception: pitch restored, lease released.
    assert pitch == [0.35, 0.0]
    assert released == [True]


def test_protocol_reports_no_subject_without_touching_pitch_loop() -> None:
    skill_obj = _bare_intervention()
    pitch: list[float] = []
    released: list[bool] = []

    result = skill_obj._run_protocol(
        find_subject=lambda: None,
        approach=lambda _subject: pytest.fail("must not approach without a subject"),
        set_pitch=lambda value: pitch.append(value),
        waver=lambda: pytest.fail("must not wave without a subject"),
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: released.append(True),
        clock=lambda: 0.0,
    )

    assert result.found is False
    assert result.waves_performed == 0
    # Even the no-subject early return still restores pitch and releases.
    assert pitch == [0.0]
    assert released == [True]


def test_active_lease_suppresses_gestures_and_follow_in_tick() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._lock = RLock()
    now = time.monotonic()
    curiosity._leases = {
        "L": BehaviorLease(
            owner="intervention",
            lease_id="L",
            behavior=BehaviorKind.INTERVENE,
            priority=40,
            issued_at=now,
            expires_at=now + 100.0,
            reason="suspicious protocol",
        )
    }
    curiosity.config = SimpleNamespace(min_battery_soc=10)
    curiosity._enabled = True
    curiosity._explicit_hold_reason = None
    curiosity._preempt_requested = False
    curiosity._movement_intent_tool = None
    curiosity._movement_intent_at = 0.0
    curiosity._agent_busy = False
    curiosity._exploring = False
    curiosity._battery_soc = 80
    curiosity._refresh_battery = lambda _now: None
    curiosity._is_following = lambda: False
    curiosity._navigation_state = lambda: NavigationState.IDLE
    curiosity._sensors_ready = lambda _now: True
    curiosity._interrupt_dog_expression = lambda *_a, **_k: None
    curiosity._stop_exploration = lambda *_a, **_k: None
    curiosity._stop_patrol = lambda *_a, **_k: None

    gestures: list[bool] = []
    follows: list[bool] = []
    people: list[bool] = []
    curiosity._maybe_dog_expression = lambda *_a, **_k: gestures.append(True) or False
    curiosity._start_curious_follow = lambda *_a, **_k: follows.append(True)
    curiosity._person_is_close = lambda *_a, **_k: people.append(True) or True

    activity: list[tuple] = []
    curiosity._set_activity = lambda behavior, owner, moving, hold: activity.append(
        (behavior, owner)
    )

    curiosity._tick()

    # No gesture fired, no close-person follow dispatched, no person poll reached.
    assert gestures == []
    assert follows == []
    assert people == []
    # The lease is surfaced as the operator-visible intervene activity.
    assert activity and activity[0] == (BehaviorKind.INTERVENE, "intervention")


def test_perform_dog_expression_refused_while_lease_active() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._lock = RLock()
    now = time.monotonic()
    curiosity._leases = {
        "L": BehaviorLease(
            owner="intervention",
            lease_id="L",
            behavior=BehaviorKind.INTERVENE,
            priority=40,
            issued_at=now,
            expires_at=now + 100.0,
            reason="suspicious protocol",
        )
    }
    curiosity._battery_soc = 80
    curiosity._explicit_hold_reason = None
    curiosity._dog_expression_name = None
    curiosity._navigation_state = lambda: NavigationState.IDLE
    curiosity._is_following = lambda: False
    started: list[tuple] = []
    curiosity._start_dog_expression = lambda *args: started.append(args) or True

    message = curiosity.perform_dog_expression("WiggleHips")

    assert "intervention" in message
    assert started == []


def test_protocol_reapplies_camera_pitch_after_every_real_wave() -> None:
    skill_obj = _bare_intervention()
    pitch: list[float] = []

    # Four real waves, gaze never confirmed: pitch up once before the loop plus
    # once after each of the four waves, then restored to zero exactly once.
    result = skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _subject: True,
        set_pitch=lambda value: pitch.append(value),
        waver=lambda: True,
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: None,
        clock=lambda: 0.0,
    )

    assert result.waves_performed == 4
    assert pitch == [0.35, 0.35, 0.35, 0.35, 0.35, 0.0]
    assert pitch.count(0.35) == 5  # one before the loop + one per real wave
    assert pitch.count(0.0) == 1  # restored exactly once at the end


def test_protocol_reapplies_pitch_on_early_gaze_exit_path() -> None:
    skill_obj = _bare_intervention()
    pitch: list[float] = []
    gaze_calls = {"n": 0}

    def gaze_check() -> bool:
        gaze_calls["n"] += 1
        return gaze_calls["n"] >= 2  # subject looks back after the second wave

    result = skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _subject: True,
        set_pitch=lambda value: pitch.append(value),
        waver=lambda: True,
        gaze_check=gaze_check,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: None,
        clock=lambda: 0.0,
    )

    # Three real waves (two, then one final wave after the look).
    assert result.subject_looked is True
    assert result.waves_performed == 3
    # Up once before the loop + once after each of the three waves, restore once.
    assert pitch == [0.35, 0.35, 0.35, 0.35, 0.0]
    assert pitch.count(0.35) == 4
    assert pitch.count(0.0) == 1


def test_protocol_pitch_reapply_failure_does_not_abort() -> None:
    skill_obj = _bare_intervention()
    calls: list[float] = []
    released: list[bool] = []

    def failing_pitch(value: float) -> bool:
        calls.append(value)
        return False  # firmware rejects every Euler pose

    # A rejected re-apply must not stop the waves; all four still run and the
    # lease is still released with a final restore attempt.
    result = skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _subject: True,
        set_pitch=failing_pitch,
        waver=lambda: True,
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: released.append(True),
        clock=lambda: 0.0,
    )

    assert result.waves_performed == 4
    assert calls == [0.35, 0.35, 0.35, 0.35, 0.35, 0.0]
    assert released == [True]


# ---------------------------------------------------------------------------
# Uninterruptible escort (escort_to_sleeping_area): lease, retries, cleanup.
# ---------------------------------------------------------------------------


class _FakeClock:
    """A monotonic clock the loop advances only through its injected sleep."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


def test_run_escort_happy_path_finds_area_navigates_and_releases() -> None:
    clock = _FakeClock()
    acquires: list[bool] = []
    renews: list[bool] = []
    releases: list[bool] = []
    goals: list[tuple[float, float]] = []
    spoken: list[str] = []
    # Pose walks from the origin toward the area at (2, 0); it enters the 0.8 m
    # arrival radius at (1.9, 0), so nav never has to report goal-reached.
    poses = iter([(0.0, 0.0), (1.0, 0.0), (1.9, 0.0), (2.0, 0.0)])
    last = {"p": (0.0, 0.0)}

    def get_pose() -> tuple[float, float]:
        try:
            last["p"] = next(poses)
        except StopIteration:
            pass
        return last["p"]

    def acquire() -> dict:
        acquires.append(True)
        return {"accepted": True, "lease": {"lease_id": "L1"}}

    result = run_escort(
        now=clock.now,
        get_pose=get_pose,
        find_area=lambda x, y: {
            "area_id": "sleep",
            "x": 2.0,
            "y": 0.0,
            "area_type": "sleeping_area",
            "source": "session",
        },
        acquire=acquire,
        renew=lambda: renews.append(True),
        release=lambda: releases.append(True),
        send_goal=lambda x, y: goals.append((x, y)),
        arrived=lambda: False,  # rely purely on the distance-to-goal radius
        sleep=clock.sleep,
        speak=lambda text: spoken.append(text),
        arrival_radius_m=0.8,
        tick_s=0.5,
        per_goal_timeout_s=10.0,
        overall_timeout_s=30.0,
        max_goal_attempts=3,
    )

    assert isinstance(result, EscortResult)
    assert result.found is True and result.acquired is True
    assert result.arrived is True
    assert result.area_id == "sleep"
    assert result.source == "session"
    # Lease taken exactly once, one goal sent (arrived on the first attempt),
    # renewed while waiting, and released at the end.
    assert acquires == [True]
    assert goals == [(2.0, 0.0)]
    assert len(renews) >= 1
    assert releases == [True]
    # Arrival line was spoken once.
    assert spoken == ["Here is the sleeping area. Rest well."]
    # Traveled estimate accumulates the pose deltas up to arrival (0 -> 1 -> 1.9).
    assert result.distance_m == pytest.approx(1.9, abs=1e-6)
    assert "arrived at sleeping area 'sleep'" in result.message


def test_run_escort_no_sleeping_area_never_acquires_lease() -> None:
    clock = _FakeClock()
    acquires: list[bool] = []
    releases: list[bool] = []
    goals: list[tuple[float, float]] = []

    result = run_escort(
        now=clock.now,
        get_pose=lambda: (0.0, 0.0),
        find_area=lambda x, y: None,
        acquire=lambda: acquires.append(True)
        or {"accepted": True, "lease": {"lease_id": "L"}},
        renew=lambda: None,
        release=lambda: releases.append(True),
        send_goal=lambda x, y: goals.append((x, y)),
        arrived=lambda: True,
        sleep=clock.sleep,
    )

    assert result.found is False
    assert result.acquired is False
    assert result.arrived is False
    # Motion authority was never touched, and no goal was ever sent.
    assert acquires == []
    assert releases == []
    assert goals == []
    assert "No sleeping area is known yet" in result.message


def test_run_escort_navigation_timeout_retries_three_goals_then_releases() -> None:
    clock = _FakeClock()
    goals: list[tuple[float, float]] = []
    releases: list[bool] = []

    result = run_escort(
        now=clock.now,
        # The pose never moves off the origin, 10 m from the area, so the escort
        # never enters the arrival radius and nav never reports goal-reached.
        get_pose=lambda: (0.0, 0.0),
        find_area=lambda x, y: {
            "area_id": "sleep",
            "x": 10.0,
            "y": 0.0,
            "source": "persistent",
        },
        acquire=lambda: {"accepted": True, "lease": {"lease_id": "L"}},
        renew=lambda: None,
        release=lambda: releases.append(True),
        send_goal=lambda x, y: goals.append((x, y)),
        arrived=lambda: False,
        sleep=clock.sleep,
        arrival_radius_m=0.8,
        tick_s=0.5,
        per_goal_timeout_s=1.0,
        overall_timeout_s=3.0,
        max_goal_attempts=3,
    )

    assert result.arrived is False
    assert result.goal_attempts == 3
    # A fresh goal is the retry; three total sends, then it gives up.
    assert len(goals) == 3
    # The lease is still released despite the timeout (the finally path).
    assert releases == [True]
    assert "without reaching sleeping area 'sleep'" in result.message


def test_run_escort_renews_lease_during_multi_tick_navigation() -> None:
    clock = _FakeClock()
    renews: list[bool] = []
    releases: list[bool] = []
    # Nav reports goal-reached only on the third poll, forcing a multi-tick wait.
    arrivals = iter([False, False, True])

    result = run_escort(
        now=clock.now,
        get_pose=lambda: (0.0, 0.0),
        find_area=lambda x, y: {
            "area_id": "sleep",
            "x": 5.0,
            "y": 0.0,
            "source": "session",
        },
        acquire=lambda: {"accepted": True, "lease": {"lease_id": "L"}},
        renew=lambda: renews.append(True),
        release=lambda: releases.append(True),
        send_goal=lambda x, y: None,
        arrived=lambda: next(arrivals),
        sleep=clock.sleep,
        arrival_radius_m=0.8,
        tick_s=0.5,
        per_goal_timeout_s=10.0,
        overall_timeout_s=30.0,
    )

    assert result.arrived is True
    # The lease was renewed on every wait tick before arrival.
    assert len(renews) >= 2
    assert releases == [True]


def test_run_escort_refused_lease_sends_no_goal_and_releases_nothing() -> None:
    clock = _FakeClock()
    goals: list[tuple[float, float]] = []
    releases: list[bool] = []

    result = run_escort(
        now=clock.now,
        get_pose=lambda: (0.0, 0.0),
        find_area=lambda x, y: {
            "area_id": "sleep",
            "x": 1.0,
            "y": 0.0,
            "source": "session",
        },
        # Another escort already owns motion at priority 60.
        acquire=lambda: {"accepted": False, "current": {"owner": "escort"}},
        renew=lambda: None,
        release=lambda: releases.append(True),
        send_goal=lambda x, y: goals.append((x, y)),
        arrived=lambda: True,
        sleep=clock.sleep,
    )

    assert result.found is True
    assert result.acquired is False
    assert result.arrived is False
    assert goals == []
    # No lease was ever held, so nothing is released.
    assert releases == []
    assert "Cannot start the escort" in result.message


def test_run_escort_speaks_start_line_once_after_acquire_before_arrival() -> None:
    clock = _FakeClock()
    events: list[str] = []

    def acquire() -> dict:
        events.append("acquire")
        return {"accepted": True, "lease": {"lease_id": "L"}}

    # Nav reports goal-reached only on the second poll, so there is a wait window
    # between the start announcement and arrival.
    arrivals = iter([False, True])

    result = run_escort(
        now=clock.now,
        get_pose=lambda: (0.0, 0.0),
        find_area=lambda x, y: {
            "area_id": "sleep",
            "x": 5.0,
            "y": 0.0,
            "source": "session",
        },
        acquire=acquire,
        renew=lambda: None,
        release=lambda: events.append("release"),
        send_goal=lambda x, y: events.append("goal"),
        arrived=lambda: next(arrivals),
        sleep=clock.sleep,
        speak=lambda text: events.append(f"speak:{text}"),
        start_line="wake-up line",
        arrival_line="arrival line",
        arrival_radius_m=0.8,
        tick_s=0.5,
        per_goal_timeout_s=10.0,
        overall_timeout_s=30.0,
    )

    assert result.arrived is True
    # The start line is spoken exactly once.
    assert events.count("speak:wake-up line") == 1
    # Order: lease acquired, first goal sent, THEN the start line, before arrival.
    assert (
        events.index("acquire")
        < events.index("goal")
        < events.index("speak:wake-up line")
        < events.index("speak:arrival line")
    )


def test_run_escort_start_speech_failure_never_changes_result() -> None:
    clock = _FakeClock()
    releases: list[bool] = []

    def boom(_text: str) -> None:
        raise RuntimeError("bluetooth speaker dropped")

    result = run_escort(
        now=clock.now,
        get_pose=lambda: (0.0, 0.0),
        # Area within the arrival radius: it arrives on the first poll.
        find_area=lambda x, y: {
            "area_id": "sleep",
            "x": 0.2,
            "y": 0.0,
            "source": "session",
        },
        acquire=lambda: {"accepted": True, "lease": {"lease_id": "L"}},
        renew=lambda: None,
        release=lambda: releases.append(True),
        send_goal=lambda x, y: None,
        arrived=lambda: False,
        sleep=clock.sleep,
        speak=boom,  # every utterance raises
        start_line="start",
        arrival_line="arrive",
        arrival_radius_m=0.8,
    )

    # Both speeches raised, yet the escort still arrived and released cleanly.
    assert result.arrived is True
    assert result.acquired is True
    assert result.area_id == "sleep"
    assert releases == [True]


def test_run_escort_uses_overridden_start_text() -> None:
    clock = _FakeClock()
    spoken: list[str] = []

    result = run_escort(
        now=clock.now,
        get_pose=lambda: (0.0, 0.0),
        find_area=lambda x, y: {
            "area_id": "sleep",
            "x": 0.1,
            "y": 0.0,
            "source": "session",
        },
        acquire=lambda: {"accepted": True, "lease": {"lease_id": "L"}},
        renew=lambda: None,
        release=lambda: None,
        send_goal=lambda x, y: None,
        arrived=lambda: True,
        sleep=clock.sleep,
        speak=lambda text: spoken.append(text),
        start_line="custom start announcement",
        arrival_line="custom arrival",
        arrival_radius_m=0.8,
    )

    assert result.arrived is True
    # The injected (config-driven) start text is spoken first, verbatim.
    assert spoken[0] == "custom start announcement"
    assert "custom arrival" in spoken
    # The default config ships the natural-Chinese TTS line, and it is a field
    # the skill passes through as start_line, so operators can override it.
    assert "休息区" in EscortConfig().start_text


def test_run_escort_ignores_stale_latched_goal_reached_flag() -> None:
    # The planner latches goal_reached=True after any completed leg and only
    # resets it when the new goal is ingested on the planner worker. A finished
    # patrol leg right before an escort therefore leaves the flag True, and the
    # first arrival poll must not turn that into an instant fake arrival.
    clock = _FakeClock()
    releases: list[bool] = []
    # Stale True from the previous leg, then the planner ingests the escort
    # goal (False), then the dog genuinely arrives (True).
    flag_values = iter([True, True, False, False, True])
    poses = iter([(0.0, 0.0), (0.5, 0.0), (1.5, 0.0), (3.0, 0.0), (4.5, 0.0)])
    last = {"p": (0.0, 0.0)}

    def get_pose() -> tuple[float, float]:
        try:
            last["p"] = next(poses)
        except StopIteration:
            pass
        return last["p"]

    result = run_escort(
        now=clock.now,
        get_pose=get_pose,
        find_area=lambda x, y: {
            "area_id": "sleep",
            "x": 10.0,
            "y": 0.0,
            "source": "persistent",
        },
        acquire=lambda: {"accepted": True, "lease": {"lease_id": "L"}},
        renew=lambda: None,
        release=lambda: releases.append(True),
        send_goal=lambda x, y: None,
        arrived=lambda: next(flag_values, True),
        sleep=clock.sleep,
        arrival_radius_m=0.8,
        tick_s=0.5,
        per_goal_timeout_s=20.0,
        overall_timeout_s=30.0,
    )

    assert result.arrived is True
    # The stale True polls were not trusted: the escort kept walking and only
    # completed after the flag reset (False) and re-asserted (True), so real
    # distance was covered instead of an instant 0.0 m arrival.
    assert result.distance_m > 0.0
    assert releases == [True]


def test_run_escort_never_arrives_on_permanently_stale_flag() -> None:
    # If the planner never ingests the goal (the flag stays latched True) and
    # the dog never physically approaches the area, the escort must time out
    # rather than register a nap that never happened.
    clock = _FakeClock()
    goals: list[tuple[float, float]] = []

    result = run_escort(
        now=clock.now,
        get_pose=lambda: (0.0, 0.0),
        find_area=lambda x, y: {
            "area_id": "sleep",
            "x": 10.0,
            "y": 0.0,
            "source": "persistent",
        },
        acquire=lambda: {"accepted": True, "lease": {"lease_id": "L"}},
        renew=lambda: None,
        release=lambda: None,
        send_goal=lambda x, y: goals.append((x, y)),
        arrived=lambda: True,  # latched forever; never observed False
        sleep=clock.sleep,
        arrival_radius_m=0.8,
        tick_s=0.5,
        per_goal_timeout_s=2.0,
        overall_timeout_s=6.0,
        max_goal_attempts=3,
    )

    assert result.arrived is False
    # Every per-goal window expired and was retried with a fresh goal.
    assert len(goals) == 3
    assert result.distance_m == 0.0
    assert "without reaching sleeping area" in result.message


def test_operator_sleep_area_action_and_button_are_wired() -> None:
    # The console button maps to the escort skill with no arguments.
    assert _OPERATOR_ACTIONS["sleep_area"] == {
        "tool": "escort_to_sleeping_area",
        "args": {},
    }
    # The operator page renders the button that triggers it.
    assert "act('sleep_area')" in _OPERATOR_HTML
    assert "Take me to the sleeping area" in _OPERATOR_HTML


# ---------------------------------------------------------------------------
# Drowsiness assessment ingestion: POST sink + operator status + ring buffer.
# ---------------------------------------------------------------------------


def _assessment_web_interface() -> LatestFrameRobotWebInterface:
    # No port is bound until run(); the app is exercised through a TestClient.
    return LatestFrameRobotWebInterface(
        port=5599,
        text_streams={},
        audio_subject=None,
        camera=None,
        operator_action=lambda _action: True,
        operator_status=lambda: {"behavior": "explore", "owner": "curiosity"},
    )


def test_operator_assessment_post_status_round_trip() -> None:
    client = TestClient(_assessment_web_interface().app)

    # A valid FatigueAssessment body is accepted; extra fields are tolerated.
    r = client.post(
        "/operator/assessment",
        json={
            "assessment_id": "a1",
            "ts": 1000.0,
            "track_id": "t7",
            "fatigue_score": 0.82,
            "confidence": 0.9,
            "quality": 0.7,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    status = client.get("/operator/status").json()
    # Status carries the assessments alongside the usual robot fields.
    assert status["behavior"] == "explore"
    assert status["assessment_count"] == 1
    assert len(status["assessments"]) == 1
    latest = status["assessments"][0]
    assert latest["track_id"] == "t7"
    assert latest["fatigue_score"] == 0.82
    # age_s is computed server-side, so the page needs no clock reconciliation.
    assert latest["age_s"] is not None and latest["age_s"] >= 0.0


def test_operator_assessment_rejects_malformed_body_with_400() -> None:
    client = TestClient(_assessment_web_interface().app)

    # Missing a required field.
    r = client.post(
        "/operator/assessment",
        json={"assessment_id": "a2", "ts": 1.0, "track_id": "t", "fatigue_score": 0.5},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "confidence" in body["error"]

    # A numeric field that is not a number.
    r = client.post(
        "/operator/assessment",
        json={
            "assessment_id": "a3",
            "ts": "soon",
            "track_id": "t",
            "fatigue_score": 0.5,
            "confidence": 0.9,
        },
    )
    assert r.status_code == 400
    assert "ts" in r.json()["error"]

    # A body that is not a JSON object at all.
    r = client.post("/operator/assessment", json=[1, 2, 3])
    assert r.status_code == 400

    # Nothing malformed was ever stored.
    status = client.get("/operator/status").json()
    assert status["assessment_count"] == 0
    assert status["assessments"] == []

    # The validator itself never raises and reports a reason.
    record, error = _validate_assessment("not a dict")
    assert record is None and error is not None


def test_operator_assessment_ring_buffer_caps_at_fifty_newest_first() -> None:
    client = TestClient(_assessment_web_interface().app)

    for i in range(60):
        r = client.post(
            "/operator/assessment",
            json={
                "assessment_id": f"x{i}",
                "ts": 2000.0 + i,
                "track_id": "tt",
                "fatigue_score": 0.1,
                "confidence": 0.2,
            },
        )
        assert r.status_code == 200

    status = client.get("/operator/status").json()
    # The buffer is bounded at 50 no matter how many were posted.
    assert status["assessment_count"] == 50
    # Only the latest 5 are surfaced, newest first.
    ids = [a["assessment_id"] for a in status["assessments"]]
    assert ids == ["x59", "x58", "x57", "x56", "x55"]


def _intervention_with_follow(following: bool, *, stop_releases: bool):
    """Build a bare InterventionSkill with fake follow + curiosity call logs.

    ``_run_protocol`` is stubbed so the test exercises only the follow-standdown
    preamble and lease-acquisition ordering, never the real wave loop.
    """
    from nightwatch.intervene import InterventionResult

    skill_obj = object.__new__(InterventionSkill)
    skill_obj.config = SimpleNamespace(
        follow_release_timeout_s=0.0,
        follow_release_poll_s=0.0,
        lease_priority=40,
        lease_ttl_s=15.0,
        body_pitch_rad=0.35,
        max_waves=4,
        max_wave_attempts=8,
        gaze_window_s=3.0,
        gaze_frames_required=3,
        gaze_poll_s=0.15,
    )
    log: list[tuple] = []
    state = {"following": following}

    def is_following() -> bool:
        log.append(("is_following", state["following"]))
        return state["following"]

    def stop_following() -> str:
        log.append(("stop_following",))
        if stop_releases:
            state["following"] = False
        return "Stopped following."

    skill_obj._follow = SimpleNamespace(
        is_following=is_following, stop_following=stop_following
    )

    def acquire_behavior(owner, behavior, priority, ttl_s, reason):
        log.append(("acquire", owner, behavior))
        return {"accepted": True, "lease": {"lease_id": "L"}}

    skill_obj._curiosity = SimpleNamespace(
        acquire_behavior=acquire_behavior,
        renew_behavior=lambda *_a: True,
        release_behavior=lambda *_a: True,
    )
    skill_obj._connection = SimpleNamespace()
    skill_obj._find_subject = lambda: object()

    def fake_run_protocol(**_kwargs):
        log.append(("run_protocol",))
        return InterventionResult(
            found=True,
            waves_performed=1,
            blocked_attempts=0,
            subject_looked=False,
            duration_s=0.1,
            message="done",
        )

    skill_obj._run_protocol = fake_run_protocol
    return skill_obj, log


def test_potential_detected_stops_active_follow_before_acquiring_lease() -> None:
    skill_obj, log = _intervention_with_follow(True, stop_releases=True)

    skill_obj.potential_detected("person")

    names = [entry[0] for entry in log]
    assert "stop_following" in names
    assert "acquire" in names
    # The follow is stood down BEFORE the intervene lease is taken.
    assert names.index("stop_following") < names.index("acquire")
    # And the protocol actually runs after acquiring.
    assert names.index("acquire") < names.index("run_protocol")


def test_potential_detected_does_not_stop_follow_when_idle() -> None:
    skill_obj, log = _intervention_with_follow(False, stop_releases=False)

    skill_obj.potential_detected("person")

    names = [entry[0] for entry in log]
    assert "stop_following" not in names
    # It still checks follow state, then acquires and runs.
    assert names == ["is_following", "acquire", "run_protocol"]


def test_potential_detected_refuses_when_follow_will_not_release() -> None:
    skill_obj, log = _intervention_with_follow(True, stop_releases=False)

    message = skill_obj.potential_detected("person")

    names = [entry[0] for entry in log]
    # Follow was asked to stop but never released, so the lease is never taken.
    assert "stop_following" in names
    assert "acquire" not in names
    assert "run_protocol" not in names
    assert "person_follow" in message


def test_protocol_speaks_greeting_after_arrival_before_arc() -> None:
    skill_obj = _bare_intervention()
    log: list = []

    result = skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _s: log.append("approach") or True,
        set_pitch=lambda _v: None,
        waver=lambda: False,  # all blocked -> loop exits at the attempt cap
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: None,
        greet=lambda: log.append("greet"),
        face_check=lambda: log.append("face_check") or False,
        arc_step=lambda hop: log.append(("arc", hop)),
        clock=lambda: 0.0,
    )

    assert log.count("greet") == 1
    # Greeting is announced AFTER arrival and BEFORE the face-seeking arc, so
    # the subject hears the dog while it repositions.
    assert log.index("approach") < log.index("greet")
    assert log.index("greet") < log.index("face_check")
    assert result.found is True


def test_protocol_unaffected_when_greeting_speech_raises() -> None:
    skill_obj = _bare_intervention()

    def boom() -> None:
        raise RuntimeError("no speaker attached")

    result = skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _s: True,
        set_pitch=lambda _v: None,
        waver=lambda: True,
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: None,
        greet=boom,
        clock=lambda: 0.0,
    )

    # Greeting failure is swallowed; the protocol still runs its four waves.
    assert result.found is True
    assert result.waves_performed == 4


def test_personalized_comment_spoken_after_arrival_before_first_wave() -> None:
    skill_obj = _bare_intervention()
    log: list = []

    def start_comment():
        log.append("start_comment")

        def take() -> str:
            log.append("take_comment")
            return "你的红色外套很好看"

        return take

    def waver() -> bool:
        log.append("wave")
        return True

    skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _s: log.append("approach") or True,
        set_pitch=lambda _v: None,
        waver=waver,
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: None,
        start_comment=start_comment,
        speak_comment=lambda text: log.append(("speak", text)),
        clock=lambda: 0.0,
    )

    assert ("speak", "你的红色外套很好看") in log
    # Vision kicked off before the approach (parallel), retrieved after arrival.
    assert log.index("start_comment") < log.index("approach")
    assert log.index("approach") < log.index("take_comment")
    # Spoken after arrival but before the first wave.
    speak_idx = next(
        i for i, e in enumerate(log) if isinstance(e, tuple) and e[0] == "speak"
    )
    assert speak_idx < log.index("wave")


def test_personalized_comment_failure_speaks_nothing_and_does_not_fail() -> None:
    skill_obj = _bare_intervention()
    spoken: list = []

    def start_comment_timeout():
        def take() -> None:
            return None  # timed out / errored -> no comment

        return take

    result = skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _s: True,
        set_pitch=lambda _v: None,
        waver=lambda: True,
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: None,
        start_comment=start_comment_timeout,
        speak_comment=lambda text: spoken.append(text),
        clock=lambda: 0.0,
    )

    assert spoken == []
    assert result.found is True
    assert result.waves_performed == 4

    # A take() that raises is also swallowed with nothing spoken.
    def start_comment_raise():
        def take() -> str:
            raise TimeoutError("vision took too long")

        return take

    spoken.clear()
    result2 = skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _s: True,
        set_pitch=lambda _v: None,
        waver=lambda: True,
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: None,
        start_comment=start_comment_raise,
        speak_comment=lambda text: spoken.append(text),
        clock=lambda: 0.0,
    )
    assert spoken == []
    assert result2.waves_performed == 4


def test_comment_job_never_calls_vision_when_flag_disabled() -> None:
    skill_obj = _bare_intervention()
    skill_obj.config.personalized_comment = False
    skill_obj.config.comment_timeout_s = 2.0
    called: list = []

    take = skill_obj._start_comment_job(lambda: called.append(True) or "unused")

    assert take() is None
    assert called == []  # vision model never invoked


def test_comment_job_returns_sentence_and_is_fail_silent() -> None:
    skill_obj = _bare_intervention()
    skill_obj.config.personalized_comment = True
    skill_obj.config.comment_timeout_s = 2.0

    take = skill_obj._start_comment_job(lambda: "你的帽子很酷")
    assert take() == "你的帽子很酷"

    def boom() -> str:
        raise RuntimeError("vision backend down")

    take_failing = skill_obj._start_comment_job(boom)
    assert take_failing() is None


# --- SpeakSkill natural-TTS backend chain -----------------------------------


class _FakeTtsBackend:
    """Test double for a TTS backend in SpeakSkill's chain.

    Records the utterances handed to synth(); can pretend to be unavailable
    (ready() False), to fail (raises), and to either produce a file path or
    play itself (produced=None, like `say`).
    """

    def __init__(self, name, ready=True, raises=False, produced="PATH"):
        self.name = name
        self._ready = ready
        self._raises = raises
        self._produced = produced
        self.calls = []

    def ready(self):
        return self._ready

    def synth(self, text, out_path):
        self.calls.append(text)
        if self._raises:
            raise RuntimeError(f"{self.name} boom")
        return self._produced


def _speak_skill_for_chain(backends, monkeypatch, mode, cache_dir=None):
    monkeypatch.setenv("NIGHTWATCH_TTS", mode)
    skill_obj = object.__new__(SpeakSkill)
    skill_obj._backends = {b.name: b for b in backends}
    skill_obj._tmpdir = "/tmp"
    skill_obj._utt_counter = 0
    skill_obj._cache_dir = str(cache_dir) if cache_dir is not None else None
    skill_obj._kokoro_voice = "zf_test"
    played: list[str] = []
    skill_obj._play = lambda path: played.append(path)
    return skill_obj, played


def test_speak_chain_order_per_mode() -> None:
    assert _chain_for_mode("auto") == ["kokoro", "edge", "say"]
    assert _chain_for_mode(None) == ["kokoro", "edge", "say"]
    assert _chain_for_mode("kokoro") == ["kokoro", "say"]
    assert _chain_for_mode("edge") == ["edge", "say"]
    # Non-say modes always keep `say` as the final safety net; `say` mode is
    # only `say`.
    assert _chain_for_mode("say") == ["say"]


def test_speak_selection_respects_env_override(monkeypatch) -> None:
    kokoro = _FakeTtsBackend("kokoro")
    edge = _FakeTtsBackend("edge")
    say = _FakeTtsBackend("say", produced=None)
    skill_obj, played = _speak_skill_for_chain([kokoro, edge, say], monkeypatch, "edge")

    assert skill_obj._speak_now("hi") == "edge"
    # Explicit override skips the preferred backend entirely.
    assert kokoro.calls == []
    assert edge.calls == ["hi"]
    # edge succeeded, so the `say` fallback is never reached.
    assert say.calls == []


def test_speak_say_override_uses_only_say(monkeypatch) -> None:
    kokoro = _FakeTtsBackend("kokoro")
    edge = _FakeTtsBackend("edge")
    say = _FakeTtsBackend("say", produced=None)
    skill_obj, _ = _speak_skill_for_chain([kokoro, edge, say], monkeypatch, "say")

    assert skill_obj._speak_now("hi") == "say"
    assert kokoro.calls == []
    assert edge.calls == []
    assert say.calls == ["hi"]


def test_speak_auto_falls_through_kokoro_edge_say(monkeypatch) -> None:
    # Kokoro not warm yet, edge network dead, say saves the utterance.
    kokoro = _FakeTtsBackend("kokoro", ready=False)
    edge = _FakeTtsBackend("edge", raises=True)
    say = _FakeTtsBackend("say", produced=None)
    skill_obj, played = _speak_skill_for_chain([kokoro, edge, say], monkeypatch, "auto")

    assert skill_obj._speak_now("hi") == "say"
    # Cold kokoro is skipped without a synth attempt.
    assert kokoro.calls == []
    # edge was attempted (and raised), then say handled it.
    assert edge.calls == ["hi"]
    assert say.calls == ["hi"]
    # `say` plays itself (produced=None), so afplay is never invoked.
    assert played == []


def test_speak_auto_prefers_kokoro_when_warm(monkeypatch) -> None:
    kokoro = _FakeTtsBackend("kokoro", produced="/tmp/utt.wav")
    edge = _FakeTtsBackend("edge")
    say = _FakeTtsBackend("say", produced=None)
    skill_obj, played = _speak_skill_for_chain([kokoro, edge, say], monkeypatch, "auto")

    assert skill_obj._speak_now("hi") == "kokoro"
    assert kokoro.calls == ["hi"]
    # Kokoro won, so the rest of the chain is untouched.
    assert edge.calls == []
    assert say.calls == []
    # A file-producing backend is played through afplay.
    assert played == ["/tmp/utt.wav"]


def test_speak_backend_raising_falls_through_not_propagates(monkeypatch) -> None:
    kokoro = _FakeTtsBackend("kokoro", raises=True)
    edge = _FakeTtsBackend("edge", raises=True)
    say = _FakeTtsBackend("say", produced=None)
    skill_obj, _ = _speak_skill_for_chain([kokoro, edge, say], monkeypatch, "auto")

    # A raising backend must never propagate out of speak; the chain absorbs it.
    assert skill_obj._speak_now("hi") == "say"
    assert kokoro.calls == ["hi"]
    assert edge.calls == ["hi"]
    assert say.calls == ["hi"]


def test_speak_all_backends_failing_returns_none_quietly(monkeypatch) -> None:
    kokoro = _FakeTtsBackend("kokoro", raises=True)
    edge = _FakeTtsBackend("edge", raises=True)
    say = _FakeTtsBackend("say", raises=True)
    skill_obj, _ = _speak_skill_for_chain([kokoro, edge, say], monkeypatch, "auto")

    # Every backend down: no exception escapes, result is just None.
    assert skill_obj._speak_now("hi") is None


class _FileWritingKokoro(_FakeTtsBackend):
    """Kokoro double that actually writes a wav file, like the real backend."""

    def __init__(self):
        super().__init__("kokoro")

    def synth(self, text, out_path):
        self.calls.append(text)
        with open(out_path, "wb") as f:
            f.write(b"RIFFfake-wav-bytes")
        return out_path


def test_speak_cache_hit_skips_synthesis_entirely(monkeypatch, tmp_path) -> None:
    kokoro = _FakeTtsBackend("kokoro")
    say = _FakeTtsBackend("say", produced=None)
    skill_obj, played = _speak_skill_for_chain(
        [kokoro, say], monkeypatch, "auto", cache_dir=tmp_path
    )
    cached = skill_obj._cache_path("你好")
    with open(cached, "wb") as f:
        f.write(b"RIFFcached")

    assert skill_obj._speak_now("你好") == "cache"
    # The cached wav plays directly; no backend is ever asked to synthesize.
    assert played == [cached]
    assert kokoro.calls == []
    assert say.calls == []


def test_speak_kokoro_output_lands_in_cache_and_replays(
    monkeypatch, tmp_path
) -> None:
    kokoro = _FileWritingKokoro()
    say = _FakeTtsBackend("say", produced=None)
    skill_obj, played = _speak_skill_for_chain(
        [kokoro, say], monkeypatch, "auto", cache_dir=tmp_path
    )
    skill_obj._tmpdir = str(tmp_path / "tmp")
    os.makedirs(skill_obj._tmpdir)

    # First utterance synthesizes, and the wav is moved into the cache.
    assert skill_obj._speak_now("你好") == "kokoro"
    cached = skill_obj._cache_path("你好")
    assert os.path.isfile(cached)
    assert played == [cached]

    # The repeat is a cache hit: no second synthesis.
    assert skill_obj._speak_now("你好") == "cache"
    assert kokoro.calls == ["你好"]


def test_speak_say_mode_ignores_kokoro_cache(monkeypatch, tmp_path) -> None:
    say = _FakeTtsBackend("say", produced=None)
    skill_obj, played = _speak_skill_for_chain(
        [say], monkeypatch, "say", cache_dir=tmp_path
    )
    with open(skill_obj._cache_path("你好"), "wb") as f:
        f.write(b"RIFFcached")

    # Explicit `say` mode means `say`, even when a cached wav exists.
    assert skill_obj._speak_now("你好") == "say"
    assert say.calls == ["你好"]
    assert played == []


def test_speak_overlong_text_is_not_cached(monkeypatch, tmp_path) -> None:
    kokoro = _FileWritingKokoro()
    skill_obj, _played = _speak_skill_for_chain(
        [kokoro], monkeypatch, "kokoro", cache_dir=tmp_path
    )
    skill_obj._tmpdir = str(tmp_path / "tmp")
    os.makedirs(skill_obj._tmpdir)

    long_text = "长" * 201
    assert skill_obj._speak_now(long_text) == "kokoro"
    # Nothing landed in the cache dir (the synth tmp file stays in tmpdir).
    assert os.listdir(tmp_path) == ["tmp"]


def test_prewarm_presets_synthesizes_each_line_once(monkeypatch, tmp_path) -> None:
    kokoro = _FileWritingKokoro()
    skill_obj, _played = _speak_skill_for_chain(
        [kokoro], monkeypatch, "auto", cache_dir=tmp_path
    )
    skill_obj._tmpdir = str(tmp_path / "tmp")
    os.makedirs(skill_obj._tmpdir)
    skill_obj._stopping = False

    skill_obj._prewarm_presets()
    assert sorted(kokoro.calls) == sorted(voice_presets.PRESET_LINES)
    for line in voice_presets.PRESET_LINES:
        assert skill_obj._cached_wav(line) is not None

    # A second prewarm (e.g. next process start) finds everything cached.
    skill_obj._prewarm_presets()
    assert len(kokoro.calls) == len(voice_presets.PRESET_LINES)


def test_speak_queue_drops_oldest_beyond_two_pending() -> None:
    skill_obj = object.__new__(SpeakSkill)
    skill_obj._pending = deque()
    skill_obj._max_pending = 2
    skill_obj._queue_cv = Condition()

    for line in ("a", "b", "c", "d"):
        skill_obj._enqueue(line)

    # Only the two most recent survive; the oldest are dropped, not queued.
    assert list(skill_obj._pending) == ["c", "d"]


def test_speak_enqueues_and_returns_immediately() -> None:
    skill_obj = object.__new__(SpeakSkill)
    skill_obj._pending = deque()
    skill_obj._max_pending = 2
    skill_obj._queue_cv = Condition()

    result = skill_obj.speak("你好")

    assert result == "Speaking: 你好"
    assert list(skill_obj._pending) == ["你好"]


def test_speak_ignores_blank_text() -> None:
    skill_obj = object.__new__(SpeakSkill)
    skill_obj._pending = deque()
    skill_obj._max_pending = 2
    skill_obj._queue_cv = Condition()

    assert skill_obj.speak("   ") == "(nothing to speak)"
    assert list(skill_obj._pending) == []


def _run_with_face_seek(*, faces, log):
    """Drive _run_protocol with an injected face-seek sequence and wave log.

    ``faces`` is the sequence returned by successive face_check() calls
    (initial check, then one after each hop). Waves always succeed so the wave
    loop runs to its cap; the shared ``log`` records arc hops and waves.
    """
    skill_obj = _bare_intervention()
    face_iter = iter(faces)
    return skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _s: log.append("approach") or True,
        set_pitch=lambda _v: None,
        waver=lambda: log.append("wave") or True,
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: None,
        face_check=lambda: next(face_iter),
        arc_step=lambda hop: log.append(("arc", hop)),
        clock=lambda: 0.0,
    )


def test_face_seek_arcs_until_frontal_face_then_waves() -> None:
    log: list = []
    # Back view at the standoff and after hop 1; frontal face after hop 2.
    result = _run_with_face_seek(faces=[False, False, True], log=log)

    hops = [entry for entry in log if isinstance(entry, tuple) and entry[0] == "arc"]
    assert hops == [("arc", 0), ("arc", 1)]  # stopped the moment a face appeared
    assert result.face_found is True
    assert log.count("wave") == 4  # waves proceed after the face is found
    assert log.index(("arc", 1)) < log.index("wave")  # arc before waving


def test_face_seek_gives_up_after_max_hops_and_still_waves() -> None:
    log: list = []
    # A face is never seen: initial plus three post-hop checks all fail.
    result = _run_with_face_seek(faces=[False, False, False, False], log=log)

    hops = [entry for entry in log if isinstance(entry, tuple) and entry[0] == "arc"]
    assert hops == [("arc", 0), ("arc", 1), ("arc", 2)]  # full three-hop arc
    assert result.face_found is False
    assert log.count("wave") == 4  # protocol proceeds to wave anyway


def test_face_seek_frontal_face_immediately_means_zero_hops() -> None:
    log: list = []
    result = _run_with_face_seek(faces=[True], log=log)

    assert not any(isinstance(e, tuple) and e[0] == "arc" for e in log)  # no hops
    assert result.face_found is True
    assert log.count("wave") == 4


def test_face_seek_arc_step_failure_never_aborts_protocol() -> None:
    skill_obj = _bare_intervention()

    def failing_arc(_hop: int) -> None:
        raise RuntimeError("goal send failed")

    result = skill_obj._run_protocol(
        find_subject=lambda: object(),
        approach=lambda _s: True,
        set_pitch=lambda _v: None,
        waver=lambda: True,
        gaze_check=lambda: False,
        rearm=lambda: None,
        renew=lambda: True,
        release=lambda: None,
        face_check=lambda: False,  # never a face -> arcs the full three hops
        arc_step=failing_arc,
        clock=lambda: 0.0,
    )

    # Every hop raised and was swallowed; the protocol still waved four times.
    assert result.face_found is False
    assert result.waves_performed == 4


# --- permanent keep-out zones (the window fix) ---------------------------------


def _keep_out_explorer(tmp_path, transform=(0.0, 0.0, 0.0)):
    """Ranking explorer extended with keep-out state and a fake transform."""
    explorer = _ranking_explorer()
    explorer._keep_outs = []
    explorer._keep_out_path = str(tmp_path / "keepout.json")
    explorer._world_to_map_cache = transform
    # Far-future cache expiry so no reloc RPC is attempted.
    explorer._world_to_map_cache_until = float("inf")
    return explorer


def test_keep_out_circle_discards_frontier_inside_it(tmp_path) -> None:
    explorer = _keep_out_explorer(tmp_path)
    # Identity transform: map coords equal world coords.
    explorer._keep_outs = [{"x": 4.0, "y": 0.0, "radius_m": 2.0, "created": 0.0}]
    costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8), resolution=0.05
    )
    inside = Vector3(4.0, 0.0, 0.0)
    outside = Vector3(0.0, 8.0, 0.0)
    ranked = explorer._rank_frontiers(
        [inside, outside], [40, 40], Vector3(0.0, 0.0, 0.0), costmap
    )
    assert [(round(f.x, 1), round(f.y, 1)) for f in ranked] == [(0.0, 8.0)]


def test_keep_out_circle_discards_route_crossing_it(tmp_path) -> None:
    explorer = _keep_out_explorer(tmp_path)
    explorer._keep_outs = [{"x": 5.0, "y": 5.0, "radius_m": 1.0, "created": 0.0}]
    costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8), resolution=0.05
    )

    ranked = explorer._rank_frontiers(
        [Vector3(8.0, 5.0, 0.0), Vector3(2.0, 9.0, 0.0)],
        [40, 40],
        Vector3(2.0, 5.0, 0.0),
        costmap,
    )

    assert [(round(f.x, 1), round(f.y, 1)) for f in ranked] == [(2.0, 9.0)]


def test_keep_out_circle_allows_route_exiting_it(tmp_path) -> None:
    explorer = _keep_out_explorer(tmp_path)
    explorer._keep_outs = [{"x": 0.0, "y": 0.0, "radius_m": 1.0, "created": 0.0}]
    costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8), resolution=0.05
    )

    ranked = explorer._rank_frontiers(
        [Vector3(3.0, 0.0, 0.0)],
        [40],
        Vector3(0.0, 0.0, 0.0),
        costmap,
    )

    assert [(round(f.x, 1), round(f.y, 1)) for f in ranked] == [(3.0, 0.0)]


def test_keep_out_dormant_without_transform(tmp_path) -> None:
    explorer = _keep_out_explorer(tmp_path, transform=None)
    explorer._keep_outs = [{"x": 4.0, "y": 0.0, "radius_m": 2.0, "created": 0.0}]
    costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8), resolution=0.05
    )
    inside = Vector3(4.0, 0.0, 0.0)
    ranked = explorer._rank_frontiers([inside], [40], Vector3(0.0, 0.0, 0.0), costmap)
    # No transform: keep-outs cannot be anchored, frontier stays selectable.
    assert len(ranked) == 1


def test_nearby_prelock_world_keep_out_stays_active_after_egress(tmp_path) -> None:
    explorer = _keep_out_explorer(tmp_path, transform=None)
    explorer._keep_outs = [
        {
            "x": -2.0,
            "y": -1.0,
            "radius_m": 1.5,
            "created": 0.0,
            "world_x": 0.0,
            "world_y": -1.5,
        }
    ]
    explorer.latest_odometry = SimpleNamespace(position=SimpleNamespace(x=0.0, y=0.0))
    assert explorer._keep_out_world_circles() == [(0.0, -1.5, 1.5)]

    explorer.latest_odometry = SimpleNamespace(position=SimpleNamespace(x=10.0, y=10.0))
    assert explorer._keep_out_world_circles() == [(0.0, -1.5, 1.5)]


def test_matching_odometry_epoch_restores_distant_keep_out() -> None:
    circles = [
        {
            "x": 2.0,
            "y": 3.0,
            "radius_m": 1.5,
            "world_x": 2.0,
            "world_y": 3.0,
            "odom_epoch": "same-boot",
        }
    ]
    assert _prelock_world_keep_outs(
        circles, (20.0, 20.0), "same-boot"
    ) == [(2.0, 3.0, 1.5)]
    assert _prelock_world_keep_outs(circles, (20.0, 20.0), "new-boot") == []


def test_odometry_epoch_survives_restart_but_rotates_after_pose_reset(tmp_path) -> None:
    path = str(tmp_path / "odom_epoch.json")
    first = _resolve_odom_epoch((10.0, -2.0), path=path, now=100.0)
    continued = _resolve_odom_epoch((11.0, -2.5), path=path, now=110.0)
    reset = _resolve_odom_epoch((0.0, 0.0), path=path, now=120.0)
    assert continued == first
    assert reset != first


def test_mark_keep_out_here_pre_lock_is_provisional_not_refused(tmp_path) -> None:
    explorer = _keep_out_explorer(tmp_path, transform=None)
    explorer.latest_odometry = SimpleNamespace(position=SimpleNamespace(x=1.0, y=2.0))
    message = explorer.mark_keep_out_here(radius_m=3.0)
    assert "active NOW" in message
    # Nothing persisted yet (no map anchor), but the zone applies in-session.
    assert explorer._keep_outs == []
    assert explorer._pending_world_keep_outs == [(1.0, 2.0, 3.0)]


def test_mark_keep_out_here_writes_and_applies(tmp_path) -> None:
    import json as _json

    explorer = _keep_out_explorer(tmp_path)
    explorer.latest_odometry = SimpleNamespace(position=SimpleNamespace(x=1.0, y=2.0))
    message = explorer.mark_keep_out_here(radius_m=3.0)
    assert "off limits forever" in message
    assert len(explorer._keep_outs) == 1
    on_disk = _json.loads((tmp_path / "keepout.json").read_text())
    assert on_disk[0]["radius_m"] == 3.0
    assert abs(on_disk[0]["x"] - 1.0) < 1e-6 and abs(on_disk[0]["y"] - 2.0) < 1e-6


def test_struck_out_region_promotes_to_permanent_keep_out(tmp_path) -> None:
    import json as _json

    explorer = _keep_out_explorer(tmp_path)
    with explorer._coverage_lock:
        for _ in range(explorer._strike_limit):
            explorer._add_region_strike_locked(4.0, 0.0)
    assert len(explorer._keep_outs) == 1
    on_disk = _json.loads((tmp_path / "keepout.json").read_text())
    assert on_disk[0]["radius_m"] == explorer._strike_region_m


def test_arrival_without_resolution_strikes_the_region(tmp_path) -> None:
    explorer = _keep_out_explorer(tmp_path, transform=None)
    explorer._invalidate_radius_m = 0.9
    explorer.goal_reached_event = Event()
    # Goal sits beside a large permanently-unknown region (the glass case).
    explorer.latest_costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8), resolution=0.05
    )
    explorer._active_goal = Vector3(4.0, 0.0, 0.0)
    explorer._on_goal_reached(SimpleNamespace(data=True))
    key = explorer._strike_region_key(4.0, 0.0)
    assert explorer._region_strikes[key] == 1
    assert explorer._active_goal is None
    assert explorer.goal_reached_event.is_set()


def test_hard_failure_at_unresolved_frontier_persists_epoch_keep_out(tmp_path) -> None:
    explorer = _keep_out_explorer(tmp_path, transform=None)
    explorer._odom_epoch_id = "same-boot"
    explorer._invalidate_radius_m = 0.9
    explorer.goal_reached_event = Event()
    explorer.latest_costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8), resolution=0.05
    )
    explorer._active_goal = Vector3(4.0, 0.0, 0.0)

    explorer._on_goal_reached(SimpleNamespace(data=False))

    key = explorer._strike_region_key(4.0, 0.0)
    assert explorer._region_strikes[key] == explorer._strike_limit
    on_disk = json.loads((tmp_path / "keepout.json").read_text())
    assert on_disk[0]["odom_epoch"] == "same-boot"
    assert on_disk[0]["world_x"] == pytest.approx(4.5)


def test_arrival_with_resolution_does_not_strike(tmp_path) -> None:
    explorer = _keep_out_explorer(tmp_path, transform=None)
    explorer._invalidate_radius_m = 0.9
    explorer.goal_reached_event = Event()
    # Fully-known neighborhood: the trip resolved its frontier.
    explorer.latest_costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8), resolution=0.05
    )
    explorer._active_goal = Vector3(4.0, 0.0, 0.0)
    explorer._on_goal_reached(SimpleNamespace(data=True))
    assert explorer._region_strikes == {}
    assert explorer.goal_reached_event.is_set()


def test_pre_lock_keep_out_applies_immediately_in_world_frame(tmp_path) -> None:
    explorer = _keep_out_explorer(tmp_path, transform=None)
    explorer.latest_odometry = SimpleNamespace(position=SimpleNamespace(x=4.0, y=0.0))
    message = explorer.mark_keep_out_here(radius_m=2.0)
    assert "active NOW" in message
    costmap = OccupancyGrid(
        grid=np.full((400, 400), -1, dtype=np.int8), resolution=0.05
    )
    ranked = explorer._rank_frontiers(
        [Vector3(4.0, 0.0, 0.0), Vector3(0.0, 8.0, 0.0)],
        [40, 40],
        Vector3(0.0, 0.0, 0.0),
        costmap,
    )
    assert [(round(f.x, 1), round(f.y, 1)) for f in ranked] == [(0.0, 8.0)]


def test_pending_keep_out_anchors_permanently_on_lock(tmp_path) -> None:
    import json as _json

    explorer = _keep_out_explorer(tmp_path, transform=None)
    explorer.latest_odometry = SimpleNamespace(position=SimpleNamespace(x=4.0, y=0.0))
    explorer.mark_keep_out_here(radius_m=2.0)
    # Relocalization locks with the identity transform.
    explorer._world_to_map_cache = (0.0, 0.0, 0.0)
    circles = explorer._keep_out_world_circles()
    assert len(circles) == 1
    assert explorer._pending_world_keep_outs == []
    on_disk = _json.loads((tmp_path / "keepout.json").read_text())
    assert abs(on_disk[0]["x"] - 4.0) < 1e-6


# --- July 24 glass-window / false-home root-cause regressions -----------------


def test_frontier_explorer_never_targets_boot_region_after_egress() -> None:
    explorer = _ranking_explorer()
    explorer._session_origin = (0.0, 0.0)
    explorer._origin_exited = True
    costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8),
        resolution=0.05,
    )

    ranked = explorer._rank_frontiers(
        [Vector3(2.5, 0.0, 0.0), Vector3(8.0, 0.0, 0.0)],
        [40, 40],
        Vector3(5.0, 0.0, 0.0),
        costmap,
    )

    assert [(round(goal.x, 1), round(goal.y, 1)) for goal in ranked] == [(8.0, 0.0)]


def test_frontier_origin_guard_allows_outward_progress_inside_guard() -> None:
    explorer = _ranking_explorer()
    explorer._session_origin = (0.0, 0.0)
    explorer._origin_exited = True
    costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8),
        resolution=0.05,
    )

    ranked = explorer._rank_frontiers(
        [Vector3(1.0, 0.0, 0.0), Vector3(3.0, 0.0, 0.0)],
        [40, 40],
        Vector3(2.1, 0.0, 0.0),
        costmap,
    )

    assert [(round(goal.x, 1), round(goal.y, 1)) for goal in ranked] == [(3.0, 0.0)]


def test_frontier_origin_guard_allows_initial_egress() -> None:
    explorer = _ranking_explorer()
    explorer._session_origin = (0.0, 0.0)
    explorer._origin_exited = False
    costmap = OccupancyGrid(
        grid=np.zeros((400, 400), dtype=np.int8),
        resolution=0.05,
    )

    ranked = explorer._rank_frontiers(
        [Vector3(2.5, 0.0, 0.0)],
        [40],
        Vector3(0.0, 0.0, 0.0),
        costmap,
    )

    assert [(round(goal.x, 1), round(goal.y, 1)) for goal in ranked] == [(2.5, 0.0)]


def test_patrol_spreads_from_boot_origin_not_back_toward_it() -> None:
    router = object.__new__(NightwatchCoveragePatrolRouter)
    router._session_origin = (0.0, 0.0)
    router._recent_goals = []

    selected = router._select_spread_goal(
        [
            (500, (1.0, 0.0)),
            (5, (9.0, 0.0)),
        ],
        start=(8.0, 0.0),
    )

    assert selected == (9.0, 0.0)


def test_mapped_handoff_starts_patrol_without_mcp_registry() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._patrolling = False
    curiosity._retry_not_before = 0.0
    curiosity._patrol_requested_at = 0.0
    curiosity._last_odom = PoseStamped(position=Vector3())
    curiosity._lock = RLock()
    calls: list[str] = []
    curiosity._connection = SimpleNamespace(
        sport_command=lambda _api_id: True,
    )
    curiosity._patrol = SimpleNamespace(
        is_patrolling=lambda: False,
        ensure_patrolling=lambda: calls.append("direct") or True,
    )
    curiosity._agent_spec = SimpleNamespace(
        dispatch_continuation=lambda *_args: calls.append("mcp") or True
    )

    curiosity._ensure_patrolling(10.0)

    assert curiosity._patrolling is True
    assert calls == ["direct"]


def test_failed_patrol_handoff_reopens_exploration_without_turning() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig()
    curiosity._map_phase = "MAPPED"
    curiosity._patrolling = False
    curiosity._exploring = False
    curiosity._retry_not_before = 0.0
    curiosity._patrol_requested_at = 0.0
    curiosity._last_odom = PoseStamped(position=Vector3())
    curiosity._lock = RLock()
    curiosity._connection = SimpleNamespace(
        sport_command=lambda _api_id: True,
    )
    curiosity._patrol = SimpleNamespace(
        is_patrolling=lambda: False,
        ensure_patrolling=lambda: False,
    )
    recoveries: list[str] = []
    curiosity._recover_from_stall = lambda: recoveries.append("turn")

    curiosity._ensure_patrolling(10.0)
    curiosity._enforce_liveness(11.0, NavigationState.IDLE)

    assert curiosity._map_phase == "EXPLORING"
    assert curiosity._retry_not_before == 0.0
    assert recoveries == []


def test_patrol_goal_wait_is_bounded() -> None:
    async def scenario() -> None:
        patrol = object.__new__(PatrollingModule)
        calls = 0

        def next_goal():
            nonlocal calls
            calls += 1
            if calls == 1:
                return PoseStamped(position=Vector3(5.0, 0.0, 0.0))
            raise asyncio.CancelledError

        published: list[PoseStamped] = []
        patrol._router = SimpleNamespace(next_goal=next_goal)
        patrol._goal_reached_event = asyncio.Event()
        patrol.goal_request = SimpleNamespace(
            publish=lambda goal: published.append(goal)
        )
        patrol._no_goal_since = None
        patrol._goals_published = 0
        patrol._patrol_goal_timeout_s = 0.01
        patrol._wander_dwell_range_s = (0.0, 0.0)

        try:
            await patrol._patrol_loop()
        except asyncio.CancelledError:
            pass

        assert len(published) == 1
        assert calls == 2

    asyncio.run(scenario())


def test_rejected_patrol_leg_immediately_releases_goal_wait() -> None:
    async def scenario() -> None:
        patrol = object.__new__(PatrollingModule)
        patrol._goal_reached_event = asyncio.Event()

        await patrol.handle_goal_reached(SimpleNamespace(data=False))

        assert patrol._goal_reached_event.is_set()

    asyncio.run(scenario())


def test_patrol_keep_out_allows_exit_but_never_reentry() -> None:
    circle = [(0.0, 0.0, 1.0)]

    def path(*points: tuple[float, float]) -> SimpleNamespace:
        return SimpleNamespace(
            poses=[PoseStamped(position=Vector3(x, y, 0.0)) for x, y in points]
        )

    assert (
        NightwatchCoveragePatrolRouter._path_reenters_exclusion(
            path((0.0, 0.0), (0.5, 0.0), (1.5, 0.0)),
            circle,
        )
        is False
    )
    assert (
        NightwatchCoveragePatrolRouter._path_reenters_exclusion(
            path((0.0, 0.0), (1.5, 0.0), (0.5, 0.0)),
            circle,
        )
        is True
    )
    assert (
        NightwatchCoveragePatrolRouter._path_reenters_exclusion(
            path((2.0, 0.0), (0.5, 0.0)),
            circle,
        )
        is True
    )


def test_stopping_patrol_cancels_without_publishing_current_pose() -> None:
    async def scenario() -> None:
        patrol = object.__new__(PatrollingModule)
        patrol._patrol_task = None
        calls: list[str] = []
        patrol._planner_spec = SimpleNamespace(
            set_replanning_enabled=lambda enabled: calls.append(
                f"replanning:{enabled}"
            ),
            reset_safe_goal_clearance=lambda: calls.append("clearance"),
            cancel_goal=lambda: calls.append("cancel"),
        )
        patrol.stop_tool = lambda name: calls.append(f"stop_tool:{name}")
        published: list[PoseStamped] = []
        patrol.goal_request = SimpleNamespace(publish=published.append)
        patrol._latest_pose = PoseStamped(position=Vector3(1.0, 2.0, 0.0))

        await patrol._stop_patrolling()

        assert published == []
        assert calls == [
            "replanning:True",
            "clearance",
            "stop_tool:start_patrol",
            "cancel",
        ]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Sleeping-area marking methods: map-drawn zones, AprilTag markers, and
# operator-tag protection. All three must produce areas the escort can find.
# ---------------------------------------------------------------------------


def _write_zone_file(path, zones) -> None:
    path.write_text(json.dumps({"version": 1, "zones": zones}))


def test_sleeping_zone_polygon_becomes_navigable_area_and_retracts(
    tmp_path, monkeypatch
) -> None:
    zone_file = tmp_path / "zones.json"
    _write_zone_file(
        zone_file,
        [
            {
                "id": "zone-a",
                "kind": "sleeping",
                "active": True,
                "points_frame": "world",
                "points": [[4.0, 4.0], [6.0, 4.0], [6.0, 6.0], [4.0, 6.0]],
                "world_points": [[4.0, 4.0], [6.0, 4.0], [6.0, 6.0], [4.0, 6.0]],
            },
            {
                "id": "zone-b",
                "kind": "keep_out",
                "active": True,
                "points_frame": "world",
                "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]],
                "world_points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]],
            },
        ],
    )
    monkeypatch.setattr("nightwatch.world_model._OPERATOR_ZONE_PATH", str(zone_file))
    model = _world_model_with_memory_db()
    model._world_to_map = lambda: None

    model._sync_sleeping_zones()

    rows = model._db.execute(
        "SELECT area_id,center_x,center_y,area_type,confidence,auto_tag,protected"
        " FROM areas"
    ).fetchall()
    assert len(rows) == 1  # the keep_out constraint never becomes an area
    area_id, cx, cy, area_type, confidence, auto_tag, protected = rows[0]
    assert (cx, cy) == pytest.approx((5.0, 5.0))
    assert area_type == "sleeping_area"
    assert confidence == 1.0
    assert auto_tag == "map_zone:zone-a"
    assert protected == 1
    # The escort finds it exactly like a self-tagged area.
    found = model.nearest_area("sleeping_area", 0.0, 0.0)
    assert found is not None and found["area_id"] == str(area_id)

    # Re-saving the polygon moves the same row instead of duplicating it.
    _write_zone_file(
        zone_file,
        [
            {
                "id": "zone-a",
                "kind": "sleeping",
                "active": True,
                "points_frame": "world",
                "points": [[8.0, 8.0], [10.0, 8.0], [10.0, 10.0], [8.0, 10.0]],
                "world_points": [[8.0, 8.0], [10.0, 8.0], [10.0, 10.0], [8.0, 10.0]],
            }
        ],
    )
    os.utime(zone_file, ns=(1, 1))  # force a distinct mtime on fast writes
    model._sync_sleeping_zones()
    rows = model._db.execute("SELECT area_id,center_x,center_y FROM areas").fetchall()
    assert len(rows) == 1
    assert (rows[0][1], rows[0][2]) == pytest.approx((9.0, 9.0))

    # Deactivating (or deleting) the polygon retracts the escort destination.
    _write_zone_file(
        zone_file,
        [
            {
                "id": "zone-a",
                "kind": "sleeping",
                "active": False,
                "points_frame": "world",
                "points": [[8.0, 8.0], [10.0, 8.0], [10.0, 10.0], [8.0, 10.0]],
                "world_points": [[8.0, 8.0], [10.0, 8.0], [10.0, 10.0], [8.0, 10.0]],
            }
        ],
    )
    os.utime(zone_file, ns=(2, 2))
    model._sync_sleeping_zones()
    assert model._db.execute("SELECT COUNT(*) FROM areas").fetchone()[0] == 0
    assert model.nearest_area("sleeping_area", 0.0, 0.0) is None


def test_sleeping_zone_map_frame_projects_through_live_transform(
    tmp_path, monkeypatch
) -> None:
    zone_file = tmp_path / "zones.json"
    # Map square centered at (10, 0); transform map = R(90 deg) @ world + (0, 0),
    # so the world centroid must be R^T @ (10, 0) = (0, -10).
    _write_zone_file(
        zone_file,
        [
            {
                "id": "zone-m",
                "kind": "sleeping",
                "active": True,
                "points_frame": "map",
                "points": [[9.0, -1.0], [11.0, -1.0], [11.0, 1.0], [9.0, 1.0]],
                "world_points": [[99.0, 99.0], [99.5, 99.0], [99.5, 99.5]],
            }
        ],
    )
    monkeypatch.setattr("nightwatch.world_model._OPERATOR_ZONE_PATH", str(zone_file))
    model = _world_model_with_memory_db()
    model._world_to_map = lambda: (0.0, 0.0, math.pi / 2.0)

    model._sync_sleeping_zones()

    row = model._db.execute("SELECT center_x,center_y FROM areas").fetchone()
    # The live transform wins over the stale stored world snapshot.
    assert (row[0], row[1]) == pytest.approx((0.0, -10.0), abs=1e-6)


def test_operator_tagged_area_survives_hostile_votes() -> None:
    model = _world_model_with_memory_db()
    model._latest_odom = PoseStamped(position=Vector3(0.0, 0.0, 0.0))

    message = model.tag_area_here("nap zone")
    assert message == "Tagged nap zone."
    row = model._db.execute(
        "SELECT area_id,area_type,protected FROM areas"
    ).fetchone()
    area_id, area_type, protected = str(row[0]), str(row[1]), int(row[2])
    # Synonym parsing: "nap zone" is a sleeping area, not area_type=unknown.
    assert area_type == "sleeping_area"
    assert protected == 1

    # Four workspace hint scans used to flip a manual sleeping tag back to
    # workspace via _resolve_area's correction branch. Protected rows hold.
    model._vote_area(area_id, "workspace", 16)
    model._resolve_area(area_id)
    assert model._area_row(area_id)["area_type"] == "sleeping_area"


def test_marker_detection_creates_navigable_sleeping_area() -> None:
    model = _world_model_with_memory_db()
    model.config = SimpleNamespace(
        area_radius_m=2.75,
        area_merge_radius_m=None,
        object_min_evidence=3,
        marker_names={0: "checkpoint_0", 1: "rest_area", 2: "sleeping_area"},
        marker_min_evidence=2,
    )
    model._marker_counts = Counter()
    model._latest_odom = PoseStamped(position=Vector3(0.0, 0.0, 0.0))

    detection = SimpleNamespace(
        id="2",
        bbox=SimpleNamespace(
            center=SimpleNamespace(position=SimpleNamespace(x=3.0, y=0.0, z=0.5))
        ),
    )
    message = SimpleNamespace(detections=[detection], detections_length=1)
    # Two sightings reach marker_min_evidence exactly once.
    model._on_marker_detections(message)
    model._on_marker_detections(message)

    found = model.nearest_area("sleeping_area", 0.0, 0.0)
    assert found is not None
    # Stored at the collision-safe standoff pose (0.8 m short of the tag),
    # not at the marker on the wall/tent itself.
    assert (found["x"], found["y"]) == pytest.approx((2.2, 0.0), abs=1e-6)
    row = model._db.execute(
        "SELECT area_type,protected,auto_tag FROM areas"
    ).fetchone()
    assert row[0] == "sleeping_area"
    assert int(row[1]) == 1
    assert str(row[2]) == "marker:2:sleeping_area"


def test_world_status_lists_sleeping_area_coordinates() -> None:
    # The booth map needs positions, not only a count: current-session rows
    # use live world centers; anchored prior-session rows re-project through
    # the current transform; unanchored stale rows are flagged tentative.
    model = _world_model_with_memory_db()
    model._explore = SimpleNamespace(coverage_status=lambda: {})
    model._experience_images = None
    model._detector = None
    model._detector_error = None
    model._area_vlm_error = None
    model.config = SimpleNamespace(
        area_radius_m=2.75,
        area_merge_radius_m=None,
        object_min_evidence=3,
        area_vlm_enabled=False,
    )
    now = 1000.0
    db = model._db
    db.execute(
        """
        INSERT INTO areas
        (area_id,center_x,center_y,samples,area_type,confidence,auto_tag,
         first_seen,last_seen,map_x,map_y,protected)
        VALUES('a_session',1.0,2.0,1,'sleeping_area',1.0,'sleeping_area',?,?,
               NULL,NULL,1)
        """,
        (now, now),
    )
    model._session_area_ids.add("a_session")
    # Prior-session row anchored at map (10, 0); identity-rotation transform
    # with translation (4, 0) puts its current world position at (6, 0).
    db.execute(
        """
        INSERT INTO areas
        (area_id,center_x,center_y,samples,area_type,confidence,auto_tag,
         first_seen,last_seen,map_x,map_y,protected)
        VALUES('a_prior',99.0,99.0,1,'sleeping_area',0.8,'sleeping_area_2',?,?,
               10.0,0.0,0)
        """,
        (now, now),
    )
    db.execute(
        """
        INSERT INTO areas
        (area_id,center_x,center_y,samples,area_type,confidence,auto_tag,
         first_seen,last_seen,map_x,map_y,protected)
        VALUES('a_stale',5.0,5.0,1,'sleeping_area',0.6,'sleeping_area_3',?,?,
               NULL,NULL,0)
        """,
        (now, now),
    )
    db.commit()
    model._world_to_map = lambda: (4.0, 0.0, 0.0)

    status = model.world_status()

    assert status["sleeping_areas"] == 3
    by_id = {item["area_id"]: item for item in status["sleeping_area_list"]}
    assert by_id["a_session"]["x"] == pytest.approx(1.0)
    assert by_id["a_session"]["y"] == pytest.approx(2.0)
    assert by_id["a_session"]["protected"] is True
    assert by_id["a_prior"]["x"] == pytest.approx(6.0)
    assert by_id["a_prior"]["anchored"] is True
    assert by_id["a_stale"]["x"] == pytest.approx(5.0)
    assert by_id["a_stale"]["anchored"] is False


# ---------------------------------------------------------------------------
# Zone frame validation across sessions: WORLD snapshots are epoch-scoped and
# must be refused (not reinterpreted) once the odometry frame has moved.
# ---------------------------------------------------------------------------


def test_stale_world_zone_is_refused_not_reinterpreted(tmp_path) -> None:
    """A WORLD drawing from a previous session must never be anchored or
    enforced under a new session's transform: that translated polygons by the
    inter-session odometry delta (zones "moving" after robot restarts)."""
    path = tmp_path / "zones.json"
    drawing = [[1.0, -1.0], [3.0, -1.0], [3.0, 1.0], [1.0, 1.0]]
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "zones": [
                    {
                        "id": "focus",
                        "kind": "keep_in",
                        "points_frame": "world",
                        "points": drawing,
                        "world_points": drawing,
                        "world_epoch": "old-session",
                    }
                ],
            }
        )
    )
    before = path.read_text()

    # Enforcement refuses the stale snapshot in the new session...
    assert _load_operator_zones(str(path), None, "new-session") == []
    # ...and anchoring leaves the drawing untouched instead of minting MAP
    # coordinates from the wrong frame.
    assert _anchor_world_operator_zones(str(path), (10.0, 4.0, 0.5), "new-session") == 0
    assert path.read_text() == before

    # The same snapshot remains fully usable in the session that drew it.
    assert len(_load_operator_zones(str(path), None, "old-session")) == 1
    assert (
        _anchor_world_operator_zones(str(path), (10.0, 4.0, 0.5), "old-session") == 1
    )
    persisted = json.loads(path.read_text())["zones"][0]
    assert persisted["points_frame"] == "map"
    assert persisted["world_epoch"] == "old-session"
    assert persisted["drawn_world_points"] == drawing
    assert persisted["points"][0] == pytest.approx(
        _world_to_map_point(1.0, -1.0, (10.0, 4.0, 0.5))
    )


def test_anchor_reanchors_from_original_drawing_after_transform_correction(
    tmp_path,
) -> None:
    """An in-session alignment correction (restored transform overturned by
    live verification, operator initial pose) must re-derive MAP coordinates
    from the original drawing instead of keeping wrong-transform points."""
    path = tmp_path / "zones.json"
    drawing = [[1.0, -1.0], [3.0, -1.0], [3.0, 1.0], [1.0, 1.0]]
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "zones": [
                    {
                        "id": "focus",
                        "kind": "keep_in",
                        "points_frame": "world",
                        "points": drawing,
                        "world_points": drawing,
                        "world_epoch": "epoch-1",
                    }
                ],
            }
        )
    )
    wrong = (10.0, 4.0, 0.5)
    corrected = (2.0, -1.0, 0.0)

    assert _anchor_world_operator_zones(str(path), wrong, "epoch-1") == 1
    assert _anchor_world_operator_zones(str(path), corrected, "epoch-1") == 1
    persisted = json.loads(path.read_text())["zones"][0]
    assert persisted["points"][0] == pytest.approx(
        _world_to_map_point(1.0, -1.0, corrected)
    )
    # The refreshed WORLD snapshot equals the original drawing again.
    assert np.allclose(persisted["world_points"], drawing)
    assert persisted["anchor_transform"] == pytest.approx(list(corrected))

    # A NEW session may not replay the old drawing under its own transform.
    unchanged = json.loads(path.read_text())["zones"][0]["points"]
    _anchor_world_operator_zones(str(path), (7.0, 7.0, 1.0), "epoch-2")
    assert json.loads(path.read_text())["zones"][0]["points"] == unchanged


def test_resolve_odom_epoch_requires_heading_continuity(tmp_path) -> None:
    """A power-cycled Go2 restarts odometry at ~(0, 0, yaw 0). A dog that died
    near its boot spot used to falsely 'continue' the old epoch, restoring a
    transform for a WORLD frame that had physically moved."""
    path = str(tmp_path / "odom_epoch.json")
    first = _resolve_odom_epoch((0.5, 0.2), yaw=2.5, path=path, now=100.0)
    # Small in-place drift: same session.
    assert _resolve_odom_epoch((0.3, 0.1), yaw=2.3, path=path, now=110.0) == first
    # Position is again within 4 m of the origin, but the heading snapped to
    # zero: the reboot signature, so the epoch must rotate.
    assert _resolve_odom_epoch((0.0, 0.0), yaw=0.0, path=path, now=120.0) != first


def test_resolve_odom_epoch_without_saved_heading_stays_positional(tmp_path) -> None:
    path = str(tmp_path / "odom_epoch.json")
    legacy = _resolve_odom_epoch((0.2, 0.0), path=path, now=100.0)
    assert _resolve_odom_epoch((0.0, 0.0), yaw=1.5, path=path, now=110.0) == legacy


def test_restore_refused_when_heading_flipped(tmp_path) -> None:
    reloc = _persisted_relocalization_fixture(tmp_path)
    epoch = json.loads(reloc._odom_epoch_path.read_text())
    epoch["last_yaw"] = 2.5
    reloc._odom_epoch_path.write_text(json.dumps(epoch))

    assert not reloc._try_restore_accepted_alignment(
        (4.1, -2.1), yaw=0.0, now=1_000.0
    )
    assert reloc._alignment_locked is False

    assert reloc._try_restore_accepted_alignment((4.1, -2.1), yaw=2.2, now=1_000.0)
    assert reloc._alignment_locked is True


def test_restored_alignment_is_corrected_by_live_planar_match(
    tmp_path, monkeypatch
) -> None:
    """A restored alignment is a hypothesis: once the live map is informative
    the planar matcher must confirm it, and a clear disagreement (undetected
    odometry reset) must move navigation to the fresh transform."""
    from nightwatch.memory import _transform_from_2d

    reloc = _persisted_relocalization_fixture(tmp_path)
    reloc._premap = SimpleNamespace(pointcloud=object())
    assert reloc._try_restore_accepted_alignment((4.1, -2.1), now=1_000.0)
    assert reloc._restore_verification_pending is True

    fresh = _transform_from_2d(12.5, 5.0, math.pi / 2.0)
    monkeypatch.setattr(
        "nightwatch.memory.relocalize_planar",
        lambda _premap, _scan: SimpleNamespace(transform=fresh),
    )
    corrected_tf = reloc._try_relocalize(_FakeScan())

    assert corrected_tf is not None
    assert reloc._restore_verification_pending is False
    assert reloc.world_to_map_2d() == pytest.approx(
        {"x": 12.5, "y": 5.0, "yaw": math.pi / 2.0}
    )


def test_restored_alignment_verification_agreement_keeps_transform(
    tmp_path, monkeypatch
) -> None:
    from nightwatch.memory import _transform_from_2d

    reloc = _persisted_relocalization_fixture(tmp_path)
    reloc._premap = SimpleNamespace(pointcloud=object())
    assert reloc._try_restore_accepted_alignment((4.1, -2.1), now=1_000.0)

    close = _transform_from_2d(10.2, 5.1, math.pi / 2.0 + 0.05)
    monkeypatch.setattr(
        "nightwatch.memory.relocalize_planar",
        lambda _premap, _scan: SimpleNamespace(transform=close),
    )
    assert reloc._try_relocalize(_FakeScan()) is None
    assert reloc._restore_verification_pending is False
    # The map must not shift under active navigation for a sub-tolerance
    # refinement: the restored transform stays authoritative.
    assert reloc.world_to_map_2d() == pytest.approx(
        {"x": 10.0, "y": 5.0, "yaw": math.pi / 2.0}
    )


def test_restore_verification_attempts_stay_eligible_while_locked(
    tmp_path,
) -> None:
    reloc = _persisted_relocalization_fixture(tmp_path)
    reloc.config = SimpleNamespace(retry_interval_s=30.0, max_failed_attempts=5)
    reloc._last_attempt_at = 0.0
    assert reloc._try_restore_accepted_alignment((4.1, -2.1), now=1_000.0)

    # Locked, but a pending verification keeps the matcher eligible at the
    # normal cadence.
    assert reloc._should_attempt(_FakeScan()) is True
    assert reloc._should_attempt(_FakeScan()) is False  # cadence gate

    reloc._restore_verification_pending = False
    reloc._last_attempt_at = 0.0
    assert reloc._should_attempt(_FakeScan()) is False  # stock locked gate


def test_sleeping_zone_area_moves_when_alignment_is_corrected(
    tmp_path, monkeypatch
) -> None:
    """The sleeping-area sync must re-project when the transform VALUE
    changes, not only when it appears: an alignment correction used to leave
    every zone-derived area at the wrong pre-correction position."""
    zone_file = tmp_path / "zones.json"
    _write_zone_file(
        zone_file,
        [
            {
                "id": "zone-m",
                "kind": "sleeping",
                "active": True,
                "points_frame": "map",
                "points": [[9.0, -1.0], [11.0, -1.0], [11.0, 1.0], [9.0, 1.0]],
                "world_points": [[9.0, -1.0], [11.0, -1.0], [11.0, 1.0], [9.0, 1.0]],
            }
        ],
    )
    monkeypatch.setattr("nightwatch.world_model._OPERATOR_ZONE_PATH", str(zone_file))
    model = _world_model_with_memory_db()

    model._world_to_map = lambda: (0.0, 0.0, math.pi / 2.0)
    model._sync_sleeping_zones()
    row = model._db.execute("SELECT center_x,center_y FROM areas").fetchone()
    assert (row[0], row[1]) == pytest.approx((0.0, -10.0), abs=1e-6)

    # Same file mtime, corrected transform: the area must follow.
    model._world_to_map = lambda: (0.0, 0.0, 0.0)
    model._sync_sleeping_zones()
    row = model._db.execute("SELECT center_x,center_y FROM areas").fetchone()
    assert (row[0], row[1]) == pytest.approx((10.0, 0.0), abs=1e-6)


def test_stale_world_sleeping_zone_is_not_registered(tmp_path, monkeypatch) -> None:
    """A sleeping polygon whose WORLD snapshot belongs to another session must
    not produce an escort destination at the wrong physical spot."""
    zone_file = tmp_path / "zones.json"
    epoch_file = tmp_path / "odom_epoch.json"
    epoch_file.write_text(
        json.dumps(
            {
                "epoch_id": "new-session",
                "updated_at": time.time(),
                "last_pose": [0.0, 0.0],
            }
        )
    )
    monkeypatch.setattr("nightwatch.world_model._OPERATOR_ZONE_PATH", str(zone_file))
    monkeypatch.setattr("nightwatch.world_model._ODOM_EPOCH_PATH", str(epoch_file))

    def zone(epoch: str) -> dict:
        return {
            "id": "zone-w",
            "kind": "sleeping",
            "active": True,
            "points_frame": "world",
            "points": [[4.0, 4.0], [6.0, 4.0], [6.0, 6.0], [4.0, 6.0]],
            "world_points": [[4.0, 4.0], [6.0, 4.0], [6.0, 6.0], [4.0, 6.0]],
            "world_epoch": epoch,
        }

    _write_zone_file(zone_file, [zone("old-session")])
    model = _world_model_with_memory_db()
    model._world_to_map = lambda: None

    model._sync_sleeping_zones()
    assert model._db.execute("SELECT COUNT(*) FROM areas").fetchone()[0] == 0

    # The same polygon drawn in THIS session's frame registers normally.
    _write_zone_file(zone_file, [zone("new-session")])
    os.utime(zone_file, ns=(1, 1))
    model._sync_sleeping_zones()
    row = model._db.execute("SELECT center_x,center_y FROM areas").fetchone()
    assert (row[0], row[1]) == pytest.approx((5.0, 5.0))


def test_map_streamer_keeps_refreshing_transform_after_lock(monkeypatch) -> None:
    """The streamer used to query the world->map transform only while it had
    none, so a mid-session alignment correction left every browser converting
    zones with the stale value forever."""
    streamer = object.__new__(MapStreamer)
    streamer.config = SimpleNamespace(max_points=150_000)
    streamer._clients = set()
    streamer._premap_seq = 0
    streamer._reloc_transform = {"x": 10.0, "y": 20.0, "yaw": 0.0}
    scheduled: list[bool] = []
    streamer._schedule_relocalization_query = lambda: scheduled.append(True)
    monkeypatch.setattr(
        "nightwatch.map_stream.ws_server.broadcast", lambda *_args: None
    )
    premap = PointCloud2.from_numpy(
        np.array([[10.0, 20.0, 0.25]], dtype=np.float32),
        frame_id="map",
        timestamp=123.0,
    )

    asyncio.run(streamer.handle_loaded_map(premap))

    assert scheduled == [True]


def test_map_streamer_rebroadcasts_premap_only_on_transform_change(
    monkeypatch,
) -> None:
    streamer = object.__new__(MapStreamer)
    streamer._reloc_transform = {"x": 1.0, "y": 2.0, "yaw": 0.5}
    streamer._latest_loaded_map = object()
    streamer._loop = SimpleNamespace(is_closed=lambda: False)
    streamer._reloc_query_lock = Lock()
    rebroadcasts: list[bool] = []

    def _capture(coro, _loop):
        coro.close()
        rebroadcasts.append(True)

    monkeypatch.setattr(
        "nightwatch.map_stream.asyncio.run_coroutine_threadsafe", _capture
    )
    streamer.handle_loaded_map = lambda _msg: _empty_coroutine()

    # Unchanged transform: refresh quietly, no premap rebroadcast.
    streamer._reloc = SimpleNamespace(
        world_to_map_2d=lambda: {"x": 1.0, "y": 2.0, "yaw": 0.5}
    )
    streamer._reloc_query_inflight = True
    streamer._query_relocalization_transform()
    assert rebroadcasts == []
    assert streamer._reloc_query_inflight is False

    # Corrected transform: adopt it and reproject the premap once.
    streamer._reloc = SimpleNamespace(
        world_to_map_2d=lambda: {"x": 4.0, "y": -1.0, "yaw": 0.1}
    )
    streamer._reloc_query_inflight = True
    streamer._query_relocalization_transform()
    assert rebroadcasts == [True]
    assert streamer._reloc_transform == {"x": 4.0, "y": -1.0, "yaw": 0.1}


async def _empty_coroutine() -> None:
    return None


def test_patrol_failure_exclusions_expire_after_ttl(monkeypatch) -> None:
    # A transient transport stall (stale lidar, degraded WebRTC) writes
    # exclusions around the robot; permanent ones poisoned the goal pool for
    # the rest of the run ("No patrol goal available" forever once the
    # transport recovered, observed live 2026-07-25). They must heal.
    router = object.__new__(NightwatchCoveragePatrolRouter)
    router._lock = RLock()
    router._failure_exclusion_radius_m = 2.0
    router._session_failure_zones = []
    clock = [1000.0]
    monkeypatch.setattr(
        "nightwatch.navigation.time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time, sleep=time.sleep),
    )

    router.report_failure_zone(1.0, 2.0)
    assert router._active_failure_zones() == [(1.0, 2.0, 2.0)]

    # Still active within the TTL; a duplicate report is deduplicated.
    clock[0] += 60.0
    router.report_failure_zone(1.2, 2.1)
    assert len(router._active_failure_zones()) == 1

    # Expired: the pool heals and the same spot may be excluded afresh later.
    clock[0] += 200.0
    assert router._active_failure_zones() == []
    router.report_failure_zone(1.0, 2.0)
    assert len(router._active_failure_zones()) == 1


def test_pose_mode_latch_is_tracked_and_retried_until_acknowledged() -> None:
    # Pose mode makes the Go2 ignore gait/velocity commands while still moving
    # joints and torso (the live "stuck but twitching" freeze). A restore that
    # is merely SENT is not enough: a dropped/rejected exit must keep the latch
    # flagged so later motion paths retry it.
    nightwatch_unitree._pose_mode_on[0] = False
    requests: list[dict] = []
    ok_connection = SimpleNamespace(
        publish_request=lambda _topic, request: requests.append(request.copy())
        or {"status": "ok"}
    )

    # Simulate firmware state left by the older hip-lowering implementation.
    nightwatch_unitree._pose_mode_on[0] = True
    assert nightwatch_unitree.pose_mode_latched() is True

    # A rejected exit leaves the latch set (so it will be retried).
    rejecting = SimpleNamespace(
        publish_request=lambda _topic, _request: {"status": "error", "code": 3}
    )
    assert nightwatch_unitree.clear_pose_mode(rejecting) is False
    assert nightwatch_unitree.pose_mode_latched() is True

    # An acknowledged exit clears it, and a second call is a cheap no-op.
    requests.clear()
    assert nightwatch_unitree.clear_pose_mode(ok_connection) is True
    assert nightwatch_unitree.pose_mode_latched() is False
    assert [request["api_id"] for request in requests] == [
        SPORT_CMD["StopMove"],
        SPORT_CMD["BalanceStand"],
    ]
    requests.clear()
    assert nightwatch_unitree.clear_pose_mode(ok_connection) is True
    assert requests == []


def test_motion_rearm_fast_path_still_breaks_a_pose_latch(monkeypatch) -> None:
    # The 5-minute TTL fast path used to return True without touching the
    # firmware, so a re-arm right after a scan left the dog unable to walk.
    nightwatch_unitree._pose_mode_on[0] = True
    requests: list[dict] = []
    connection = SimpleNamespace(
        publish_request=lambda _topic, request: requests.append(request.copy())
        or {"status": "ok"}
    )
    monkeypatch.setattr(nightwatch_unitree.time, "monotonic", lambda: 1000.0)
    nightwatch_unitree._last_ready[0] = 999.0  # well inside the TTL

    assert nightwatch_unitree.ensure_motion_ready(connection) is True

    assert SPORT_CMD["StopMove"] in [
        request["api_id"] for request in requests
    ]
    assert nightwatch_unitree.pose_mode_latched() is False


def test_supervisor_clears_pose_mode_that_outlived_its_scan() -> None:
    # The watchdog is the last line of defence: whatever loses the restore
    # (WebRTC drop mid-scan, unacked command, interrupted window), the next
    # ticks clear the latch so the robot can walk again without a power cycle.
    nightwatch_unitree._pose_mode_on[0] = True
    cleared: list[bool] = []
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._connection = SimpleNamespace()
    curiosity._scan_kind = None
    curiosity._face_observation_until = 0.0
    curiosity._pose_clear_not_before = 0.0

    import nightwatch.curiosity as curiosity_module

    original = curiosity_module.clear_pose_mode
    curiosity_module.clear_pose_mode = lambda _c: cleared.append(True) or True
    try:
        curiosity._enforce_pose_mode_invariant(100.0)
        # Rate-limited: a second immediate tick does not spam the firmware.
        curiosity._enforce_pose_mode_invariant(100.2)
        assert cleared == [True]

        # A scan never owns pose mode now; even during a scan, a legacy latch
        # is recovered immediately.
        cleared.clear()
        curiosity._scan_kind = "scheduled"
        curiosity._pose_clear_not_before = 0.0
        curiosity._enforce_pose_mode_invariant(200.0)
        assert cleared == [True]
    finally:
        curiosity_module.clear_pose_mode = original
        nightwatch_unitree._pose_mode_on[0] = False


def test_gesture_cleanup_runs_even_under_manual_override() -> None:
    # A sport gesture hands the body to the firmware; only the force re-arm
    # after it restores a locomotion-ready stance. That cleanup used to live
    # only in the autonomy path, so waving and then taking Manual Override
    # left the dog in the gesture stance ignoring WASD entirely.
    from nightwatch.contracts import OperatingMode

    rearms: list[bool] = []
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._connection = SimpleNamespace()
    curiosity._dog_expression_name = "Hello"
    curiosity._dog_expression_deadline = 500.0  # still "running"
    curiosity._retry_not_before = 0.0
    curiosity._motion_watch_started_at = 0.0
    curiosity._last_motion_ts = None

    import nightwatch.curiosity as curiosity_module

    original = curiosity_module.ensure_motion_ready
    curiosity_module.ensure_motion_ready = (
        lambda _c, force=False: rearms.append(force) or True
    )
    try:
        # Manual override mid-gesture: aborted immediately and stance re-armed.
        curiosity._enforce_expression_watchdog(
            100.0, SimpleNamespace(mode=OperatingMode.MANUAL)
        )
        assert curiosity._dog_expression_name is None
        assert rearms == [True]

        # An expired gesture is also cleaned up outside autonomy.
        rearms.clear()
        curiosity._dog_expression_name = "WiggleHips"
        curiosity._dog_expression_deadline = 50.0
        curiosity._enforce_expression_watchdog(
            100.0, SimpleNamespace(mode=OperatingMode.AUTONOMOUS)
        )
        assert curiosity._dog_expression_name is None
        assert rearms == [True]

        # A gesture still within its window under autonomy is left alone.
        rearms.clear()
        curiosity._dog_expression_name = "Hello"
        curiosity._dog_expression_deadline = 500.0
        curiosity._enforce_expression_watchdog(
            100.0, SimpleNamespace(mode=OperatingMode.AUTONOMOUS)
        )
        assert curiosity._dog_expression_name == "Hello"
        assert rearms == []
    finally:
        curiosity_module.ensure_motion_ready = original
