from __future__ import annotations

import asyncio
from collections import deque
import json
import math
from queue import Queue
import sqlite3
import struct
import subprocess
import sys
from threading import Condition, Event, RLock, Thread
import time
from types import SimpleNamespace

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
    _scout_rerun_config,
    scout,
)
from nightwatch.connection import GO2Connection
from nightwatch.contracts import (
    BehaviorKind,
    BehaviorLease,
    MissionMode,
    RobotActivity,
)
from nightwatch.curiosity import (
    CuriosityConfig,
    CuriositySupervisor,
    _skin_cover_fraction,
)
from nightwatch.escort import EscortConfig, EscortResult, run_escort
from nightwatch.follow import NightwatchPersonFollow
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
)
from nightwatch.map_stream import MapStreamer, PREMAP_MAGIC
from nightwatch.navigation import (
    NightwatchCoveragePatrolRouter,
    PatrollingModule,
    WavefrontFrontierExplorer,
    _prelock_world_keep_outs,
    _resolve_odom_epoch,
)
from nightwatch.speak import SpeakSkill, _chain_for_mode
from nightwatch.tracker import (
    YoloFollowTracker,
    _appearance_descriptor,
    _best_device,
)
from nightwatch.webchat import (
    _OPERATOR_ACTIONS,
    _OPERATOR_GUIDE_HTML,
    _OPERATOR_HTML,
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
from unitree_webrtc_connect.constants import SPORT_CMD

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
from dimos.protocol.pubsub.impl.lcmpubsub import LCM
from dimos.protocol.pubsub.impl.zenohpubsub import Zenoh
from dimos.robot.unitree.connection import UnitreeWebRTCConnection


def test_frontier_and_patrol_legs_allow_full_floor_travel() -> None:
    assert FRONTIER_GOAL_TIMEOUT_S >= 60.0
    assert PatrollingModule._patrol_goal_timeout_s >= 60.0


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
        SPORT_CMD["StandUp"],
        SPORT_CMD["RecoveryStand"],
        SPORT_CMD["BalanceStand"],
        SPORT_CMD["SwitchJoystick"],
    ]
    assert requests[-1]["parameter"] == {"data": True}


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
        SPORT_CMD["StandUp"],
        SPORT_CMD["RecoveryStand"],
    ]
    assert nightwatch_unitree._last_ready[0] == 0.0


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
    connection.liedown = lambda: events.append("liedown")
    connection.connection = SimpleNamespace(stop=lambda: events.append("webrtc"))
    monkeypatch.setattr(
        "dimos.core.module.Module.stop", lambda _self: events.append("module")
    )

    connection.stop()
    connection.stop()

    assert events == ["liedown", "webrtc", "module"]


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
    assert config.explore_liveness_timeout_s > 22.0
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


def test_reopened_exploration_rearms_map_completion_prompt() -> None:
    curiosity = object.__new__(CuriositySupervisor)
    curiosity._last_exploration_status_at = 0.0
    curiosity._map_phase = "MAPPED"
    curiosity._exploring = False
    curiosity._prior_session_mapped = False
    curiosity._map_completion_prompt_suppressed = True
    curiosity._map_completion_prompt_deadline = 123.0
    curiosity._retry_not_before = 0.0
    curiosity._explore = SimpleNamespace(
        exploration_status=lambda: {
            "active": False,
            "map_complete": False,
        }
    )
    curiosity._patrol = SimpleNamespace(
        patrol_status=lambda: {"no_goal_for_s": 9.0, "saturation": 1.0}
    )
    curiosity._stop_patrol = lambda _reason: None

    curiosity._refresh_map_phase(2.0)

    assert curiosity._map_phase == "EXPLORING"
    assert curiosity._map_completion_prompt_suppressed is False
    assert curiosity._map_completion_prompt_deadline == 0.0


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


def test_relocalization_never_accepts_repeated_below_threshold_transform(
    monkeypatch,
) -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc.config = SimpleNamespace(fitness_threshold=0.45, max_failed_attempts=12)
    reloc._premap = SimpleNamespace(pointcloud=object())
    reloc._failed_attempts = 0
    reloc._alignment_locked = False
    T = np.eye(4)
    # Repeated corridors can converge to the exact same, wildly wrong
    # transform. Agreement does not make a below-threshold match safe.
    T[:3, 3] = [1.0, -37.0, 0.0]
    monkeypatch.setattr(
        "nightwatch.memory._relocalize", lambda _premap, _scan: (T.copy(), 0.35)
    )

    first = reloc._try_relocalize(_FakeScan())
    assert first is None
    assert reloc._failed_attempts == 1

    second = reloc._try_relocalize(_FakeScan())
    assert second is None
    assert reloc._failed_attempts == 2
    assert reloc._alignment_locked is False


def test_relocalization_rejects_low_fitness(monkeypatch) -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc.config = SimpleNamespace(fitness_threshold=0.45, max_failed_attempts=12)
    reloc._failed_attempts = 0
    reloc._alignment_locked = False
    reloc._premap = SimpleNamespace(pointcloud=object())
    T = np.eye(4)
    monkeypatch.setattr(
        "nightwatch.memory._relocalize", lambda _premap, _scan: (T.copy(), 0.10)
    )

    assert reloc._try_relocalize(_FakeScan()) is None
    assert reloc._try_relocalize(_FakeScan()) is None
    assert reloc._alignment_locked is False
    assert reloc._failed_attempts == 2


def test_relocalization_aborts_four_decisively_wrong_matches(monkeypatch) -> None:
    reloc = object.__new__(NightwatchRelocalization)
    reloc.config = SimpleNamespace(fitness_threshold=0.55, max_failed_attempts=12)
    reloc._failed_attempts = 0
    reloc._alignment_locked = False
    reloc._rejected_fitnesses = []
    reloc._premap = SimpleNamespace(pointcloud=object())
    T = np.eye(4)
    monkeypatch.setattr(
        "nightwatch.memory._relocalize", lambda _premap, _scan: (T.copy(), 0.25)
    )

    for _ in range(4):
        assert reloc._try_relocalize(_FakeScan()) is None

    assert reloc._failed_attempts == 12
    assert reloc._alignment_locked is False


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

    # 10 s since explorer start is inside the 15 s startup grace.
    curiosity._enforce_liveness(now, NavigationState.IDLE)
    assert recoveries == []

    # Past startup grace, 5 s stationary is fine while exploring (30 s
    # window), but recovery fires once the longer window elapses.
    curiosity._explore_requested_at = now - 30.0
    curiosity._enforce_liveness(now, NavigationState.IDLE)
    assert recoveries == []
    curiosity._last_motion_ts = now - 31.0
    curiosity._enforce_liveness(now, NavigationState.IDLE)
    assert recoveries == [True]


def test_explore_gesture_cadence_is_sparser_than_mapped() -> None:
    config = CuriosityConfig()
    assert (
        config.dog_expression_explore_min_interval_s
        > config.dog_expression_max_interval_s
    )


def test_gesture_break_interrupts_exploration_while_mapping() -> None:
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
        is True
    )
    assert stops == [("dog expression break", True)]
    assert started


def test_exploration_mode_never_triggers_curious_follow() -> None:
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
    curiosity._mission_mode = MissionMode.EXPLORATION
    curiosity._exploring = True
    curiosity._patrolling = False
    curiosity._curious_follow_started = 0.0
    curiosity._navigation_state = lambda: NavigationState.FOLLOWING_PATH
    curiosity._is_following = lambda: False
    curiosity._person_is_close = lambda _now: True
    curiosity._interrupt_dog_expression = lambda _reason: None
    curiosity._stop_face_observation = lambda _reason: None
    curiosity._stop_patrol = lambda _reason: None
    curiosity._ensure_exploring = lambda _now: None
    curiosity._maybe_escape_stall = lambda _now: False
    curiosity._set_activity = lambda *_args: None
    follows: list[bool] = []
    curiosity._start_curious_follow = lambda: follows.append(True)

    curiosity._tick()

    assert follows == []


def test_patrol_scan_settings_persist_with_safe_bounds(tmp_path) -> None:
    settings_path = tmp_path / "operator-settings.json"
    curiosity = object.__new__(CuriositySupervisor)
    curiosity.config = CuriosityConfig(
        operator_settings_path=str(settings_path)
    )
    curiosity._lock = RLock()
    curiosity._patrol_active_elapsed_s = 500.0

    message = curiosity.set_patrol_scan_settings(999.0, 1.0)
    saved = json.loads(settings_path.read_text(encoding="utf-8"))

    assert "300s moving" in message
    assert saved["patrol_interval_s"] == 300.0
    assert saved["scan_duration_s"] == 10.0
    assert curiosity._load_operator_settings() == {
        "patrol_interval_s": 300.0,
        "scan_duration_s": 10.0,
    }


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


def test_stuck_escape_caps_at_three_then_asks_for_help() -> None:
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


def test_stuck_escape_give_up_speaks_help_once_and_holds() -> None:
    curiosity = _escape_ready_curiosity(CuriosityConfig())
    curiosity._escape_events = deque([1.0, 2.0, 3.0])
    spoken: list[str] = []
    activities: list[tuple] = []
    curiosity._speak_line = lambda text: spoken.append(text)
    curiosity._set_activity = lambda kind, owner, moving, reason: activities.append(
        (kind, owner, moving, reason)
    )
    curiosity._stop_exploration = lambda reason, *, cancel_goal: None
    curiosity._stop_patrol = lambda reason: None
    curiosity._publish_stop = lambda: None
    curiosity._lock = RLock()
    curiosity._explicit_hold_reason = None

    curiosity._escape_give_up(100.0)
    curiosity._escape_give_up(101.0)

    # Help line spoken exactly once; a hold is asserted while it waits.
    assert spoken == [CuriosityConfig().help_text]
    assert activities[-1] == (BehaviorKind.HOLD, "safety", False, "STUCK_HELP")
    assert curiosity._explicit_hold_reason == "STUCK_HELP"


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
        face_observation_streak=1,
        face_observation_s=15.0,
    )
    curiosity._map_phase = "MAPPED"
    curiosity._mission_mode = MissionMode.CRUISE
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
    pitches: list[float] = []
    monkeypatch.setattr("nightwatch.curiosity.ensure_motion_ready", lambda _c: True)
    monkeypatch.setattr(
        "nightwatch.curiosity.set_body_pitch",
        lambda _c, pitch: pitches.append(float(pitch)) or True,
    )

    now = 100.0
    assert curiosity._maybe_face_observation(now, NavigationState.IDLE) is True
    assert curiosity._face_observation_key == "person:visitor-a"
    assert curiosity._face_observation_until >= now + 14.0
    assert pitches == [curiosity.config.face_observation_pitch_rad]
    assert spoken == [curiosity.config.face_observation_prompt]
    assert activities[-1][1] == "face_observation"

    # An active path is never interrupted to acquire another face.
    curiosity._stop_face_observation("test complete")
    assert pitches[-1] == 0.0
    assert (
        curiosity._maybe_face_observation(now + 1.0, NavigationState.FOLLOWING_PATH)
        is False
    )


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


def test_growing_map_clouds_are_throttled_and_bounded_for_the_viewer() -> None:
    max_hz = _scout_rerun_config["max_hz"]
    assert max_hz["world/merged_map"] == 0.2
    assert max_hz["world/global_map"] == 1
    assert max_hz["world/loaded_map"] == 0.5

    override = _scout_rerun_config["visual_override"]
    for path in ("world/global_map", "world/merged_map", "world/loaded_map"):
        assert override[path] is _convert_big_cloud


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


def test_set_body_pitch_uses_euler_command_and_clamps() -> None:
    requests: list[dict] = []
    connection = SimpleNamespace(
        publish_request=lambda _topic, request: requests.append(request.copy())
        or {"status": "ok"}
    )
    assert nightwatch_unitree.set_body_pitch(connection, 0.35) is True
    assert requests[-1]["api_id"] == SPORT_CMD["Euler"]
    assert requests[-1]["parameter"] == {"x": 0.0, "y": 0.35, "z": 0.0}

    # Over-range up-tilt is clamped to the safe maximum.
    requests.clear()
    nightwatch_unitree.set_body_pitch(connection, 2.0)
    assert requests[-1]["parameter"]["y"] == pytest.approx(0.4)

    # Restore publishes a neutral Euler pose.
    requests.clear()
    nightwatch_unitree.set_body_pitch(connection, 0.0)
    assert requests[-1]["parameter"] == {"x": 0.0, "y": 0.0, "z": 0.0}

    # A rejected Euler ack is reported as failure (no silent success).
    rejected = SimpleNamespace(
        publish_request=lambda _topic, _request: {"status": "error", "code": 3}
    )
    assert nightwatch_unitree.set_body_pitch(rejected, 0.35) is False


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


def test_wave_protocol_stops_before_firmware_command_when_authority_is_revoked() -> None:
    waves, attempts, looked = run_wave_protocol(
        waver=lambda: pytest.fail("must not wave after manual takeover"),
        rearm=lambda: pytest.fail("must not rearm after manual takeover"),
        gaze_check=lambda: pytest.fail("must not inspect after manual takeover"),
        renew_lease=lambda: False,
        max_waves=4,
        max_attempts=8,
    )

    assert waves == 0
    assert attempts == 0
    assert looked is False


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


def test_operator_sleep_area_action_and_button_are_wired() -> None:
    # The console button maps to the escort skill with no arguments.
    assert _OPERATOR_ACTIONS["sleep_area"] == {
        "tool": "escort_to_sleeping_area",
        "args": {},
    }
    # The upgraded page exposes the unique Bedroom map and automatic flow.
    assert "http://localhost:3000/lidar" in _OPERATOR_HTML
    assert "接近最近的人" in _OPERATOR_HTML
    assert "标记 BEDROOM" not in _OPERATOR_HTML  # belongs to the map page


def test_operator_console_and_guide_offer_shared_bilingual_controls() -> None:
    assert 'id="languageZh"' in _OPERATOR_HTML
    assert 'id="languageEn"' in _OPERATOR_HTML
    assert 'href="/operator/help"' in _OPERATOR_HTML
    assert "nightwatch.operator.language" in _OPERATOR_HTML
    assert "data-i18n" in _OPERATOR_HTML

    assert 'id="languageZh"' in _OPERATOR_GUIDE_HTML
    assert 'id="languageEn"' in _OPERATOR_GUIDE_HTML
    assert 'href="/operator"' in _OPERATOR_GUIDE_HTML
    assert "nightwatch.operator.language" in _OPERATOR_GUIDE_HTML
    assert "Keyboard shortcuts" in _OPERATOR_GUIDE_HTML
    assert "安全优先" in _OPERATOR_GUIDE_HTML


def test_operator_guide_route_is_available_without_robot_actions() -> None:
    client = TestClient(_assessment_web_interface().app)
    response = client.get("/operator/help")

    assert response.status_code == 200
    assert 'id="languageZh"' in response.text
    assert 'href="/operator"' in response.text


def test_unique_bedroom_overwrites_and_persists_map_coordinates() -> None:
    model = object.__new__(NightwatchWorldModel)
    model._lock = RLock()
    model._db = sqlite3.connect(":memory:")
    model._session_id = "session-a"
    model._latest_odom = None
    model._world_to_map_cache = None
    model._world_to_map_cache_until = 0.0
    model._world_to_map = lambda: (10.0, 20.0, math.pi / 2)
    model._event = lambda *_args, **_kwargs: None
    model._create_schema()

    assert "Bedroom updated" in model.set_bedroom_at(1.0, 2.0, 0.0)
    assert "Bedroom updated" in model.set_bedroom_at(3.0, 4.0, 0.0)

    count = model._db.execute(
        "SELECT COUNT(*) FROM operator_destinations"
    ).fetchone()[0]
    row = model._db.execute(
        "SELECT map_x,map_y FROM operator_destinations WHERE name='bedroom'"
    ).fetchone()
    destination = model.bedroom_destination()

    assert count == 1
    assert row == pytest.approx((6.0, 23.0))
    assert destination is not None
    assert destination["x"] == pytest.approx(3.0)
    assert destination["y"] == pytest.approx(4.0)


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


def _speak_skill_for_chain(backends, monkeypatch, mode):
    monkeypatch.setenv("NIGHTWATCH_TTS", mode)
    skill_obj = object.__new__(SpeakSkill)
    skill_obj._backends = {b.name: b for b in backends}
    skill_obj._tmpdir = "/tmp"
    skill_obj._utt_counter = 0
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
    assert curiosity._retry_not_before == 10.0
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
