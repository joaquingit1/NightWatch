"""Always-on curiosity and deterministic motion ownership.

Curiosity is the robot's base behavior, not an idle animation.  It keeps
frontier exploration active while the agent is listening or thinking, yields
to real movement tools/manual control/following, and resumes immediately when
the higher-priority owner finishes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import random
import threading
import time
from typing import Any, Protocol
import uuid

from langchain_core.messages import AIMessage, BaseMessage
import numpy as np
from reactivex.disposable import Disposable
from unitree_webrtc_connect.constants import SPORT_CMD

from dimos.agents.agent_spec import AgentSpec
from dimos.agents.annotation import skill
from dimos.agents.skills.speak_skill_spec import SpeakSkillSpec
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import CostValues, OccupancyGrid
from dimos.msgs.sensor_msgs.Image import Image
from dimos.navigation.base import NavigationState
from dimos.navigation.navigation_spec import NavigationInterfaceSpec
from dimos.spec.utils import Spec
from dimos.utils.logging_config import setup_logger
from nightwatch.contracts import (
    BehaviorKind,
    BehaviorLease,
    InteractionState,
    OperatingMode,
    RobotActivity,
)
from nightwatch.mode_control import ModeController
from nightwatch.unitree import (
    clear_pose_mode,
    ensure_motion_ready,
    pose_mode_latched,
    set_body_pitch,
    wave_hello,
)

logger = setup_logger()

# Tools whose execution, rather than the LLM merely thinking, owns motion.
_MOVEMENT_TOOLS = frozenset(
    {
        "end_exploration",
        "follow_person",
        "stop_following",
        "go_to_visible_object",
        "navigate_with_text",
        "relative_move",
        "stop_patrol",
        "hold_position",
        "lie_down_until_resumed",
        "stand_up_and_resume",
        "return_home_now",
        "perform_dog_expression",
        "stop_idle_behavior",
        "stop_curiosity",
    }
)


class FollowSpec(Spec, Protocol):
    def is_following(self) -> bool: ...
    def observe_person(self, min_height_frac: float = 0.0) -> dict[str, Any] | None: ...
    def observe_waving_person(
        self, min_height_frac: float = 0.10
    ) -> dict[str, Any] | None: ...
    def observe_attentive_person(
        self,
        min_height_frac: float = 0.12,
        min_attention_s: float = 5.0,
    ) -> dict[str, Any] | None: ...
    def identify_visible_track(self, track_id: int) -> dict[str, Any] | None: ...
    def follow_person(
        self,
        query: str,
        initial_bbox: list[float] | None = None,
        initial_image: str | None = None,
        initial_track_id: int | None = None,
        person_id: str | None = None,
        identify_during_follow: bool = True,
    ) -> str: ...
    def stop_following(self) -> str: ...


class ExploreSpec(Spec, Protocol):
    def explore(self) -> bool: ...
    def is_exploration_active(self) -> bool: ...
    def stop_exploration(self) -> bool: ...
    def exploration_status(self) -> dict[str, Any]: ...
    def mark_keep_out_here(self, radius_m: float = 4.0) -> str: ...


class PatrolSpec(Spec, Protocol):
    def is_patrolling(self) -> bool: ...
    def patrol_status(self) -> dict[str, Any]: ...
    def ensure_patrolling(self) -> bool: ...
    def stop_patrolling(self) -> bool: ...


class CuriousConnectionSpec(Spec, Protocol):
    def battery_soc(self) -> int | None: ...
    def liedown(self) -> bool: ...
    def standup(self) -> bool: ...
    def sport_command(self, api_id: int) -> bool: ...


class RelocSpec(Spec, Protocol):
    def relocalization_status(self) -> dict[str, Any]: ...


class CuriosityConfig(ModuleConfig):
    enabled: bool = True
    # Sleep Analysis deliberately alternates locomotion with a short,
    # camera-up capture window. Keep both deadlines below the booth-visible
    # ten-second stillness limit and persist per-venue overrides.
    sleep_scan_interval_s: float = 25.0
    sleep_scan_duration_s: float = 7.0
    sleep_scan_interval_min_s: float = 10.0
    sleep_scan_interval_max_s: float = 300.0
    sleep_scan_duration_min_s: float = 3.0
    sleep_scan_duration_max_s: float = 9.0
    operator_settings_path: str = os.getenv(
        "NIGHTWATCH_OPERATOR_SETTINGS_PATH",
        "assets/output/maps/nightwatch_operator_settings.json",
    )
    # Exploration remains active through 6%. At 5% the supervisor cancels the
    # current autonomous leg and navigates to the saved launch origin; it lies
    # down only after arriving there.
    min_battery_soc: int = 5
    return_home_radius_m: float = 0.8
    return_home_retry_s: float = 5.0
    # Return through nearby recorded poses instead of asking a freshly started
    # local planner for one long path across an as-yet unloaded floor map.
    return_home_waypoint_radius_m: float = 0.55
    return_home_waypoint_max_m: float = 1.25
    return_home_max_waypoint_retries: int = 4
    home_state_path: str = os.getenv(
        "NIGHTWATCH_HOME_STATE_PATH", "assets/output/maps/nightwatch_home.json"
    )
    breadcrumb_state_path: str = os.getenv(
        "NIGHTWATCH_BREADCRUMB_STATE_PATH",
        "assets/output/maps/nightwatch_breadcrumbs.json",
    )
    home_max_age_s: float = 12 * 60 * 60
    breadcrumb_spacing_m: float = 0.75
    breadcrumb_save_s: float = 5.0
    breadcrumb_max_points: int = 4000
    poll_s: float = 0.1
    battery_check_s: float = 5.0
    sensor_stale_s: float = 4.0
    exploration_retry_s: float = 2.0
    # Product invariant: when curiosity owns motion, a stationary pose for
    # this long triggers a bounded recovery action.
    liveness_timeout_s: float = 3.0
    # Frontier scoring is vectorized and convergence now takes two 0.5 s empty
    # scans, so the former 30 s grace only hid goal starvation. Both autonomous
    # modes must either publish a route or relinquish ownership before the
    # booth-visible ten-second stillness threshold.
    explore_liveness_timeout_s: float = 6.0
    patrol_liveness_timeout_s: float = 6.0
    autonomy_start_grace_s: float = 5.0
    recovery_turn_s: float = 0.8
    recovery_turn_rad_s: float = 0.28
    motion_distance_m: float = 0.025
    motion_yaw_rad: float = 0.035
    person_close_frac: float = 0.45
    person_poll_s: float = 0.5
    follow_streak: int = 2
    follow_max_s: float = 25.0
    follow_cooldown_s: float = 60.0
    # Curious follow is evaluated continuously but may acquire motion only at a
    # natural IDLE boundary. Earlier builds cancelled live frontier routes for
    # every close body detection, causing minutes of follow/re-arm/replan churn.
    # Explicit follow_person tool calls and potential_detected are unaffected.
    curious_follow_enabled: bool = True
    follow_greeting_text: str = "你好呀！"
    # Never solicit attention during autonomous operation. In practice,
    # stopping and asking a passer-by to turn around strands the dog while the
    # person keeps walking. The camera/fatigue service already scores visible
    # faces passively; only an existing confident signal (or an observed wave)
    # may interrupt the route. Keep this legacy acquisition routine available
    # for an explicitly supervised experiment, but disabled in the product.
    face_observation_enabled: bool = False
    face_observation_min_height_frac: float = 0.12
    face_observation_streak: int = 2
    face_observation_poll_s: float = 0.15
    face_observation_s: float = 10.0
    face_observation_cooldown_s: float = 45.0
    face_observation_pitch_rad: float = 0.35
    face_observation_turn_rad_s: float = 0.35
    face_observation_turn_max_s: float = 0.8
    face_observation_prompt: str = "你好，请看我这边一下好吗？"
    # A body can be detected well before its face has enough pixels for the
    # landmark model. Use the already-locked visual follower as a bounded
    # acquisition approach; it naturally stops near 1.5 m.
    face_observation_approach_s: float = 4.0
    # Wave-back is a deterministic pose reflex from the shared YOLO skeleton,
    # not an LLM/VLM task. Two consecutive acquisition frames establish a
    # one-shot raised-hand edge. The physical gesture waits for a natural
    # between-leg navigation gap so it never cancels an active path.
    wave_response_enabled: bool = True
    wave_response_poll_s: float = 0.15
    wave_response_min_height_frac: float = 0.10
    wave_response_cooldown_s: float = 60.0
    wave_response_max_wait_s: float = 12.0
    wave_response_greeting: str = "你好！"
    # Sustained, frontal attention becomes a purposeful social destination,
    # but only between autonomous navigation legs. The dog approaches for a
    # short bounded interval, waves once, then resumes exploration.
    eye_contact_enabled: bool = True
    eye_contact_min_height_frac: float = 0.12
    eye_contact_dwell_s: float = 5.0
    eye_contact_poll_s: float = 0.25
    eye_contact_approach_s: float = 4.0
    eye_contact_cooldown_s: float = 90.0
    # Stuck-escape recovery. Lidar barely sees soft clutter (blankets,
    # backpacks), so the dog walks into a nest, the planner publishes a far goal
    # then reports "No path found" while the body has not moved. Displacement
    # detection (unlike the IDLE-only liveness turn) catches this while nav still
    # looks active; a short reverse backs the body out. A stall is one full
    # stall_window_s with under stall_distance_m of travel; the SECOND stall
    # within stall_pair_window_s triggers a reverse, capped per cap window.
    stall_window_s: float = 10.0
    stall_distance_m: float = 0.08
    stall_pair_window_s: float = 90.0
    escape_reverse_speed_mps: float = 0.18
    # A 1.0 s guarded reverse clears about 18 cm—enough to unload the front
    # bumper/feet—without spending the entire obstacle-response budget moving
    # backwards before A* can resume.
    escape_reverse_s: float = 1.0
    escape_hz: float = 5.0
    escape_max_per_window: int = 3
    escape_cap_window_s: float = 300.0
    make_way_text: str = "请让一让好呀，我要过去啦。"
    help_text: str = "我好像被卡住了，谁来帮帮我呀？"
    # Friendly firmware gestures. Go2 has no actuated tail or neck:
    # WiggleHips is the tail-wag analogue; turns are its "look around".
    # While mapping they run only at natural boundaries between frontier legs,
    # never by cancelling an active route.
    dog_expressions: bool = True
    dog_expressions_while_exploring: bool = True
    dog_expression_after_s: float = 20.0
    dog_expression_min_interval_s: float = 30.0
    dog_expression_max_interval_s: float = 60.0
    # A break during discovery costs 7-15 s of real coverage time (stop,
    # firmware routine, full stand-ready re-arm, explorer restart), so while
    # mapping the dog stays in character on a much sparser cadence. The
    # dense 30-60 s cadence above applies once the floor is MAPPED.
    dog_expression_explore_min_interval_s: float = 120.0
    dog_expression_explore_max_interval_s: float = 240.0
    # A palm close to the lens produces a sudden, frame-wide skin-colour
    # occlusion. Detection is sampled and hysteretic so a beige wall cannot
    # repeatedly fire it. The response is queued until navigation is IDLE;
    # an active planner path is never cancelled for a gesture.
    hand_cover_enabled: bool = True
    hand_cover_fraction: float = 0.60
    hand_cover_release_fraction: float = 0.35
    hand_cover_delta: float = 0.20
    hand_cover_sample_s: float = 0.20
    hand_cover_sustain_frames: int = 3
    hand_cover_idle_s: float = 1.0
    hand_gesture_cooldown_s: float = 30.0
    hand_gesture_max_wait_s: float = 30.0
    activity_hz: float = 2.0
    # A prior session that finished mapping records it here. Completion is
    # advisory by default: an exported premap can change independently of this
    # sidecar, and a stale MAPPED bit previously skipped straight to an
    # unreachable far-map patrol after a marginal ICP match. Deployments with a
    # separately validated immutable map may explicitly opt into fast-start.
    map_state_path: str = os.getenv(
        "NIGHTWATCH_MAP_STATE_PATH", "assets/output/maps/nightwatch_state.json"
    )
    trust_persisted_map_complete: bool = False


_DOG_EXPRESSIONS: tuple[tuple[str, float, float], ...] = (
    # name, approximate firmware routine duration, selection weight
    ("WiggleHips", 3.0, 4.0),
    ("Content", 2.5, 3.0),
    ("Scrape", 2.5, 2.0),
    ("Sit", 3.5, 1.5),
    ("Hello", 3.0, 1.0),
    ("Stretch", 4.0, 1.0),
)


def _skin_cover_fraction(image: Image) -> float:
    """Estimate how much of a frame is occupied by close-range skin.

    The YCbCr bounds cover a broad range of skin tones while excluding neutral
    grey/white occlusions. A coarse sample keeps this callback negligible next
    to mapping and navigation.
    """
    rgb = image.to_rgb().data
    if rgb.ndim != 3 or rgb.shape[2] < 3 or rgb.size == 0:
        return 0.0
    step = max(1, max(rgb.shape[:2]) // 96)
    sample = rgb[::step, ::step, :3].astype(np.float32, copy=False)
    if np.issubdtype(rgb.dtype, np.floating) and float(np.nanmax(sample)) <= 1.0:
        sample = sample * 255.0
    r, g, b = sample[..., 0], sample[..., 1], sample[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b
    skin = (
        (y >= 35.0)
        & (cb >= 72.0)
        & (cb <= 138.0)
        & (cr >= 128.0)
        & (cr <= 180.0)
        & (r >= g * 0.85)
    )
    return float(np.count_nonzero(skin) / skin.size)


class CuriositySupervisor(Module):
    config: CuriosityConfig

    _connection: CuriousConnectionSpec
    _follow: FollowSpec
    _explore: ExploreSpec
    _patrol: PatrolSpec
    _reloc: RelocSpec
    _navigation: NavigationInterfaceSpec
    _agent_spec: AgentSpec
    # Optional: speech is best-effort, so a stack without a speaker still runs.
    # Auto-wires by type to the SpeakSkill already in the coordinator.
    _speak: SpeakSkillSpec | None

    human_input: In[str]
    agent: In[BaseMessage]
    agent_idle: In[bool]
    odom: In[PoseStamped]
    global_costmap: In[OccupancyGrid]
    color_image: In[Image]
    navigation_obstacle: In[PoseStamped]
    recovery_request: In[PoseStamped]
    tele_cmd_vel: Out[Twist]
    activity: Out[RobotActivity]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        now = time.monotonic()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = bool(self.config.enabled)
        try:
            initial_mode = ModeController.parse_mode(
                os.getenv("NIGHTWATCH_OPERATING_MODE", "autonomous")
            )
        except ValueError:
            logger.warning(
                "invalid NIGHTWATCH_OPERATING_MODE; using autonomous",
                value=os.getenv("NIGHTWATCH_OPERATING_MODE"),
            )
            initial_mode = OperatingMode.AUTONOMOUS
        self._mode_control = ModeController(initial_mode)
        self._explicit_hold_reason: str | None = (
            "STARTUP_GUARD"
            if os.getenv("NIGHTWATCH_START_HELD", "").strip().lower()
            in {"1", "true", "yes", "on"}
            else None
        )
        self._agent_busy = False
        self._movement_intent_at = 0.0
        self._movement_intent_tool: str | None = None
        self._preempt_requested = False
        self._leases: dict[str, BehaviorLease] = {}

        self._last_odom: PoseStamped | None = None
        self._latest_costmap: OccupancyGrid | None = None
        self._motion_anchor: PoseStamped | None = None
        self._last_odom_at = 0.0
        self._last_costmap_at = 0.0
        self._last_motion_ts: float | None = None
        self._motion_watch_started_at = now

        self._battery_checked_at = 0.0
        self._battery_soc: int | None = None
        self._low_battery_liedown = False
        self._home_xy: tuple[float, float] | None = self._load_home_state()
        self._breadcrumbs: list[tuple[float, float]] = self._load_breadcrumb_state()
        self._last_breadcrumb_save_at = now
        self._returning_home = False
        self._home_goal_sent_at = 0.0
        self._return_waypoint_index: int | None = None
        self._return_waypoint_xy: tuple[float, float] | None = None
        self._return_waypoint_retries = 0
        self._return_route_blocked = False
        self._home_arrived = False
        self._operator_return_home = False
        self._operator_liedown = False
        self._exploring = False
        self._exploration_capability_bound = False
        self._explore_started_once = False
        self._explore_requested_at = 0.0
        self._patrol_requested_at = 0.0
        self._retry_not_before = 0.0
        self._map_phase = "NOT_READY"
        self._patrolling = False
        self._last_exploration_status_at = 0.0
        self._prior_session_mapped = self._load_map_state()
        self._no_motion_owner_warned_at = 0.0
        operator_settings = self._load_operator_settings()
        self._sleep_scan_interval_s = max(
            self.config.sleep_scan_interval_min_s,
            min(
                self.config.sleep_scan_interval_max_s,
                float(
                    operator_settings.get(
                        "sleep_scan_interval_s",
                        self.config.sleep_scan_interval_s,
                    )
                ),
            ),
        )
        self._sleep_scan_duration_s = max(
            self.config.sleep_scan_duration_min_s,
            min(
                self.config.sleep_scan_duration_max_s,
                float(
                    operator_settings.get(
                        "sleep_scan_duration_s",
                        self.config.sleep_scan_duration_s,
                    )
                ),
            ),
        )
        self._sleep_active_elapsed_s = 0.0
        self._last_tick_started_at = now
        self._scan_kind: str | None = None
        self._scan_started_at = 0.0
        self._intake_wait_until = 0.0

        self._last_person_poll = 0.0
        self._close_streak = 0
        self._close_bbox: list[float] | None = None
        self._close_track_id: int | None = None
        self._close_person_id: str | None = None
        self._follow_block_until = 0.0
        self._curious_follow_started = 0.0
        self._follow_stop_requested_at = 0.0
        self._face_observation_until = 0.0
        self._face_observation_key: str | None = None
        self._face_observation_candidate: str | None = None
        self._face_observation_streak = 0
        self._face_observation_last_poll = 0.0
        self._face_observation_pitch_active = False
        self._face_observation_bbox: list[float] | None = None
        self._face_observation_cooldowns: dict[str, float] = {}
        self._face_approach_until = 0.0
        self._wave_response_last_poll = 0.0
        self._wave_response_cooldowns: dict[str, float] = {}
        self._wave_response_global_until = 0.0
        self._wave_response_pending: dict[str, Any] | None = None
        self._wave_response_pending_at = 0.0
        self._eye_contact_last_poll = 0.0
        self._eye_contact_cooldown_until = 0.0
        self._attention_follow_until = 0.0
        self._dog_expression_name: str | None = None
        self._dog_expression_deadline = 0.0
        self._dog_expression_next_at = now + self.config.dog_expression_after_s
        self._last_dog_expression: str | None = None
        self._last_hand_scan_at = 0.0
        self._hand_cover_baseline: float | None = None
        self._hand_cover_fraction = 0.0
        self._hand_cover_streak = 0
        self._hand_cover_latched = False
        self._hand_cover_idle_since = 0.0
        self._hand_scan_navigation_idle = False
        self._hand_gesture_pending_at = 0.0
        self._hand_gesture_cooldown_until = 0.0

        self._behavior = BehaviorKind.HOLD
        self._owner = "safety"
        self._moving_expected = False
        self._hold_reason: str | None = "STARTING"
        self._last_activity_publish = 0.0
        self._twist_in_flight = False

        # Stuck-escape state. _stall_anchor is the (t, x, y) the window is
        # measured from; _stall_events and _escape_events are pruned timestamp
        # windows for the pair trigger and the per-window escape cap.
        self._stall_anchor: tuple[float, float, float] | None = None
        self._stall_events: deque[float] = deque()
        self._escape_events: deque[float] = deque()
        self._escape_help_spoken = False
        self._route_hazard_escape_requested = False

    @rpc
    def start(self) -> None:
        super().start()
        self._stop_event.clear()
        self.register_disposable(
            Disposable(self.human_input.subscribe(self._on_human_input))
        )
        self.register_disposable(Disposable(self.agent.subscribe(self._on_agent)))
        self.register_disposable(
            Disposable(self.agent_idle.subscribe(self._on_agent_idle))
        )
        self.register_disposable(Disposable(self.odom.subscribe(self._on_odom)))
        self.register_disposable(
            Disposable(self.global_costmap.subscribe(self._on_costmap))
        )
        self.register_disposable(Disposable(self.color_image.subscribe(self._on_image)))
        if self.navigation_obstacle.transport is not None:
            self.register_disposable(
                Disposable(
                    self.navigation_obstacle.subscribe(
                        self._on_navigation_obstacle
                    )
                )
            )
        if self.recovery_request.transport is not None:
            self.register_disposable(
                Disposable(self.recovery_request.subscribe(self._on_recovery_request))
            )
        self._thread = threading.Thread(
            target=self._run, name="CuriositySupervisor-loop", daemon=True
        )
        self._thread.start()
        logger.info(
            "CuriositySupervisor started",
            enabled=self._enabled,
            liveness_timeout_s=self.config.liveness_timeout_s,
        )

    @rpc
    def stop(self) -> None:
        self._stop_event.set()
        # Do not make cross-worker RPC calls during coordinator teardown:
        # dependency workers may already be stopping, which previously wedged
        # this shared worker until the coordinator killed it. The explorer and
        # navigator each stop/cancel in their own lifecycle hooks.
        self._exploring = False
        self._publish_stop()
        self._save_breadcrumb_state()
        if self._map_phase == "MAPPED":
            self._save_map_state()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        self._thread = None
        super().stop()

    @skill
    def start_curiosity(self) -> str:
        """Enable the always-on curious base behavior immediately."""
        with self._lock:
            self._enabled = True
            self._explicit_hold_reason = None
            self._retry_not_before = 0.0
            self._escape_events.clear()
            self._escape_help_spoken = False
        return "Curiosity is enabled and will move as soon as safety inputs are ready."

    @skill
    def stop_curiosity(self, reason: str = "operator requested hold") -> str:
        """Disable autonomous curiosity and hold position."""
        with self._lock:
            self._enabled = False
            self._explicit_hold_reason = reason
            self._preempt_requested = True
        return f"Curiosity disabled; holding position ({reason})."

    @skill
    def hold_position(self, reason: str = "operator requested hold") -> str:
        """Explicitly permit the robot to remain still until resumed."""
        with self._lock:
            self._explicit_hold_reason = reason
            self._preempt_requested = True
        return f"Holding position ({reason}). Call resume_curiosity to continue."

    @skill
    def resume_curiosity(self) -> str:
        """Release an explicit hold and return to curious exploration."""
        with self._lock:
            self._enabled = True
            self._explicit_hold_reason = None
            self._operator_return_home = False
            self._retry_not_before = 0.0
            self._escape_events.clear()
            self._escape_help_spoken = False
        return "Hold released; curious exploration is active."

    def _modes(self) -> ModeController:
        """Return the mode controller, including for legacy replay/test objects."""
        controller = getattr(self, "_mode_control", None)
        if controller is None:
            # Older serialized/test supervisors predate explicit modes and
            # historically enabled patrol/social behavior. Preserve that
            # compatibility only for such partially constructed instances;
            # every real __init__ starts in AUTONOMOUS unless configured.
            controller = ModeController(OperatingMode.SLEEP_ANALYSIS)
            self._mode_control = controller
        return controller

    @skill
    def set_operating_mode(self, mode: str) -> str:
        """Select Autonomous, Sleep Analysis, or Manual Override.

        Mode transfer is atomic. It invalidates commands from the prior epoch,
        cleans up any scan posture, stops the prior autonomous owner, and
        leaves Manual Override latched until another explicit mode selection.
        """
        try:
            transition = self._modes().transition(mode)
        except ValueError as exc:
            return str(exc)
        if not transition.changed:
            return (
                f"Operating mode is already {transition.current.value} "
                f"(epoch {transition.epoch})."
            )
        with self._lock:
            self._preempt_requested = True
            self._leases.clear()
            self._sleep_active_elapsed_s = self._initial_sleep_scan_elapsed(
                transition.current
            )
            self._retry_not_before = 0.0
            self._intake_wait_until = 0.0
        return (
            f"Operating mode changed to {transition.current.value}; "
            f"command epoch is {transition.epoch}."
        )

    def _initial_sleep_scan_elapsed(self, mode: OperatingMode) -> float:
        """Seed the scan clock so the first Sleep Analysis scan is prompt.

        Entering Sleep Analysis must produce a visible bounded scan within
        one short, well-defined delay (the hard minimum scan interval)
        instead of one full cadence interval. Every other mode starts from
        zero so a stale clock can never fire a scan on re-entry.
        """
        if mode is not OperatingMode.SLEEP_ANALYSIS:
            return 0.0
        interval = float(
            getattr(
                self,
                "_sleep_scan_interval_s",
                self.config.sleep_scan_interval_s,
            )
        )
        return max(0.0, interval - self.config.sleep_scan_interval_min_s)

    @skill
    def set_mission_mode(self, mode: str) -> str:
        """Compatibility adapter for exploration/cruise workbench clients."""
        normalized = str(mode).strip().lower()
        if normalized == "exploration":
            return self.set_operating_mode(OperatingMode.AUTONOMOUS.value)
        if normalized == "cruise":
            return self.set_operating_mode(OperatingMode.SLEEP_ANALYSIS.value)
        return "Unsupported mission mode. Choose exploration or cruise."

    @skill
    def set_control_mode(self, mode: str) -> str:
        """Compatibility adapter for autonomous/manual workbench clients."""
        normalized = str(mode).strip().lower()
        if normalized == "manual":
            return self.set_operating_mode(OperatingMode.MANUAL.value)
        if normalized != "autonomous":
            return "Unsupported control mode. Choose autonomous or manual."
        transition = self._modes().resume_autonomy()
        with self._lock:
            self._preempt_requested = True
            self._leases.clear()
            self._sleep_active_elapsed_s = self._initial_sleep_scan_elapsed(
                transition.current
            )
            self._retry_not_before = 0.0
        return (
            f"Operating mode changed to {transition.current.value}; "
            f"command epoch is {transition.epoch}."
            if transition.changed
            else f"Autonomous control is already active in "
            f"{transition.current.value}."
        )

    @skill
    def note_manual_input(self, epoch: int, sequence: int) -> str:
        """Accept one ordered browser command in the current Manual epoch."""
        accepted, reason = self._modes().accept_manual(
            epoch=int(epoch),
            sequence=int(sequence),
        )
        if not accepted:
            return f"Manual input refused: {reason}."
        with self._lock:
            self._leases.clear()
        return f"Manual input {sequence} accepted for epoch {epoch}."

    @skill
    def request_fatigue_scan(self) -> str:
        """Request one immediate bounded camera-up scan."""
        accepted, reason = self._modes().request_scan()
        if not accepted:
            return f"Fatigue scan refused: {reason}."
        with self._lock:
            self._preempt_requested = True
        return "Immediate sleep-analysis scan requested."

    @skill
    def set_sleep_scan_settings(
        self, interval_s: float = 25.0, duration_s: float = 7.0
    ) -> str:
        """Update Sleep Analysis scan cadence within hard safety limits."""
        interval = max(
            self.config.sleep_scan_interval_min_s,
            min(self.config.sleep_scan_interval_max_s, float(interval_s)),
        )
        duration = max(
            self.config.sleep_scan_duration_min_s,
            min(self.config.sleep_scan_duration_max_s, float(duration_s)),
        )
        with self._lock:
            self._sleep_scan_interval_s = interval
            self._sleep_scan_duration_s = duration
            self._sleep_active_elapsed_s = min(
                self._sleep_active_elapsed_s, interval
            )
        self._save_operator_settings()
        return (
            f"Sleep scan cadence set to {interval:.0f}s moving and "
            f"{duration:.0f}s scanning."
        )

    @skill
    def set_patrol_scan_settings(
        self, interval_s: float = 25.0, duration_s: float = 7.0
    ) -> str:
        """Compatibility alias for set_sleep_scan_settings."""
        return self.set_sleep_scan_settings(interval_s, duration_s)

    @skill
    def begin_intake_wait(self, timeout_s: float = 180.0) -> str:
        """Hold near one visitor while the bound QR/NFC form is active."""
        if self._modes().snapshot().mode is not OperatingMode.SLEEP_ANALYSIS:
            return "Intake wait refused outside Sleep Analysis mode."
        duration = max(5.0, min(180.0, float(timeout_s)))
        if not self._modes().set_interaction(InteractionState.WAITING_FORM):
            return "Intake wait refused because another interaction owns motion."
        with self._lock:
            self._intake_wait_until = time.monotonic() + duration
            self._preempt_requested = True
        return f"Bound intake wait started for at most {duration:.0f}s."

    @skill
    def end_intake_wait(self) -> str:
        """End the bound form wait and resume the selected autonomous mission."""
        with self._lock:
            self._intake_wait_until = 0.0
            self._retry_not_before = time.monotonic() + 0.25
        self._modes().set_interaction(InteractionState.IDLE)
        return "Bound intake wait ended; patrol will resume."

    @skill
    def lie_down_until_resumed(self) -> str:
        """Stop autonomy and lie down until the operator explicitly stands it."""
        with self._lock:
            self._enabled = True
            self._explicit_hold_reason = "OPERATOR_LYING_DOWN"
            self._preempt_requested = True
            self._operator_liedown = False
        return "Lie-down requested; autonomy will remain held until Stand is pressed."

    @skill
    def stand_up_and_resume(self) -> str:
        """Stand safely, release the posture hold, and resume autonomy."""
        try:
            ready = bool(ensure_motion_ready(self._connection, force=True))
        except Exception:
            logger.exception("operator stand-up failed")
            ready = False
        if not ready:
            return "Stand-up failed; the robot remains safely held."
        with self._lock:
            self._enabled = True
            self._explicit_hold_reason = None
            self._operator_liedown = False
            self._returning_home = False
            self._return_waypoint_index = None
            self._return_waypoint_xy = None
            self._return_waypoint_retries = 0
            self._return_route_blocked = False
            self._home_arrived = False
            self._operator_return_home = False
            self._retry_not_before = time.monotonic() + 0.5
            self._last_motion_ts = time.monotonic()
        return "Robot is standing; curious exploration will resume."

    @skill
    def set_home_here(self) -> str:
        """Record the current odometry position as the low-battery home."""
        with self._lock:
            odom = self._last_odom
            if odom is None:
                return "Cannot set home before odometry is available."
            self._home_xy = (float(odom.position.x), float(odom.position.y))
            self._breadcrumbs = [self._home_xy]
            self._return_waypoint_index = None
            self._home_arrived = False
        self._save_home_state()
        self._save_breadcrumb_state()
        return f"Home set at ({self._home_xy[0]:.2f}, {self._home_xy[1]:.2f})."

    @skill
    def return_home_now(self) -> str:
        """Recall the dog to the saved home base and hold there."""
        with self._lock:
            if self._home_xy is None:
                return "Cannot return home because no booth home has been set."
            self._enabled = True
            self._explicit_hold_reason = None
            self._operator_return_home = True
            self._home_arrived = False
            self._return_route_blocked = False
            self._returning_home = False
            self._return_waypoint_index = None
            self._return_waypoint_xy = None
            self._return_waypoint_retries = 0
            self._preempt_requested = True
            return "Returning to the saved home base; the robot will hold on arrival."

    # Compatibility names retained for the existing prompt/tool surface.
    @skill
    def start_idle_behavior(self) -> str:
        """Compatibility alias for start_curiosity."""
        return self.start_curiosity()

    @skill
    def stop_idle_behavior(self) -> str:
        """Compatibility alias for an explicit curiosity hold."""
        return self.stop_curiosity()

    @rpc
    def acquire_behavior(
        self,
        owner: str,
        behavior: str,
        priority: int,
        ttl_s: float,
        reason: str,
    ) -> dict[str, Any]:
        """Acquire a renewable higher-level behavior lease.

        The highest-priority unexpired lease is the only accepted external
        motion owner.  This is the integration point for patrol, intervention,
        escort, and manual-control modules.
        """
        kind = BehaviorKind(behavior)
        now = time.monotonic()
        mode = self._modes().snapshot(now=now).mode
        if mode is OperatingMode.MANUAL:
            return {
                "accepted": False,
                "reason": "manual override is latched",
            }
        if mode is OperatingMode.AUTONOMOUS and kind in {
            BehaviorKind.OBSERVE,
            BehaviorKind.FOLLOW,
            BehaviorKind.INTERVENE,
            BehaviorKind.ESCORT,
        }:
            return {
                "accepted": False,
                "reason": "people interaction is disabled in autonomous mode",
            }
        lease = BehaviorLease(
            owner=owner,
            lease_id=uuid.uuid4().hex,
            behavior=kind,
            priority=int(priority),
            issued_at=now,
            expires_at=now + max(0.1, float(ttl_s)),
            reason=reason,
        )
        with self._lock:
            self._expire_leases_locked(now)
            current = self._top_lease_locked()
            if current is not None and current.priority > lease.priority:
                return {"accepted": False, "current": asdict(current)}
            self._leases[lease.lease_id] = lease
            self._preempt_requested = True
        return {"accepted": True, "lease": asdict(lease)}

    @rpc
    def renew_behavior(self, lease_id: str, ttl_s: float) -> bool:
        now = time.monotonic()
        with self._lock:
            current = self._leases.get(lease_id)
            if current is None or current.expires_at <= now:
                self._leases.pop(lease_id, None)
                return False
            self._leases[lease_id] = BehaviorLease(
                owner=current.owner,
                lease_id=current.lease_id,
                behavior=current.behavior,
                priority=current.priority,
                issued_at=current.issued_at,
                expires_at=now + max(0.1, float(ttl_s)),
                reason=current.reason,
            )
        return True

    @rpc
    def release_behavior(self, lease_id: str) -> bool:
        with self._lock:
            return self._leases.pop(lease_id, None) is not None

    @skill
    def curiosity_status(self) -> dict[str, Any]:
        """Return the current motion owner, holds, sensors, and liveness state."""
        status = asdict(self._activity_snapshot())
        mode = self._modes().snapshot()
        status.update(
            {
                "home_xy": list(self._home_xy) if self._home_xy is not None else None,
                "returning_home": self._returning_home,
                "home_arrived": self._home_arrived,
                "operator_return_home": self._operator_return_home,
                "return_route_blocked": self._return_route_blocked,
                "breadcrumb_count": len(self._breadcrumbs),
                "return_waypoint_index": self._return_waypoint_index,
                "hand_cover_fraction": round(self._hand_cover_fraction, 3),
                "hand_gesture_pending": bool(self._hand_gesture_pending_at),
                "operator_liedown": self._operator_liedown,
                "face_observation_active": bool(self._face_observation_until),
                "face_observation_subject": self._face_observation_key,
                "face_observation_bbox": self._face_observation_bbox,
                "operating_mode": mode.mode.value,
                "mode_epoch": mode.epoch,
                "interaction_state": mode.interaction_state.value,
                "manual_input_seq": mode.manual_sequence,
                "manual_command_age_s": mode.manual_command_age_s,
                "scan_requested": mode.scan_requested,
                "scan_active": mode.interaction_state
                in {
                    InteractionState.SCHEDULED_SCAN,
                    InteractionState.FORCED_SCAN,
                },
                "scan_interval_s": self._sleep_scan_interval_s,
                "scan_duration_s": self._sleep_scan_duration_s,
                "scan_remaining_s": max(
                    0.0, self._face_observation_until - time.monotonic()
                )
                if self._scan_kind
                else 0.0,
                "interaction_remaining_s": max(
                    0.0,
                    float(getattr(self, "_intake_wait_until", 0.0))
                    - time.monotonic(),
                ),
                # Compatibility projection for the imported workbench.
                "mission_mode": (
                    "cruise"
                    if mode.mode is OperatingMode.SLEEP_ANALYSIS
                    else "exploration"
                ),
                "control_mode": (
                    "manual"
                    if mode.mode is OperatingMode.MANUAL
                    else "autonomous"
                ),
            }
        )
        return status

    @skill
    def perform_dog_expression(self, expression: str = "WiggleHips") -> str:
        """Perform one safe, friendly dog-like gesture while stationary.

        Supported expressions are WiggleHips (tail-wag analogue), Content
        (happy), Scrape (pawing), Sit, Hello (wave), and Stretch (play bow).
        Dynamic tricks such as flips, jumps, dances, and wallowing are
        deliberately unavailable through this safe expression skill.
        """
        match = next(
            (
                item
                for item in _DOG_EXPRESSIONS
                if item[0].casefold() == expression.casefold()
            ),
            None,
        )
        if match is None:
            return (
                f"Unsupported safe expression: {expression}. Choose one of "
                + ", ".join(item[0] for item in _DOG_EXPRESSIONS)
                + "."
            )
        if (
            self._battery_soc is None
            or self._battery_soc <= self.config.min_battery_soc
        ):
            return "Dog expression refused because battery is unknown or low."
        if self._explicit_hold_reason:
            return (
                f"Dog expression refused while holding: {self._explicit_hold_reason}."
            )
        # An external behavior lease (intervene, escort, manual, ...) owns
        # motion; a sport gesture would fight it for the body controller. The
        # _tick arbitration already returns before _maybe_dog_expression while
        # a lease is active; this guards the same contract on the directly
        # callable skill so the agent cannot gesture during an intervention.
        with self._lock:
            self._expire_leases_locked(time.monotonic())
            active_lease = self._top_lease_locked()
        if active_lease is not None:
            return (
                f"Dog expression refused because {active_lease.owner} owns motion "
                f"({active_lease.behavior.value})."
            )
        if self._navigation_state() is not NavigationState.IDLE or self._is_following():
            return "Dog expression refused because another movement behavior is active."
        if self._dog_expression_name is not None:
            return f"Already performing {self._dog_expression_name}."

        name, duration_s, _weight = match
        if not self._start_dog_expression(name, duration_s, time.monotonic()):
            return f"Firmware refused the {name} expression."
        return f"Performing {name}."

    def _on_human_input(self, _query: str) -> None:
        # Deliberately no stop: curiosity continues while the model thinks.
        logger.info("curiosity: human input received; base motion continues")

    def _on_agent_idle(self, idle: bool) -> None:
        with self._lock:
            self._agent_busy = not bool(idle)

    def _on_agent(self, msg: BaseMessage) -> None:
        if not isinstance(msg, AIMessage):
            return
        calls = getattr(msg, "tool_calls", None) or []
        movement = next(
            (
                str(call.get("name"))
                for call in calls
                if str(call.get("name")) in _MOVEMENT_TOOLS
            ),
            None,
        )
        if movement is None:
            return
        with self._lock:
            self._movement_intent_at = time.monotonic()
            self._movement_intent_tool = movement
            self._preempt_requested = True
        logger.info("curiosity: movement tool intent", tool=movement)

    def _on_costmap(self, msg: OccupancyGrid) -> None:
        with self._lock:
            self._latest_costmap = msg
            self._last_costmap_at = time.monotonic()
            if self._map_phase == "NOT_READY":
                self._map_phase = "EXPLORING"

    def _on_navigation_obstacle(self, pose: PoseStamped) -> None:
        """Request one guarded physical escape from a proven route boundary."""
        with self._lock:
            self._route_hazard_escape_requested = True
        logger.warning(
            "route-boundary escape requested",
            x=round(float(pose.position.x), 2),
            y=round(float(pose.position.y), 2),
        )

    def _on_recovery_request(self, pose: PoseStamped) -> None:
        """Escape a globally blocked start without learning a false obstacle."""
        with self._lock:
            self._route_hazard_escape_requested = True
        logger.warning(
            "global no-path escape requested",
            x=round(float(pose.position.x), 2),
            y=round(float(pose.position.y), 2),
        )

    def _on_image(self, image: Image) -> None:
        """Queue one gesture after a sustained sudden >60% hand occlusion."""
        if not self.config.hand_cover_enabled:
            return
        now = time.monotonic()
        if now - self._last_hand_scan_at < self.config.hand_cover_sample_s:
            return
        self._last_hand_scan_at = now
        try:
            fraction = _skin_cover_fraction(image)
        except Exception:
            logger.exception("hand-cover frame analysis failed")
            return
        with self._lock:
            self._hand_cover_fraction = fraction
            baseline = self._hand_cover_baseline
            if baseline is None:
                self._hand_cover_baseline = fraction
                return
            # A turn from a cool-coloured scene to wood, beige fabric, or a
            # warm wall can satisfy broad skin-colour bounds over most of the
            # image. Never interpret scene changes while the body is moving as
            # a hand. The status tick maintains this gate from the planner's
            # actual state, and a stable idle dwell re-baselines the new view
            # before detection is armed.
            if not getattr(self, "_hand_scan_navigation_idle", False):
                self._hand_cover_idle_since = 0.0
                self._hand_cover_streak = 0
                self._hand_cover_baseline = fraction
                return
            if not self._hand_cover_idle_since:
                self._hand_cover_idle_since = now
                self._hand_cover_streak = 0
                self._hand_cover_baseline = fraction
                return
            if now - self._hand_cover_idle_since < self.config.hand_cover_idle_s:
                self._hand_cover_streak = 0
                self._hand_cover_baseline = fraction
                return
            if self._hand_cover_latched:
                if fraction <= self.config.hand_cover_release_fraction:
                    self._hand_cover_latched = False
                    self._hand_cover_streak = 0
                    self._hand_cover_baseline = fraction
                return
            sudden = fraction - baseline >= self.config.hand_cover_delta
            if (
                now >= self._hand_gesture_cooldown_until
                and fraction >= self.config.hand_cover_fraction
                and sudden
            ):
                self._hand_cover_streak += 1
                if self._hand_cover_streak >= self.config.hand_cover_sustain_frames:
                    self._hand_cover_latched = True
                    self._hand_cover_streak = 0
                    self._hand_gesture_pending_at = now
                    self._hand_gesture_cooldown_until = (
                        now + self.config.hand_gesture_cooldown_s
                    )
                    logger.info(
                        "camera hand-cover gesture queued",
                        covered_fraction=round(fraction, 3),
                    )
                return
            self._hand_cover_streak = 0
            # Follow slow scene/lighting changes but preserve sensitivity to a
            # suddenly introduced palm.
            self._hand_cover_baseline = 0.9 * baseline + 0.1 * fraction

    def _on_odom(self, msg: PoseStamped) -> None:
        now = time.monotonic()
        with self._lock:
            self._last_odom = msg
            self._last_odom_at = now
            if self._home_xy is None:
                self._home_xy = (
                    float(msg.position.x),
                    float(msg.position.y),
                )
                self._breadcrumbs = [self._home_xy]
                self._save_home_state()
                self._save_breadcrumb_state()
                logger.info(
                    "low-battery home captured",
                    x=round(self._home_xy[0], 2),
                    y=round(self._home_xy[1], 2),
                )
            self._record_breadcrumb_locked(msg, now)
            anchor = self._motion_anchor
            if anchor is None:
                self._motion_anchor = msg
                return
            # Compare against the last confirmed-motion anchor, not the
            # immediately previous high-rate sample. Slow valid walking moves
            # only a few millimetres per sample and was previously mistaken
            # for a three-second stall.
            dx = msg.position.x - anchor.position.x
            dy = msg.position.y - anchor.position.y
            distance = math.hypot(dx, dy)
            yaw_delta = abs(
                math.atan2(
                    math.sin(msg.orientation.euler.z - anchor.orientation.euler.z),
                    math.cos(msg.orientation.euler.z - anchor.orientation.euler.z),
                )
            )
            if (
                distance >= self.config.motion_distance_m
                or yaw_delta >= self.config.motion_yaw_rad
            ):
                self._last_motion_ts = now
                self._motion_anchor = msg

    def _run(self) -> None:
        while not self._stop_event.wait(self.config.poll_s):
            try:
                self._tick()
            except Exception:
                logger.exception("curiosity supervisor tick failed")
                self._set_activity(
                    BehaviorKind.HOLD, "safety", False, "SUPERVISOR_ERROR"
                )
                self._stop_event.wait(0.5)

    def _tick(self) -> None:
        now = time.monotonic()
        previous_tick = float(getattr(self, "_last_tick_started_at", now))
        tick_elapsed = max(0.0, min(1.0, now - previous_tick))
        self._last_tick_started_at = now
        self._enforce_pose_mode_invariant(now)
        self._refresh_battery(now)
        following = self._is_following()
        nav_state = self._navigation_state()
        mode = self._modes().snapshot(now=now)

        with self._lock:
            self._hand_scan_navigation_idle = nav_state is NavigationState.IDLE
            self._expire_leases_locked(now)
            lease = self._top_lease_locked()
            enabled = self._enabled
            explicit_hold = self._explicit_hold_reason
            preempt = self._preempt_requested
            self._preempt_requested = False
            route_hazard_escape = bool(
                getattr(self, "_route_hazard_escape_requested", False)
            )
            intent_active = bool(
                self._movement_intent_tool
                and (
                    self._agent_busy
                    or now - self._movement_intent_at < 1.0
                    or (nav_state is not NavigationState.IDLE and not self._exploring)
                )
            )

        if preempt:
            self._stop_face_observation("higher-priority intent")
            self._stop_exploration("higher-priority intent", cancel_goal=True)
            self._stop_patrol("higher-priority intent")

        # A physical operator posture command is an immediate body-level hold,
        # including at critical battery. Pressing Stand releases it, after
        # which low-battery return-home resumes on the next tick.
        if explicit_hold == "OPERATOR_LYING_DOWN":
            self._hold(explicit_hold)
            return
        # Manual is a latched, direct relay-app equivalent. It is deliberately
        # evaluated before autonomy's battery/sensor readiness gates so a
        # stale model, map, or odometry stream cannot refuse operator motion.
        # Physical firmware safety remains authoritative below this process.
        if mode.mode is OperatingMode.MANUAL:
            self._stop_face_observation("manual override")
            self._interrupt_dog_expression("manual override")
            self._stop_exploration("manual override", cancel_goal=True)
            self._stop_patrol("manual override")
            if following:
                self._request_follow_stop("manual override")
            self._set_activity(BehaviorKind.MANUAL, "operator", False, None)
            return
        if self._battery_soc is None:
            self._hold("BATTERY_UNKNOWN")
            return
        if self._battery_soc <= self.config.min_battery_soc:
            self._stop_face_observation("low-battery return home")
            self._return_home(now, nav_state)
            return
        if not enabled:
            self._hold(explicit_hold or "CURIOSITY_DISABLED")
            return
        if explicit_hold:
            self._hold(explicit_hold)
            return
        if not self._sensors_ready(now):
            self._hold("SENSORS_NOT_READY")
            return
        intake_wait_until = float(
            getattr(self, "_intake_wait_until", 0.0)
        )
        if intake_wait_until:
            if (
                mode.mode is not OperatingMode.SLEEP_ANALYSIS
                or now >= intake_wait_until
            ):
                self._intake_wait_until = 0.0
                self._modes().set_interaction(InteractionState.IDLE)
            else:
                self._stop_face_observation("waiting for bound intake")
                self._interrupt_dog_expression("waiting for bound intake")
                self._stop_exploration(
                    "waiting for bound intake",
                    cancel_goal=True,
                )
                self._stop_patrol("waiting for bound intake")
                if following:
                    self._request_follow_stop("waiting for bound intake")
                self._set_activity(
                    BehaviorKind.INTERVENE,
                    "intake_wait",
                    False,
                    "WAITING_FORM",
                )
                return
        if lease is not None:
            self._stop_face_observation(f"leased to {lease.owner}")
            self._interrupt_dog_expression(f"leased to {lease.owner}")
            self._stop_exploration(f"leased to {lease.owner}", cancel_goal=False)
            self._stop_patrol(f"leased to {lease.owner}")
            self._set_activity(
                lease.behavior,
                lease.owner,
                lease.behavior is not BehaviorKind.HOLD,
                lease.reason if lease.behavior is BehaviorKind.HOLD else None,
            )
            return
        if route_hazard_escape:
            # The planner has already cancelled the route. Move the body out of
            # contact before allowing a new route, but only when the rear swept
            # footprint is proven clear by the live costmap. A global no-path
            # request deliberately does not record a current-pose exclusion.
            with self._lock:
                self._route_hazard_escape_requested = False
                # A queued social reaction belongs to the abandoned route
                # boundary. Never wave first and escape several seconds later.
                self._wave_response_pending = None
                self._wave_response_pending_at = 0.0
                self._hand_gesture_pending_at = 0.0
                self._attention_follow_until = 0.0
            self._interrupt_dog_expression("route-boundary hazard")
            if following:
                try:
                    self._follow.stop_following()
                except Exception:
                    logger.exception("route-boundary follow stop failed")
                self._publish_stop()
            self._run_escape(now)
            return
        if getattr(self, "_operator_return_home", False):
            if following:
                try:
                    self._follow.stop_following()
                except Exception:
                    logger.exception("operator return-home follow stop failed")
                self._publish_stop()
                return
            self._return_home(now, nav_state, lie_down=False)
            return
        if following:
            attention_until = float(
                getattr(self, "_attention_follow_until", 0.0)
            )
            if attention_until:
                if now < attention_until:
                    self._set_activity(
                        BehaviorKind.FOLLOW,
                        "sustained_eye_contact",
                        True,
                        None,
                    )
                    return
                try:
                    self._follow.stop_following()
                except Exception:
                    logger.exception("eye-contact approach stop failed")
                self._attention_follow_until = 0.0
                self._publish_stop()
                self._retry_not_before = now + 0.25
                return
            face_until = float(getattr(self, "_face_observation_until", 0.0))
            approach_until = float(getattr(self, "_face_approach_until", 0.0))
            if face_until and approach_until:
                if now < approach_until:
                    self._set_activity(
                        BehaviorKind.FOLLOW,
                        "face_observation",
                        True,
                        None,
                    )
                    return
                try:
                    self._follow.stop_following()
                except Exception:
                    logger.exception("face acquisition approach stop failed")
                self._face_approach_until = 0.0
                self._publish_stop()
                self._set_activity(
                    BehaviorKind.PATROL,
                    "face_observation",
                    False,
                    None,
                )
                return
            self._stop_face_observation("person follow owns motion")
            self._interrupt_dog_expression("person follow owns motion")
            self._stop_exploration("person follow owns motion", cancel_goal=False)
            self._stop_patrol("person follow owns motion")
            if (
                self._curious_follow_started
                and now - self._curious_follow_started >= self.config.follow_max_s
            ):
                self._stop_curious_follow()
            else:
                self._set_activity(BehaviorKind.FOLLOW, "person_follow", True, None)
            return
        if intent_active or (
            nav_state is not NavigationState.IDLE
            and not self._exploring
            and not self._patrolling
        ):
            self._stop_face_observation("explicit movement owns motion")
            self._interrupt_dog_expression("explicit movement owns motion")
            self._stop_exploration("explicit movement owns motion", cancel_goal=False)
            self._stop_patrol("explicit movement owns motion")
            self._set_activity(
                BehaviorKind.EXPLICIT_TASK,
                self._movement_intent_tool or "navigation",
                nav_state is not NavigationState.IDLE,
                None,
            )
            return

        self._refresh_map_phase(now)
        sleep_analysis = mode.mode is OperatingMode.SLEEP_ANALYSIS
        if not sleep_analysis:
            self._sleep_active_elapsed_s = 0.0
            self._modes().end_scan()
            self._stop_face_observation("autonomous mode")
        if getattr(self, "_attention_follow_until", 0.0):
            # The visual servo can finish early after reaching its stand-off
            # distance. Clear the ownership marker; the queued Hello below
            # then runs, after which normal exploration resumes.
            self._attention_follow_until = 0.0
        if sleep_analysis:
            # The scan cadence is wall-clock patrol time, not moving-only
            # ticks. Real patrol alternates short route legs with planner
            # waits (nav IDLE), so gating accrual on non-IDLE ticks made a
            # 25 s interval take minutes and the mode looked inert. This
            # point in the tick is only reached while autonomy owns the
            # body in Sleep Analysis, so counting every tick here is safe.
            self._sleep_active_elapsed_s = float(
                getattr(self, "_sleep_active_elapsed_s", 0.0)
            ) + tick_elapsed
            if self._maybe_sleep_scan(now, nav_state):
                return
            if self._maybe_eye_contact_response(now, nav_state):
                return
            if self._maybe_wave_response(now, nav_state):
                return
            if self._maybe_face_observation(now, nav_state):
                return
        # A close person may trigger a short curious follow at a natural IDLE
        # boundary. Never cancel an active frontier/patrol route for a social
        # reflex: that was the main follow/re-arm/replan feedback loop in the
        # July 24 stationary episodes.
        self._curious_follow_started = 0.0
        # Short-circuit on the flag so no observe_person RPC is made when curious
        # follow is disabled: the dog ignores nearby people while it maps/patrols.
        if (
            sleep_analysis
            and
            self.config.curious_follow_enabled
            and nav_state is NavigationState.IDLE
            and self._person_is_close(now)
        ):
            self._interrupt_dog_expression("close person")
            self._start_curious_follow()
            return

        if self._map_phase == "MAPPED":
            if sleep_analysis:
                if self._maybe_hand_expression(now, nav_state):
                    return
                if self._maybe_dog_expression(now, nav_state):
                    return
            self._ensure_patrolling(now)
            # A live patrol coroutine does not necessarily own a physical
            # route: the router can be waiting for a fresh occupancy update or
            # have no reachable candidate. Treating that wait as expected
            # motion made the stall watchdog blacklist the stationary robot
            # pose, shrinking the next candidate set until patrol could never
            # recover. Only a non-idle local planner is expected to move.
            moving_expected = (
                self._patrolling and nav_state is not NavigationState.IDLE
            )
            # A wedged patrol reads as "goal published, no path, body stuck".
            # Back out before falling through to the IDLE-only liveness turn,
            # which cannot help while the planner still thinks it is pathing.
            if moving_expected and self._maybe_escape_stall(now):
                return
            self._set_activity(
                BehaviorKind.PATROL,
                "curiosity",
                moving_expected,
                None,
            )
            self._enforce_liveness(now, nav_state)
            return

        # Discovery owns motion between dog moments. Scheduled expression
        # breaks pause frontier travel briefly so the robot keeps reading as a
        # curious dog while it maps; the explorer restarts on the next tick.
        if sleep_analysis:
            if self._maybe_hand_expression(now, nav_state):
                return
            if self._maybe_dog_expression(
                now,
                nav_state,
                allow_break=self.config.dog_expressions_while_exploring,
            ):
                return
        self._ensure_exploring(now)
        # The explorer can spend 10-20 s ranking a mature merged map before it
        # publishes a goal. During that CPU-bound interval navigation is IDLE
        # and the body is *supposed* to be stationary. Calling it motion here
        # made both watchdogs kill the explorer before its first post-lock goal,
        # then the restart repeated the same mistake forever.
        moving_expected = self._exploring and nav_state is not NavigationState.IDLE
        # Same wedged-in-clutter escape while exploring. Reached only after the
        # lease/follow/intent/hold/expression guards above returned, so it never
        # runs while another owner or a dog gesture holds the body.
        if moving_expected and self._maybe_escape_stall(now):
            return
        self._set_activity(BehaviorKind.EXPLORE, "curiosity", moving_expected, None)

        # The base-state invariant applies while a stopped explorer thread is
        # finishing frontier computation too. Keep doing bounded curiosity
        # turns every liveness window until a single new explorer can start.
        self._enforce_liveness(now, nav_state)

    def _enforce_liveness(self, now: float, nav_state: NavigationState) -> None:
        # A direct recovery turn is sent on tele_cmd_vel, whose safety mux
        # correctly cancels navigation. It must therefore never run while the
        # planner is rotating, following, or replanning; doing so caused the
        # former start/stop goal churn and made obstacle avoidance look broken.
        # The planner has its own stuck detector for this state.
        if nav_state is not NavigationState.IDLE:
            return
        # A failed handoff is not a reason to rotate forever at the last
        # frontier. The next tick must retry patrol or reopen exploration.
        if not self._exploring and not self._patrolling:
            # Leave MAPPED intact so the next tick retries patrol directly.
            # Flipping MAPPED -> EXPLORING here fought _refresh_map_phase at
            # 10 Hz, producing thousands of log/RPC events without acquiring
            # motion. _ensure_patrolling itself reopens exploration if patrol
            # is genuinely unavailable.
            self._retry_not_before = 0.0
            if (
                now
                - float(getattr(self, "_no_motion_owner_warned_at", 0.0))
                >= 2.0
            ):
                self._no_motion_owner_warned_at = now
                logger.warning(
                    "no autonomous motion owner; retrying current phase",
                    map_phase=self._map_phase,
                )
            return
        # Vectorized scoring and two-scan convergence are both sub-five-second
        # operations. Initial rotation changes navigation out of IDLE and is
        # handled above, so it does not consume this startup allowance.
        if (
            self._exploring
            and now - self._explore_requested_at
            < self.config.autonomy_start_grace_s
        ):
            return
        last_motion = self._last_motion_ts or self._motion_watch_started_at
        timeout = self.config.liveness_timeout_s
        if self._exploring:
            timeout = max(timeout, self.config.explore_liveness_timeout_s)
        elif self._patrolling:
            timeout = max(timeout, self.config.patrol_liveness_timeout_s)
        if now - last_motion >= timeout:
            self._recover_from_stall()

    def _return_home(
        self,
        now: float,
        nav_state: NavigationState,
        *,
        lie_down: bool = True,
    ) -> None:
        """Retrace persisted nearby waypoints to the saved launch origin."""
        reason = "low-battery return home" if lie_down else "operator return home"
        owner = "battery" if lie_down else "operator"
        self._interrupt_dog_expression(reason)
        self._stop_exploration(reason, cancel_goal=True)
        self._stop_patrol(reason)
        home = self._home_xy
        odom = self._last_odom
        if home is None or odom is None:
            self._publish_stop()
            self._set_activity(
                BehaviorKind.HOLD,
                owner,
                False,
                "BATTERY_LOW_HOME_UNKNOWN" if lie_down else "HOME_ODOMETRY_UNKNOWN",
            )
            return
        distance = math.hypot(
            float(odom.position.x) - home[0],
            float(odom.position.y) - home[1],
        )
        if distance <= self.config.return_home_radius_m:
            if not self._home_arrived:
                try:
                    self._navigation.cancel_goal()
                except Exception:
                    logger.exception("return-home arrival cancel failed")
                self._publish_stop()
                if lie_down:
                    try:
                        self._low_battery_liedown = bool(self._connection.liedown())
                    except Exception:
                        logger.exception("return-home lie-down failed")
                        self._low_battery_liedown = False
                self._home_arrived = True
                self._returning_home = False
                if not lie_down:
                    self._operator_return_home = False
                    self._explicit_hold_reason = "AT_BOOTH_HOME"
                logger.info(
                    "return home reached",
                    distance_m=round(distance, 2),
                    liedown=self._low_battery_liedown if lie_down else False,
                )
            self._set_activity(
                BehaviorKind.HOLD,
                owner,
                False,
                "BATTERY_HOME" if lie_down else "AT_BOOTH_HOME",
            )
            return
        if self._return_route_blocked:
            self._publish_stop()
            if lie_down and not self._low_battery_liedown:
                try:
                    self._low_battery_liedown = bool(self._connection.liedown())
                except Exception:
                    logger.exception("blocked return-home lie-down failed")
            if not lie_down:
                self._operator_return_home = False
                self._explicit_hold_reason = "RETURN_HOME_ROUTE_BLOCKED"
            self._set_activity(
                BehaviorKind.HOLD,
                owner,
                False,
                "RETURN_HOME_ROUTE_BLOCKED",
            )
            return
        if not self._sensors_ready(now):
            self._publish_stop()
            self._set_activity(
                BehaviorKind.HOLD,
                owner,
                False,
                "RETURN_HOME_SENSORS_NOT_READY",
            )
            return

        current = (float(odom.position.x), float(odom.position.y))
        target = self._next_return_waypoint(current)
        if target is None:
            # A state file from an older deployment may only have the home
            # coordinate. Preserve that fallback, but new runs always record
            # and persist the traversed route.
            target = home

        retry_due = now - self._home_goal_sent_at >= self.config.return_home_retry_s
        if not self._returning_home or (
            nav_state is NavigationState.IDLE and retry_due
        ):
            if (
                self._returning_home
                and nav_state is NavigationState.IDLE
                and self._return_waypoint_xy is not None
                and math.hypot(
                    current[0] - self._return_waypoint_xy[0],
                    current[1] - self._return_waypoint_xy[1],
                )
                > self.config.return_home_waypoint_radius_m
            ):
                self._return_waypoint_retries += 1
            else:
                self._return_waypoint_retries = 0

            if (
                self._return_waypoint_retries
                >= self.config.return_home_max_waypoint_retries
            ):
                self._returning_home = False
                self._return_route_blocked = True
                self._publish_stop()
                if lie_down:
                    try:
                        self._low_battery_liedown = bool(self._connection.liedown())
                    except Exception:
                        logger.exception("blocked return-home lie-down failed")
                else:
                    self._operator_return_home = False
                    self._explicit_hold_reason = "RETURN_HOME_ROUTE_BLOCKED"
                logger.error(
                    "return-home route blocked",
                    waypoint=tuple(round(value, 2) for value in target),
                    retries=self._return_waypoint_retries,
                    soc=self._battery_soc,
                )
                self._set_activity(
                    BehaviorKind.HOLD,
                    owner,
                    False,
                    "RETURN_HOME_ROUTE_BLOCKED",
                )
                return

            heading = math.atan2(
                target[1] - current[1],
                target[0] - current[0],
            )
            goal = PoseStamped(
                ts=time.time(),
                frame_id="map",
                position=Vector3(target[0], target[1], 0.0),
                orientation=Quaternion.from_euler(Vector3(0.0, 0.0, heading)),
            )
            try:
                ensure_motion_ready(self._connection)
                accepted = bool(self._navigation.set_goal(goal))
            except Exception:
                logger.exception("return-home goal failed")
                accepted = False
            self._home_goal_sent_at = now
            self._returning_home = accepted
            if not accepted:
                self._return_waypoint_retries += 1
            logger.info(
                "return-home goal",
                accepted=accepted,
                home=(round(home[0], 2), round(home[1], 2)),
                waypoint=(round(target[0], 2), round(target[1], 2)),
                waypoint_index=self._return_waypoint_index,
                distance_m=round(distance, 2),
                soc=self._battery_soc,
            )
        self._set_activity(
            BehaviorKind.RETURN_HOME,
            owner,
            self._returning_home,
            None if self._returning_home else "RETURN_HOME_RETRYING",
        )

    def _next_return_waypoint(
        self, current: tuple[float, float]
    ) -> tuple[float, float] | None:
        """Choose the next recorded pose toward home, never a far-map jump."""
        breadcrumbs = self._breadcrumbs
        if not breadcrumbs:
            self._return_waypoint_xy = None
            return None

        if self._return_waypoint_xy is not None:
            if (
                math.hypot(
                    current[0] - self._return_waypoint_xy[0],
                    current[1] - self._return_waypoint_xy[1],
                )
                > self.config.return_home_waypoint_radius_m
            ):
                return self._return_waypoint_xy
            self._return_waypoint_xy = None
            self._return_waypoint_retries = 0

        if self._return_waypoint_index is None:
            self._return_waypoint_index = min(
                range(len(breadcrumbs)),
                key=lambda index: math.hypot(
                    current[0] - breadcrumbs[index][0],
                    current[1] - breadcrumbs[index][1],
                ),
            )

        index = self._return_waypoint_index
        radius = self.config.return_home_waypoint_radius_m
        while index > 0:
            waypoint = breadcrumbs[index]
            if math.hypot(current[0] - waypoint[0], current[1] - waypoint[1]) > radius:
                break
            index -= 1

        self._return_waypoint_index = index
        raw_target = breadcrumbs[index]
        dx = raw_target[0] - current[0]
        dy = raw_target[1] - current[1]
        distance = math.hypot(dx, dy)
        max_step = self.config.return_home_waypoint_max_m
        if distance > max_step and distance > 0.0:
            scale = max_step / distance
            target = (current[0] + dx * scale, current[1] + dy * scale)
        else:
            target = raw_target

        self._return_waypoint_xy = target
        return target

    def _hold(self, reason: str) -> None:
        self._stop_face_observation(reason)
        self._interrupt_dog_expression(reason)
        self._stop_exploration(reason, cancel_goal=True)
        self._stop_patrol(reason)
        if reason == "BATTERY_LOW" and not self._low_battery_liedown:
            # A standing Go2 continued draining from 10% to 4% while motion was
            # correctly held. Low-battery safety must reduce posture load too.
            try:
                self._low_battery_liedown = bool(self._connection.liedown())
                logger.info(
                    "curiosity low-battery lie-down",
                    success=self._low_battery_liedown,
                )
            except Exception:
                logger.exception("curiosity low-battery lie-down failed")
        if reason == "OPERATOR_LYING_DOWN" and not self._operator_liedown:
            try:
                self._operator_liedown = bool(self._connection.liedown())
                logger.info(
                    "operator lie-down",
                    success=self._operator_liedown,
                )
            except Exception:
                logger.exception("operator lie-down failed")
        now = time.monotonic()
        if self._is_following() and now - self._follow_stop_requested_at >= 2.0:
            self._follow_stop_requested_at = now
            try:
                self._agent_spec.dispatch_continuation(
                    {"tool": "stop_following", "args": {}},
                    {"_silent": True, "label": reason},
                )
            except Exception:
                logger.exception("curiosity safety follow stop failed", reason=reason)
        self._curious_follow_started = 0.0
        self._publish_stop()
        self._set_activity(BehaviorKind.HOLD, "safety", False, reason)

    def _sensors_ready(self, now: float) -> bool:
        # The Go2 voxel-map/costmap is a latched world model, not a periodic
        # heartbeat. In a static scene the firmware legitimately publishes no
        # replacement snapshot, so requiring a <4 s costmap timestamp creates
        # a deadlock: curiosity cannot move until the map changes, and the map
        # does not change until curiosity moves. Fresh odometry remains the
        # liveness gate; new lidar snapshots replace overlapping voxels as the
        # robot moves.
        with self._lock:
            return bool(
                self._last_odom_at
                and self._last_costmap_at
                and now - self._last_odom_at <= self.config.sensor_stale_s
            )

    def _refresh_battery(self, now: float) -> None:
        if now - self._battery_checked_at < self.config.battery_check_s:
            return
        self._battery_checked_at = now
        try:
            soc = self._connection.battery_soc()
        except Exception:
            logger.exception("curiosity battery query failed")
            soc = None
        if soc != self._battery_soc:
            logger.info("curiosity battery", soc=soc)
        self._battery_soc = soc
        if soc is not None and soc > self.config.min_battery_soc:
            self._low_battery_liedown = False
            self._returning_home = False
            self._return_waypoint_index = None
            self._return_waypoint_xy = None
            self._return_waypoint_retries = 0
            self._return_route_blocked = False

    def _navigation_state(self) -> NavigationState:
        try:
            state = self._navigation.get_state()
            return state if isinstance(state, NavigationState) else NavigationState.IDLE
        except Exception:
            logger.exception("curiosity navigation state query failed")
            return NavigationState.IDLE

    def _is_following(self) -> bool:
        try:
            return bool(self._follow.is_following())
        except Exception:
            logger.exception("curiosity follow state query failed")
            return False

    def _ensure_exploring(self, now: float) -> None:
        was_exploring = self._exploring
        try:
            active = bool(self._explore.is_exploration_active())
        except Exception:
            logger.exception("curiosity exploration state query failed")
            active = False
        self._exploring = active
        if not active:
            self._exploration_capability_bound = False
            # NO_FRONTIERS can be proven between the once-per-second map-phase
            # polls. Restarting here would clear that completion reason before
            # _refresh_map_phase observes it, producing an endless idle cycle.
            try:
                completion = self._explore.exploration_status()
            except Exception:
                logger.exception("curiosity completion status query failed")
                completion = {}
            if completion.get("map_complete") is True:
                if self._map_phase != "MAPPED":
                    logger.info(
                        "curiosity observed map completion before restart",
                        reason=completion.get("completion_reason"),
                        explored_goals=completion.get("explored_goals"),
                    )
                    self._save_map_state()
                self._map_phase = "MAPPED"
                self._prior_session_mapped = False
                return
        if now < self._retry_not_before:
            return
        if active and self._exploration_capability_bound:
            return
        if now - self._explore_requested_at < self.config.exploration_retry_s:
            return
        dispatched = False
        try:
            # Re-arm the firmware only when there is no active explorer.  The
            # MCP capability can become available after direct bootstrapping,
            # while the dog is already following its first frontier.  Repeating
            # StandUp/RecoveryStand/BalanceStand merely to bind that capability
            # interrupts the active controller and, on a degraded WebRTC peer,
            # used to block this supervisor for the full 120 s RPC timeout.
            if not active:
                if not ensure_motion_ready(self._connection):
                    # A failed StandReady used to be logged and then ignored:
                    # the planner produced perfectly valid velocity commands
                    # while the firmware silently discarded all of them. Do
                    # not manufacture a "stuck navigation" incident from an
                    # actuator that never armed; retry the bounded re-arm first.
                    self._exploring = False
                    self._retry_not_before = (
                        now + self.config.exploration_retry_s
                    )
                    logger.warning(
                        "exploration deferred until robot motion is armed"
                    )
                    return
            dispatched = bool(
                self._agent_spec.dispatch_continuation(
                    {"tool": "begin_exploration", "args": {}},
                    {"_silent": True, "label": "curiosity"},
                )
            )
            self._explore_requested_at = now
            self._exploration_capability_bound = dispatched
            if dispatched:
                logger.info("curiosity exploration capability acquired")
        except Exception:
            logger.exception("curiosity exploration dispatch failed")
        # The embedded MCP client discovers its tools only after every module
        # has started (vision warmup can take >20 s). Start the same explorer
        # directly during that boot window, then retry begin_exploration while
        # active to bind its background movement capability as soon as MCP is
        # ready. User tools are not available before that discovery completes.
        if not active and not dispatched:
            try:
                active = bool(self._explore.explore())
                self._exploring = active
                if active:
                    logger.info(
                        "curiosity exploration bootstrapped before MCP discovery"
                    )
            except Exception:
                logger.exception("curiosity exploration bootstrap failed")
        if self._exploring or dispatched:
            if not self._explore_started_once:
                self._explore_started_once = True
                # The first play break comes one full explore-cadence after
                # mapping begins, not 20 s after process start.
                self._dog_expression_next_at = now + random.uniform(
                    self.config.dog_expression_explore_min_interval_s,
                    self.config.dog_expression_explore_max_interval_s,
                )
            # A failed MCP bind is retried while the directly bootstrapped
            # explorer remains active.  That retry is not a new movement
            # epoch: repeatedly resetting watchdogs here hid real stalls.
            if not was_exploring:
                self._reset_motion_watch(now)
        else:
            self._retry_not_before = now + self.config.exploration_retry_s

    def _load_home_state(self) -> tuple[float, float] | None:
        """Load a recent launch-origin position for process restarts."""
        try:
            path = Path(self.config.home_state_path)
            if not path.exists():
                return None
            data = json.loads(path.read_text())
            age = time.time() - float(data.get("saved_at", 0.0))
            if age < 0.0 or age > self.config.home_max_age_s:
                logger.info("saved low-battery home expired", age_s=round(age))
                return None
            home = (float(data["x"]), float(data["y"]))
            logger.info(
                "saved low-battery home loaded",
                x=round(home[0], 2),
                y=round(home[1], 2),
                age_s=round(age),
            )
            return home
        except Exception:
            logger.exception("low-battery home load failed")
            return None

    def _save_home_state(self) -> None:
        home = self._home_xy
        if home is None:
            return
        try:
            path = Path(self.config.home_state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "x": home[0],
                        "y": home[1],
                        "saved_at": time.time(),
                    }
                )
            )
            tmp.replace(path)
        except Exception:
            logger.exception("low-battery home save failed")

    def _record_breadcrumb_locked(self, msg: PoseStamped, now: float) -> None:
        """Append coarse route geometry while normal autonomy is travelling."""
        if (
            self._battery_soc is None
            or self._battery_soc <= self.config.min_battery_soc
            or self._returning_home
            or self._explicit_hold_reason
        ):
            return
        point = (float(msg.position.x), float(msg.position.y))
        if not self._breadcrumbs:
            self._breadcrumbs.append(self._home_xy or point)
        previous = self._breadcrumbs[-1]
        if (
            math.hypot(point[0] - previous[0], point[1] - previous[1])
            < self.config.breadcrumb_spacing_m
        ):
            return
        self._breadcrumbs.append(point)
        if len(self._breadcrumbs) > self.config.breadcrumb_max_points:
            # Keep the launch origin and the newest route. This is intentionally
            # a very high cap (roughly 3 km at default spacing).
            self._breadcrumbs = [
                self._breadcrumbs[0],
                *self._breadcrumbs[-(self.config.breadcrumb_max_points - 1) :],
            ]
        if now - self._last_breadcrumb_save_at >= self.config.breadcrumb_save_s:
            self._last_breadcrumb_save_at = now
            self._save_breadcrumb_state()

    def _load_breadcrumb_state(self) -> list[tuple[float, float]]:
        """Load the recent traversed route used for cross-process homing."""
        try:
            path = Path(self.config.breadcrumb_state_path)
            if not path.exists():
                return [self._home_xy] if self._home_xy is not None else []
            data = json.loads(path.read_text())
            age = time.time() - float(data.get("saved_at", 0.0))
            if age < 0.0 or age > self.config.home_max_age_s:
                logger.info("saved return-home route expired", age_s=round(age))
                return [self._home_xy] if self._home_xy is not None else []
            points = [
                (float(point[0]), float(point[1]))
                for point in data.get("points", [])
                if isinstance(point, list) and len(point) >= 2
            ]
            if self._home_xy is not None and (
                not points
                or math.hypot(
                    points[0][0] - self._home_xy[0],
                    points[0][1] - self._home_xy[1],
                )
                > self.config.return_home_radius_m
            ):
                points.insert(0, self._home_xy)
            logger.info(
                "saved return-home route loaded",
                points=len(points),
                age_s=round(age),
            )
            if len(points) > self.config.breadcrumb_max_points:
                points = [
                    points[0],
                    *points[-(self.config.breadcrumb_max_points - 1) :],
                ]
            return points
        except Exception:
            logger.exception("return-home route load failed")
            return [self._home_xy] if self._home_xy is not None else []

    def _save_breadcrumb_state(self) -> None:
        if not self._breadcrumbs:
            return
        try:
            path = Path(self.config.breadcrumb_state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "saved_at": time.time(),
                        "home": list(self._home_xy)
                        if self._home_xy is not None
                        else None,
                        "points": [list(point) for point in self._breadcrumbs],
                    },
                    separators=(",", ":"),
                )
            )
            tmp.replace(path)
        except Exception:
            logger.exception("return-home route save failed")

    def _load_operator_settings(self) -> dict[str, Any]:
        """Load venue-scoped operator settings without making startup fatal."""
        try:
            path = Path(self.config.operator_settings_path)
            if not path.exists():
                return {}
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.exception("operator settings load failed")
            return {}

    def _save_operator_settings(self) -> None:
        """Atomically persist only settings owned by this supervisor."""
        try:
            path = Path(self.config.operator_settings_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "sleep_scan_interval_s": self._sleep_scan_interval_s,
                        "sleep_scan_duration_s": self._sleep_scan_duration_s,
                        "saved_at": time.time(),
                    },
                    separators=(",", ":"),
                )
            )
            tmp.replace(path)
        except Exception:
            logger.exception("operator settings save failed")

    def _load_map_state(self) -> bool:
        """Return True when a prior session recorded this floor as MAPPED."""
        try:
            path = Path(self.config.map_state_path)
            if not path.exists():
                return False
            data = json.loads(path.read_text())
            mapped = data.get("map_phase") == "MAPPED"
            trusted = bool(
                mapped and getattr(self.config, "trust_persisted_map_complete", False)
            )
            if trusted:
                logger.info(
                    "prior session mapped this floor; will fast-start on "
                    "premap relocalization",
                    saved_at=data.get("saved_at"),
                )
            elif mapped:
                logger.info(
                    "persisted MAPPED state is advisory; revalidating coverage "
                    "this session",
                    saved_at=data.get("saved_at"),
                )
            return trusted
        except Exception:
            logger.exception("curiosity map state load failed")
            return False

    def _save_map_state(self) -> None:
        try:
            path = Path(self.config.map_state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"map_phase": "MAPPED", "saved_at": time.time()}))
            tmp.replace(path)
        except Exception:
            logger.exception("curiosity map state save failed")

    def _fast_start_from_premap(self) -> bool:
        """Skip re-exploration once ICP has aligned the saved premap.

        The merged premap already covers the floor, so waiting for frontier
        convergence would just re-walk known space. If relocalization is not
        configured or gives up, the flag clears and normal exploration keeps
        ownership. The patrol-exhausted reopen below remains the safety valve
        for a premap that turns out incomplete.
        """
        if not getattr(self, "_prior_session_mapped", False):
            return False
        if self._map_phase != "EXPLORING":
            return False
        try:
            reloc = self._reloc.relocalization_status()
        except Exception:
            logger.exception("curiosity relocalization status query failed")
            return False
        if reloc.get("locked"):
            logger.info(
                "premap relocalized; resuming curious patrol without re-exploration"
            )
            self._prior_session_mapped = False
            self._stop_exploration("premap relocalized", cancel_goal=True)
            self._map_phase = "MAPPED"
            self._save_map_state()
            return True
        if not reloc.get("configured") or reloc.get("exhausted"):
            logger.info(
                "premap relocalization unavailable; exploring normally",
                configured=reloc.get("configured"),
                exhausted=reloc.get("exhausted"),
            )
            self._prior_session_mapped = False
        return False

    def _refresh_map_phase(self, now: float) -> None:
        if now - self._last_exploration_status_at < 1.0:
            return
        self._last_exploration_status_at = now
        if self._fast_start_from_premap():
            return
        try:
            status = self._explore.exploration_status()
        except Exception:
            logger.exception("curiosity exploration status query failed")
            return
        reason = status.get("completion_reason")
        if status.get("map_complete") is True:
            if self._map_phase != "MAPPED":
                logger.info(
                    "curiosity map phase complete; switching to coverage patrol",
                    reason=reason,
                    explored_goals=status.get("explored_goals"),
                )
                self._save_map_state()
            self._map_phase = "MAPPED"
            self._exploring = False
            self._prior_session_mapped = False
        elif status.get("active"):
            self._map_phase = "EXPLORING"
        elif self._map_phase == "MAPPED":
            try:
                patrol = self._patrol.patrol_status()
            except Exception:
                logger.exception("curiosity patrol status query failed")
                patrol = {}
            if float(patrol.get("no_goal_for_s", 0.0)) >= 8.0:
                logger.warning(
                    "coverage patrol exhausted locally; reopening frontier exploration",
                    saturation=patrol.get("saturation"),
                )
                self._stop_patrol("coverage has no reachable local goal")
                self._map_phase = "EXPLORING"
                self._retry_not_before = now

    def _ensure_patrolling(self, now: float) -> None:
        try:
            active = bool(self._patrol.is_patrolling())
        except Exception:
            logger.exception("curiosity patrol state query failed")
            active = False
        self._patrolling = active
        if active or now < self._retry_not_before:
            return
        if now - self._patrol_requested_at < self.config.exploration_retry_s:
            return
        self._patrol_requested_at = now
        dispatched = False
        try:
            ensure_motion_ready(self._connection)
            # Curiosity is a system behavior, so its explorer-to-patrol handoff
            # must not depend on the optional MCP registry. The July 24 failure
            # left that registry empty and `start_patrol` returned "Tool not
            # found" forever while the dog rotated beside the window.
            dispatched = bool(self._patrol.ensure_patrolling())
        except Exception:
            logger.exception("direct curiosity patrol start failed")
        self._patrolling = dispatched
        if dispatched:
            self._reset_motion_watch(now)
            logger.info("curiosity coverage patrol started")
        else:
            # Do not sit in MAPPED with no owner. Reopen frontier exploration
            # immediately; a later no-frontier convergence will retry patrol.
            self._map_phase = "EXPLORING"
            self._retry_not_before = now
            logger.warning("patrol unavailable; reopening frontier exploration")

    def _stop_exploration(self, reason: str, *, cancel_goal: bool) -> None:
        was_exploring = self._exploring
        try:
            active = bool(self._explore.is_exploration_active())
        except Exception:
            active = was_exploring
        if active:
            try:
                self._explore.stop_exploration()
            except Exception:
                logger.exception("curiosity exploration stop failed", reason=reason)
        self._exploring = False
        self._exploration_capability_bound = False
        if cancel_goal and (active or was_exploring):
            try:
                self._navigation.cancel_goal()
            except Exception:
                logger.exception("curiosity navigation cancel failed", reason=reason)
        if active or was_exploring:
            logger.info("curiosity exploration yielded", reason=reason)

    def _stop_patrol(self, reason: str) -> None:
        was_patrolling = self._patrolling
        try:
            active = bool(self._patrol.is_patrolling())
        except Exception:
            active = was_patrolling
        if not active and not was_patrolling:
            return
        try:
            self._patrol.stop_patrolling()
        except Exception:
            logger.exception("direct curiosity patrol stop failed", reason=reason)
        self._patrolling = False
        logger.info("curiosity coverage patrol yielded", reason=reason)

    def _recover_from_stall(self) -> None:
        logger.warning(
            "curiosity liveness timeout; restarting autonomous planner",
            exploring=self._exploring,
            patrolling=self._patrolling,
        )
        was_patrolling = self._patrolling
        self._stop_exploration("liveness recovery", cancel_goal=True)
        self._stop_patrol("liveness recovery")
        self._publish_stop()
        now = time.monotonic()
        self._last_motion_ts = now
        self._stall_anchor = None
        self._map_phase = "EXPLORING" if was_patrolling else self._map_phase
        self._retry_not_before = now + 0.25

    def _maybe_escape_stall(self, now: float) -> bool:
        """Displacement-based stuck escape; True when it acted this tick.

        Unlike the IDLE-only liveness turn, this fires while navigation still
        looks active (planner publishing far goals, then "No path found") but
        the body has not moved, which is the clutter-nest failure: lidar barely
        sees soft blankets/backpacks, so the dog walks in and stays. A short
        reverse backs it out. The caller has already ensured curiosity owns
        motion and no lease/follow/intent/hold/expression is active.
        """
        odom = self._last_odom
        if odom is None:
            return False
        x, y = float(odom.position.x), float(odom.position.y)
        anchor = self._stall_anchor
        if anchor is None:
            self._stall_anchor = (now, x, y)
            return False
        if math.hypot(x - anchor[1], y - anchor[2]) >= self.config.stall_distance_m:
            # Real progress: start a fresh window from here.
            self._stall_anchor = (now, x, y)
            # Escape limits are for consecutive failed recoveries, not for all
            # recoveries in a five-minute period. Once the body has actually
            # cleared the trap, a later obstacle starts a fresh recovery epoch.
            self._escape_events.clear()
            self._escape_help_spoken = False
            return False
        if now - anchor[0] < self.config.stall_window_s:
            # Barely moving, but not yet for a full window.
            return False
        # A full window with under stall_distance_m of travel: one stall.
        self._stall_anchor = (now, x, y)
        pair_cutoff = now - self.config.stall_pair_window_s
        while self._stall_events and self._stall_events[0] < pair_cutoff:
            self._stall_events.popleft()
        self._stall_events.append(now)
        if len(self._stall_events) < 2:
            return False
        # Second stall inside the pair window. Reset so the next escape needs
        # two fresh stalls, then escape unless the per-window cap is reached.
        self._stall_events.clear()
        esc_cutoff = now - self.config.escape_cap_window_s
        while self._escape_events and self._escape_events[0] < esc_cutoff:
            self._escape_events.popleft()
        if len(self._escape_events) >= self.config.escape_max_per_window:
            self._escape_give_up(now)
            return True
        self._escape_events.append(now)
        self._escape_help_spoken = False
        self._run_escape(now)
        return True

    def _reset_motion_watch(self, now: float) -> None:
        """Start a clean watchdog epoch for a newly acquired motion owner."""
        self._motion_watch_started_at = now
        self._last_motion_ts = None
        self._stall_anchor = None
        # A stall observed under the previous owner cannot be paired with one
        # under the new owner to trigger an immediate false recovery.
        stall_events = getattr(self, "_stall_events", None)
        if stall_events is None:
            self._stall_events = deque()
        else:
            stall_events.clear()
        with self._lock:
            self._motion_anchor = self._last_odom

    def _run_escape(self, now: float) -> None:
        odom = self._last_odom
        position = (
            (round(float(odom.position.x), 2), round(float(odom.position.y), 2))
            if odom is not None
            else None
        )
        # Ask a blocker to make way before backing up, but only when curious
        # follow is enabled (disabled means the dog ignores people and makes no
        # observe_person call at all).
        if self.config.curious_follow_enabled and self._person_currently_close():
            self._speak_line(self.config.make_way_text)
        logger.info(
            "stuck recovery: replanning away from failure zone", position=position
        )
        self._stop_exploration("stuck escape", cancel_goal=True)
        self._stop_patrol("stuck escape")
        self._publish_stop()
        # Replanning from the exact same wedged pose is not an escape. It was
        # the cause of the chair-trap loop: three different global paths were
        # valid on paper, but every one began with the body still touching the
        # same clutter. Reverse only when a 360-degree lidar costmap proves the
        # complete swept body corridor behind the dog is known and traversable.
        # If glass or an unseen rear obstacle makes that proof impossible, keep
        # the conservative stop-only behavior.
        reversed_safely = False
        if self._reverse_escape_is_clear():
            try:
                ensure_motion_ready(self._connection)
                twist = Twist(
                    linear=Vector3(-self.config.escape_reverse_speed_mps, 0.0, 0.0),
                    angular=Vector3(0.0, 0.0, 0.0),
                )
                period = 1.0 / max(1.0, self.config.escape_hz)
                deadline = time.monotonic() + self.config.escape_reverse_s
                while time.monotonic() < deadline and not self._stop_event.is_set():
                    self._publish_twist(twist)
                    self._stop_event.wait(period)
                reversed_safely = True
            except Exception:
                logger.exception("costmap-guarded reverse escape failed")
            finally:
                self._publish_stop()
        logger.info(
            "stuck recovery physical escape",
            reversed=reversed_safely,
            duration_s=self.config.escape_reverse_s if reversed_safely else 0.0,
        )
        end = time.monotonic()
        self._last_motion_ts = end
        self._stall_anchor = None
        self._retry_not_before = end + 0.5

    def _reverse_escape_is_clear(self) -> bool:
        """Prove the rear swept footprint is known and below lethal cost."""
        odom = self._last_odom
        costmap = getattr(self, "_latest_costmap", None)
        if odom is None or costmap is None or costmap.grid.size == 0:
            return False

        yaw = float(odom.orientation.euler.z)
        forward_x, forward_y = math.cos(yaw), math.sin(yaw)
        left_x, left_y = -forward_y, forward_x
        reverse_distance = (
            self.config.escape_reverse_speed_mps * self.config.escape_reverse_s
        )
        # The deployed footprint is 42 cm wide. Add 8 cm total margin and
        # sample the full rectangular swept corridor at costmap resolution.
        half_width = 0.25
        step = max(0.04, float(costmap.resolution))
        longitudinal = np.arange(0.05, reverse_distance + 0.12, step)
        lateral = np.arange(-half_width, half_width + step, step)
        for back in longitudinal:
            for side in lateral:
                wx = (
                    float(odom.position.x)
                    - forward_x * float(back)
                    + left_x * float(side)
                )
                wy = (
                    float(odom.position.y)
                    - forward_y * float(back)
                    + left_y * float(side)
                )
                grid_point = costmap.world_to_grid(Vector3(wx, wy, 0.0))
                gx, gy = int(grid_point.x), int(grid_point.y)
                if not (0 <= gx < costmap.width and 0 <= gy < costmap.height):
                    return False
                value = int(costmap.grid[gy, gx])
                if value == CostValues.UNKNOWN or value >= 50:
                    return False
        return True

    def _escape_give_up(self, now: float) -> None:
        logger.warning(
            "stuck escape cap reached; quarantining region and rerouting",
            escapes=len(self._escape_events),
            window_s=self.config.escape_cap_window_s,
        )
        self._stop_exploration("stuck escape cap", cancel_goal=True)
        self._stop_patrol("stuck escape cap")
        self._publish_stop()
        # Never persist a keep-out centred on the robot. That makes the next
        # startup begin inside its own forbidden circle and can remove every
        # valid first step. Proven route obstacles are learned separately from
        # the local planner's measured blocking point; here we only reset the
        # recovery window and ask the outward-progress planner for a new route.
        self._escape_events.clear()
        self._stall_events.clear()
        self._stall_anchor = None
        self._last_motion_ts = now
        self._map_phase = "EXPLORING"
        self._retry_not_before = now + 0.5
        self._set_activity(BehaviorKind.EXPLORE, "curiosity", False, None)

    def _person_currently_close(self) -> bool:
        try:
            observation = self._follow.observe_person(self.config.person_close_frac)
        except Exception:
            logger.exception("curiosity escape person check failed")
            return False
        return bool(observation)

    def _speak_line(self, text: str) -> None:
        speaker = getattr(self, "_speak", None)
        if speaker is None or not text:
            return
        try:
            speaker.speak(text)
        except Exception:
            logger.exception("curiosity speech failed")

    def _maybe_hand_expression(self, now: float, nav_state: NavigationState) -> bool:
        """Run a queued hand-cover reaction only between navigation legs."""
        if getattr(self, "_dog_expression_name", None) is not None:
            return self._maybe_dog_expression(now, nav_state)
        pending_at = getattr(self, "_hand_gesture_pending_at", 0.0)
        if not pending_at:
            return False
        if now - pending_at > self.config.hand_gesture_max_wait_s:
            self._hand_gesture_pending_at = 0.0
            logger.info("queued hand-cover gesture expired before navigation idle")
            return False
        # Critical invariant: never cancel or compete with an active planner
        # path. The request waits for the natural IDLE gap between frontier
        # legs; _start_dog_expression stops the explorer without cancelling a
        # goal, preserving all visit/failure/coverage history.
        if nav_state is not NavigationState.IDLE:
            return False
        choices = [
            expression
            for expression in _DOG_EXPRESSIONS
            if expression[0] != getattr(self, "_last_dog_expression", None)
        ] or list(_DOG_EXPRESSIONS)
        name, duration_s, _weight = random.choices(
            choices,
            weights=[item[2] for item in choices],
            k=1,
        )[0]
        # IDLE can be a short planner replan transition rather than a true
        # between-leg gap. Cancel any just-published goal and stop the body
        # before handing control to firmware, closing the race that previously
        # interrupted both the gesture and the resumed exploration.
        self._stop_exploration("hand-cover expression", cancel_goal=True)
        self._publish_stop()
        started = self._start_dog_expression(name, duration_s, now)
        if started:
            self._hand_gesture_pending_at = 0.0
            logger.info(
                "queued hand-cover gesture started between navigation legs",
                expression=name,
            )
        return started

    def _maybe_dog_expression(
        self, now: float, nav_state: NavigationState, allow_break: bool = False
    ) -> bool:
        """Run one calm, firmware-native dog gesture.

        Sport routines temporarily own the body controller, so they never run
        while a follow, explicit task, lease, or safety hold owns the robot.
        In idle mode (patrol) they additionally wait for genuine idle time.
        ``allow_break`` permits a scheduled mapping gesture only during a
        natural IDLE boundary. It never interrupts frontier travel. The
        supervisor waits for the bounded routine, re-arms locomotion, and
        immediately resumes curiosity.
        """
        active = self._dog_expression_name
        if active is not None:
            if now < self._dog_expression_deadline:
                self._set_activity(
                    BehaviorKind.EXPRESS, f"curiosity:{active}", False, None
                )
                return True
            self._dog_expression_name = None
            self._dog_expression_deadline = 0.0
            ensure_motion_ready(self._connection, force=True)
            self._retry_not_before = now + 0.5
            self._motion_watch_started_at = now
            self._last_motion_ts = now
            logger.info("curiosity dog expression finished", expression=active)
            return False

        if not self.config.dog_expressions or now < self._dog_expression_next_at:
            return False
        # Play breaks belong BETWEEN exploration legs. Both July 24 silent
        # boot collapses fired a gesture mid-startup, while GO2Connection was
        # still configuring the firmware; never run one before the first
        # exploration leg has begun.
        if allow_break and (
            not self._explore_started_once
            or nav_state is not NavigationState.IDLE
        ):
            return False
        if not allow_break:
            if (
                nav_state is not NavigationState.IDLE
                or self._exploring
                or self._patrolling
            ):
                return False
            last_motion = self._last_motion_ts or self._motion_watch_started_at
            if now - last_motion < self.config.dog_expression_after_s:
                return False

        choices = [
            expression
            for expression in _DOG_EXPRESSIONS
            if expression[0] != self._last_dog_expression
        ] or list(_DOG_EXPRESSIONS)
        name, duration_s, _weight = random.choices(
            choices,
            weights=[item[2] for item in choices],
            k=1,
        )[0]

        if allow_break:
            interval_min = max(0.0, self.config.dog_expression_explore_min_interval_s)
            interval_max = max(
                interval_min, self.config.dog_expression_explore_max_interval_s
            )
        else:
            interval_min = max(0.0, self.config.dog_expression_min_interval_s)
            interval_max = max(interval_min, self.config.dog_expression_max_interval_s)
        self._dog_expression_next_at = now + random.uniform(interval_min, interval_max)
        if allow_break:
            self._stop_exploration(
                "dog expression at route boundary",
                cancel_goal=True,
            )
            self._publish_stop()
        return self._start_dog_expression(name, duration_s, now)

    def _start_dog_expression(self, name: str, duration_s: float, now: float) -> bool:
        self._stop_exploration("dog expression", cancel_goal=False)
        self._stop_patrol("dog expression")
        try:
            # Hello was the booth's least reliable action because the former
            # generic path did not verify the firmware acknowledgement or
            # restore the required standing FSM. Use the hardened routine.
            if name == "Hello":
                started = wave_hello(self._connection)
            else:
                started = bool(
                    ensure_motion_ready(self._connection)
                    and self._connection.sport_command(SPORT_CMD[name])
                )
        except Exception:
            logger.exception("curiosity dog expression failed", expression=name)
            started = False
        if not started:
            # A rejected/timed-out firmware command must not leave autonomy
            # behind the normal expression cadence or retry delay. The
            # explorer/patrol was yielded optimistically above; let the very
            # next supervisor tick reacquire it.
            self._retry_not_before = 0.0
            return False

        self._dog_expression_name = name
        self._last_dog_expression = name
        self._dog_expression_deadline = now + duration_s
        self._dog_expression_next_at = max(
            self._dog_expression_next_at,
            now + max(0.0, self.config.dog_expression_min_interval_s),
        )
        self._set_activity(BehaviorKind.EXPRESS, f"curiosity:{name}", False, None)
        logger.info(
            "curiosity dog expression started",
            expression=name,
            duration_s=duration_s,
        )
        return True

    def _maybe_wave_response(
        self, now: float, nav_state: NavigationState
    ) -> bool:
        """Queue one wave-back and perform it only between navigation legs.

        A visitor interaction must never cancel a frontier route. Pose events
        are therefore latched while the planner is moving and either run at
        the next natural IDLE gap or expire. A global cooldown is intentional:
        tracker IDs can change as the robot turns, so a per-track cooldown
        alone cannot prevent the same raised arm from retriggering.
        """
        if not self.config.wave_response_enabled:
            self._wave_response_pending = None
            self._wave_response_pending_at = 0.0
            return False

        if (
            now - float(getattr(self, "_wave_response_last_poll", 0.0))
            >= self.config.wave_response_poll_s
        ):
            self._wave_response_last_poll = now
            observation = None
            try:
                observation = self._follow.observe_waving_person(
                    self.config.wave_response_min_height_frac
                )
            except Exception:
                logger.exception("wave-response pose observation failed")
            if (
                observation
                and getattr(self, "_wave_response_pending", None) is None
                and now
                >= float(getattr(self, "_wave_response_global_until", 0.0))
            ):
                track_id = int(observation.get("track_id", -1))
                person_id = observation.get("person_id")
                subject_key = (
                    f"person:{person_id}" if person_id else f"track:{track_id}"
                )
                self._wave_response_pending = dict(observation)
                self._wave_response_pending_at = now
                self._wave_response_global_until = (
                    now + self.config.wave_response_cooldown_s
                )
                self._wave_response_cooldowns[subject_key] = (
                    now + self.config.wave_response_cooldown_s
                )
                logger.info(
                    "wave response queued without interrupting navigation",
                    subject=subject_key,
                    side=observation.get("raised_hand_side"),
                )

        pending = getattr(self, "_wave_response_pending", None)
        pending_at = float(getattr(self, "_wave_response_pending_at", 0.0))
        if pending is None:
            return False
        if now - pending_at > self.config.wave_response_max_wait_s:
            self._wave_response_pending = None
            self._wave_response_pending_at = 0.0
            logger.info("queued wave response expired while navigation stayed busy")
            return False
        if (
            nav_state is not NavigationState.IDLE
            or getattr(self, "_dog_expression_name", None) is not None
        ):
            return False

        self._wave_response_pending = None
        self._wave_response_pending_at = 0.0
        # Hello itself is the acknowledgement. Avoid synthesizing a redundant
        # greeting here: on the demo Mac that added a seven-second stationary
        # gap before a three-second firmware gesture.
        started = self._start_dog_expression("Hello", 3.0, now)
        logger.info(
            "queued wave response started between navigation legs",
            subject=(
                f"person:{pending.get('person_id')}"
                if pending.get("person_id")
                else f"track:{int(pending.get('track_id', -1))}"
            ),
            started=started,
        )
        return started

    def _maybe_eye_contact_response(
        self, now: float, nav_state: NavigationState
    ) -> bool:
        """Approach sustained attention at a route boundary, never mid-path."""
        if (
            not self.config.eye_contact_enabled
            or now < float(getattr(self, "_eye_contact_cooldown_until", 0.0))
            or now - float(getattr(self, "_eye_contact_last_poll", 0.0))
            < self.config.eye_contact_poll_s
        ):
            return False
        self._eye_contact_last_poll = now
        try:
            observation = self._follow.observe_attentive_person(
                self.config.eye_contact_min_height_frac,
                self.config.eye_contact_dwell_s,
            )
        except Exception:
            logger.exception("eye-contact pose observation failed")
            return False
        if not observation or nav_state is not NavigationState.IDLE:
            return False
        if getattr(self, "_dog_expression_name", None) is not None:
            return False

        # IDLE is the natural boundary between frontier legs. Replace that
        # next leg with one bounded social approach; no active route is
        # cancelled and no prompt asks the visitor to wait or look around.
        self._stop_exploration("sustained eye contact", cancel_goal=False)
        self._stop_patrol("sustained eye contact")
        try:
            result = self._follow.follow_person(
                "person",
                initial_bbox=[
                    float(value) for value in observation.get("bbox", [])
                ],
                initial_track_id=int(observation.get("track_id", -1)),
                person_id=observation.get("person_id"),
                identify_during_follow=False,
            )
        except Exception:
            logger.exception("eye-contact approach failed")
            return False
        if not result.startswith("Found the person"):
            logger.info("eye-contact approach was not started", result=result)
            return False

        self._attention_follow_until = (
            now + max(0.5, self.config.eye_contact_approach_s)
        )
        self._eye_contact_cooldown_until = (
            now + self.config.eye_contact_cooldown_s
        )
        # The physical response follows the approach. Reuse the same bounded
        # queue as hand waving, including its tracker-ID-proof global cooldown.
        self._wave_response_pending = dict(observation)
        self._wave_response_pending_at = now
        self._wave_response_global_until = max(
            float(getattr(self, "_wave_response_global_until", 0.0)),
            now + self.config.wave_response_cooldown_s,
        )
        logger.info(
            "sustained eye contact approach started",
            track_id=observation.get("track_id"),
            attention_s=observation.get("attention_s"),
            duration_s=self.config.eye_contact_approach_s,
        )
        return True

    def _interrupt_dog_expression(self, reason: str) -> None:
        name = getattr(self, "_dog_expression_name", None)
        if name is None:
            return
        self._dog_expression_name = None
        self._dog_expression_deadline = 0.0
        try:
            self._connection.sport_command(SPORT_CMD["StopMove"])
        except Exception:
            logger.exception(
                "curiosity dog expression interrupt failed",
                expression=name,
                reason=reason,
            )
        logger.info(
            "curiosity dog expression interrupted",
            expression=name,
            reason=reason,
        )

    def _maybe_sleep_scan(
        self, now: float, nav_state: NavigationState
    ) -> bool:
        """Run one bounded rear-lowered, camera-up fatigue capture window."""
        active_until = float(getattr(self, "_face_observation_until", 0.0))
        scan_kind = getattr(self, "_scan_kind", None)
        if active_until and scan_kind in {"scheduled", "forced"}:
            if now < active_until:
                self._set_activity(
                    BehaviorKind.OBSERVE,
                    "sleep_scan",
                    False,
                    "SLEEP_SCAN",
                )
                return True
            self._stop_face_observation("sleep scan window complete")
            self._sleep_active_elapsed_s = 0.0
            self._retry_not_before = now + 0.25
            return False

        forced = self._modes().consume_scan_request()
        due = float(getattr(self, "_sleep_active_elapsed_s", 0.0)) >= float(
            getattr(
                self,
                "_sleep_scan_interval_s",
                self.config.sleep_scan_interval_s,
            )
        )
        if not forced and not due:
            return False
        if not self._modes().begin_scan(forced=forced):
            if forced:
                # The operator was told "scan requested"; a silent drop here
                # is indistinguishable from a dead button. Record who vetoed.
                snapshot = self._modes().snapshot()
                logger.warning(
                    "forced sleep scan dropped",
                    mode=snapshot.mode.value,
                    interaction=snapshot.interaction_state.value,
                )
            return False

        # This is the one intentional mid-route pause in Sleep Analysis mode.
        # Stop every autonomous producer before touching body posture. The
        # exploration/patrol mission remains selected and is reacquired on the
        # first tick after cleanup.
        self._stop_face_observation("starting sleep scan")
        self._interrupt_dog_expression("starting sleep scan")
        self._stop_exploration("starting sleep scan", cancel_goal=True)
        self._stop_patrol("starting sleep scan")
        if nav_state is not NavigationState.IDLE:
            try:
                self._navigation.cancel_goal()
            except Exception:
                logger.exception("sleep scan navigation cancel failed")
        self._publish_stop()

        # Euler positive pitch raises the nose by lowering the rear relative to
        # the front. This is the verified Go2 sport API; no joint-level command
        # is synthesized here.
        try:
            if not ensure_motion_ready(self._connection):
                raise RuntimeError("motion controller did not accept BalanceStand")
            self._face_observation_pitch_active = bool(
                set_body_pitch(
                    self._connection,
                    self.config.face_observation_pitch_rad,
                )
            )
        except Exception:
            logger.exception("sleep scan rear-lowered posture failed")
            self._face_observation_pitch_active = False

        self._face_observation_key = None
        self._face_observation_bbox = None
        self._face_observation_candidate = None
        self._face_observation_streak = 0
        duration = float(
            getattr(
                self,
                "_sleep_scan_duration_s",
                self.config.sleep_scan_duration_s,
            )
        )
        # ensure_motion_ready's stand sequence above can block for several
        # seconds. Timing the window from the tick's stale `now` consumed most
        # of the 7 s budget before the camera was even raised (observed live:
        # a 2.6 s effective scan). The window starts when the posture is up.
        window_start = time.monotonic()
        self._scan_started_at = window_start
        self._face_observation_until = window_start + duration
        self._scan_kind = "forced" if forced else "scheduled"
        self._set_activity(
            BehaviorKind.OBSERVE,
            "sleep_scan",
            False,
            "SLEEP_SCAN",
        )
        logger.info(
            "sleep scan started",
            kind=self._scan_kind,
            duration_s=duration,
            camera_raised=self._face_observation_pitch_active,
            posture="rear_lowered_camera_up",
        )
        return True

    def _maybe_face_observation(self, now: float, nav_state: NavigationState) -> bool:
        """Frame one visible visitor during a natural patrol idle gap.

        This is the missing acquisition stage before the face-only fatigue
        model. Whole-person YOLO supplies the trigger; the existing server
        remains the sole owner of fatigue scoring and decides whether the full
        wave/intervention protocol should run.
        """
        active_until = float(getattr(self, "_face_observation_until", 0.0))
        if active_until:
            if now < active_until:
                # Keep body/face association current after the short approach.
                try:
                    refreshed = self._follow.observe_person(
                        self.config.face_observation_min_height_frac
                    )
                except Exception:
                    refreshed = None
                if refreshed:
                    self._set_face_observation_bbox(refreshed)
                self._set_activity(
                    BehaviorKind.PATROL,
                    "face_observation",
                    False,
                    None,
                )
                return True
            self._stop_face_observation("observation window complete")
            self._retry_not_before = now + 0.5
            return False

        if (
            not self.config.face_observation_enabled
            or self._map_phase not in {"EXPLORING", "MAPPED"}
            # Acquisition is an idle-gap behavior. The previous implementation
            # accepted this argument but never checked it, so a face detection
            # could cancel a live frontier path.
            or nav_state is not NavigationState.IDLE
            # On a fresh autonomous launch, let the explorer prove at least one
            # real movement before a nearby person can take ownership. This
            # prevents a person in front of the launch point from starving the
            # first goal indefinitely while preserving later interactions.
            or (
                bool(getattr(self, "_explore_started_once", False))
                and getattr(self, "_last_motion_ts", None) is None
            )
            or now < float(getattr(self, "_retry_not_before", 0.0))
            or now - float(getattr(self, "_face_observation_last_poll", 0.0))
            < self.config.face_observation_poll_s
        ):
            return False

        self._face_observation_last_poll = now
        try:
            observation = self._follow.observe_person(
                self.config.face_observation_min_height_frac
            )
        except Exception:
            logger.exception("face observation person detection failed")
            return False
        if not observation:
            self._face_observation_candidate = None
            self._face_observation_streak = 0
            return False

        person_id = observation.get("person_id")
        subject_key = (
            f"person:{person_id}"
            if person_id
            else f"track:{int(observation['track_id'])}"
        )
        cooldowns = dict(getattr(self, "_face_observation_cooldowns", {}))
        cooldowns = {
            key: deadline for key, deadline in cooldowns.items() if deadline > now
        }
        self._face_observation_cooldowns = cooldowns
        if cooldowns.get(subject_key, 0.0) > now:
            self._face_observation_candidate = None
            self._face_observation_streak = 0
            return False

        if subject_key == getattr(self, "_face_observation_candidate", None):
            self._face_observation_streak = (
                int(getattr(self, "_face_observation_streak", 0)) + 1
            )
        else:
            self._face_observation_candidate = subject_key
            self._face_observation_streak = 1
        if self._face_observation_streak < self.config.face_observation_streak:
            return False

        # Body-track continuity is enough for this short interaction. Persistent
        # ReID used to block here before the robot even stopped, letting a
        # passer-by leave. A known person ID is reused when already available;
        # otherwise track:<id> remains stable for this observation.
        self._stop_patrol("face observation")
        self._stop_exploration("face observation", cancel_goal=True)
        self._publish_stop()
        self._face_observation_key = subject_key
        self._set_face_observation_bbox(observation)
        self._face_observation_candidate = None
        self._face_observation_streak = 0
        self._face_observation_until = (
            time.monotonic() + self.config.face_observation_s
        )
        self._face_observation_cooldowns[subject_key] = (
            self._face_observation_until
            + self.config.face_observation_cooldown_s
        )
        self._speak_line(self.config.face_observation_prompt)
        self._set_activity(BehaviorKind.PATROL, "face_observation", False, None)
        try:
            ensure_motion_ready(self._connection)
            offset = max(-1.0, min(1.0, float(observation.get("offset", 0.0))))
            turn_s = abs(offset) * self.config.face_observation_turn_max_s
            if turn_s >= 0.08:
                # Positive image offset is to the camera's right.
                angular_z = -math.copysign(
                    self.config.face_observation_turn_rad_s, offset
                )
                self._publish_twist(
                    Twist(
                        linear=Vector3(),
                        angular=Vector3(0.0, 0.0, angular_z),
                    )
                )
                self._stop_event.wait(turn_s)
                self._publish_stop()
            self._face_observation_pitch_active = bool(
                set_body_pitch(
                    self._connection,
                    self.config.face_observation_pitch_rad,
                )
            )
        except Exception:
            logger.exception("face observation camera framing failed")
            self._publish_stop()

        # The centering turn changes image coordinates, so refresh the same
        # body track before exposing its box to the face-analysis bridge.
        # Store normalized coordinates: the body detector consumes the native
        # camera image while the fatigue server intentionally resizes frames.
        try:
            refreshed = self._follow.observe_person(
                self.config.face_observation_min_height_frac
            )
        except Exception:
            logger.exception("face observation post-turn refresh failed")
            refreshed = None
        if (
            refreshed
            and int(refreshed.get("track_id", -1))
            == int(observation["track_id"])
        ):
            observation = refreshed
        self._set_face_observation_bbox(observation)

        # Close the distance only after analysis is already active. The shared
        # body tracker provides a bounded 1.5 m visual-servo approach; no LLM
        # or second detector sits on this latency-critical path.
        approach_s = max(
            0.0, float(getattr(self.config, "face_observation_approach_s", 0.0))
        )
        if approach_s and observation.get("bbox"):
            try:
                result = self._follow.follow_person(
                    "person",
                    initial_bbox=[
                        float(value) for value in observation["bbox"]
                    ],
                    initial_track_id=int(observation["track_id"]),
                    person_id=None,
                    identify_during_follow=False,
                )
                if result.startswith("Found the person"):
                    self._face_approach_until = time.monotonic() + approach_s
            except Exception:
                logger.exception("face acquisition approach failed")
        logger.info(
            "face observation started",
            subject=subject_key,
            duration_s=self.config.face_observation_s,
            camera_raised=self._face_observation_pitch_active,
        )
        self._set_activity(BehaviorKind.PATROL, "face_observation", False, None)
        return True

    def _set_face_observation_bbox(self, observation: dict[str, Any]) -> None:
        bbox = [float(value) for value in observation.get("bbox", [])]
        frame_width = float(observation.get("frame_width", 0.0))
        frame_height = float(observation.get("frame_height", 0.0))
        if len(bbox) == 4 and frame_width > 0.0 and frame_height > 0.0:
            self._face_observation_bbox = [
                bbox[0] / frame_width,
                bbox[1] / frame_height,
                bbox[2] / frame_width,
                bbox[3] / frame_height,
            ]

    def _stop_face_observation(self, reason: str) -> None:
        active = bool(
            getattr(self, "_face_observation_until", 0.0)
            or getattr(self, "_face_observation_pitch_active", False)
            or getattr(self, "_scan_kind", None)
        )
        if not active:
            return
        if getattr(self, "_face_approach_until", 0.0):
            try:
                self._follow.stop_following()
            except Exception:
                logger.exception("face approach stop during observation cleanup failed")
        self._publish_stop()
        # UNCONDITIONAL restore. The raise enters firmware pose mode
        # (Pose on + Euler), and in pose mode the Go2 moves joints but ignores
        # every gait/velocity command. Gating this on pitch_active left pose
        # mode latched whenever the raise partially failed or the flag was
        # already cleared, freezing walking until a power cycle (observed live
        # twice on 2026-07-25). The restore is idempotent and cheap.
        try:
            set_body_pitch(self._connection, 0.0)
        except Exception:
            logger.exception("face observation neutral-pose restore failed")
        subject = getattr(self, "_face_observation_key", None)
        self._face_observation_until = 0.0
        self._face_observation_key = None
        self._face_observation_bbox = None
        self._face_observation_pitch_active = False
        self._face_approach_until = 0.0
        self._face_observation_candidate = None
        self._face_observation_streak = 0
        self._scan_kind = None
        self._scan_started_at = 0.0
        self._modes().end_scan()
        logger.info(
            "face observation stopped",
            subject=subject,
            reason=reason,
        )

    def _person_is_close(self, now: float) -> bool:
        if (
            now < self._follow_block_until
            or now - self._last_person_poll < self.config.person_poll_s
        ):
            return False
        self._last_person_poll = now
        try:
            observation = self._follow.observe_person(self.config.person_close_frac)
        except Exception:
            logger.exception("curiosity person observation failed")
            return False
        if not observation:
            self._close_streak = 0
            return False
        self._close_bbox = [float(v) for v in observation["bbox"]]
        self._close_track_id = int(observation["track_id"])
        self._close_person_id = observation.get("person_id")
        self._close_streak += 1
        return self._close_streak >= self.config.follow_streak

    def _start_curious_follow(self) -> None:
        self._close_streak = 0
        self._follow_block_until = time.monotonic() + self.config.follow_cooldown_s
        self._stop_exploration("close person", cancel_goal=True)
        self._stop_patrol("close person")
        try:
            dispatched = bool(
                self._agent_spec.dispatch_continuation(
                    {
                        "tool": "follow_person",
                        "args": {
                            "query": "person",
                            "initial_bbox": "$bbox",
                            "initial_track_id": "$track_id",
                            "person_id": "$person_id",
                        },
                    },
                    {
                        "bbox": self._close_bbox,
                        "track_id": self._close_track_id,
                        "person_id": self._close_person_id,
                        "label": "person",
                        "_silent": True,
                    },
                )
            )
            if not dispatched:
                logger.info("curiosity follow deferred until MCP tools are ready")
                return
            self._curious_follow_started = time.monotonic()
            self._set_activity(BehaviorKind.FOLLOW, "curiosity", True, None)
            # A short greeting once per dispatch, best-effort. The full pitch
            # belongs to the intervention protocol, not this friendly hello.
            self._speak_line(self.config.follow_greeting_text)
            logger.info("curiosity close-person follow dispatched")
        except Exception:
            logger.exception("curiosity follow dispatch failed")

    def _stop_curious_follow(self) -> None:
        try:
            self._agent_spec.dispatch_continuation(
                {"tool": "stop_following", "args": {}},
                {"_silent": True, "label": "curiosity follow timeout"},
            )
        except Exception:
            logger.exception("curiosity follow stop failed")
        self._curious_follow_started = 0.0
        self._retry_not_before = time.monotonic() + 0.5

    def _enforce_pose_mode_invariant(self, now: float) -> None:
        """Never let firmware pose mode outlive the scan that entered it.

        Pose mode makes the Go2 ignore every gait/velocity command while still
        moving joints and torso: the exact "stuck but twitching" freeze seen
        live. A scan's restore can be lost (WebRTC drop mid-scan, unacked
        command, interrupted window) and the old code never retried, so the
        dog stayed unwalkable until a power cycle. This runs every tick: if
        the latch is set while no scan/observation owns it, clear it, retrying
        at a bounded cadence until the firmware acknowledges.
        """
        if not pose_mode_latched():
            return
        scan_owns_pose = bool(
            getattr(self, "_scan_kind", None)
            or getattr(self, "_face_observation_until", 0.0)
        )
        if scan_owns_pose:
            return
        if now < getattr(self, "_pose_clear_not_before", 0.0):
            return
        self._pose_clear_not_before = now + 1.0
        logger.warning(
            "pose mode outlived its scan; clearing so the robot can walk"
        )
        try:
            clear_pose_mode(self._connection)
        except Exception:
            logger.exception("pose mode invariant clear failed")

    def _publish_twist(self, twist: Twist) -> None:
        # A velocity command is meaningless while pose mode is latched, so the
        # very act of wanting to move breaks the latch first (covers manual
        # WASD, planner output, and escapes without waiting for the watchdog).
        if pose_mode_latched():
            try:
                clear_pose_mode(self._connection)
            except Exception:
                logger.exception("pose mode clear before motion failed")
        self._twist_in_flight = True
        self.tele_cmd_vel.publish(twist)

    def _publish_stop(self) -> None:
        if not self._twist_in_flight:
            return
        self._twist_in_flight = False
        try:
            self.tele_cmd_vel.publish(Twist.zero())
        except Exception:
            logger.exception("curiosity zero twist failed")

    def _expire_leases_locked(self, now: float) -> None:
        expired = [
            lease_id
            for lease_id, lease in self._leases.items()
            if lease.expires_at <= now
        ]
        for lease_id in expired:
            del self._leases[lease_id]

    def _top_lease_locked(self) -> BehaviorLease | None:
        if not self._leases:
            return None
        return max(
            self._leases.values(),
            key=lambda lease: (lease.priority, lease.issued_at),
        )

    def _set_activity(
        self,
        behavior: BehaviorKind,
        owner: str,
        moving_expected: bool,
        hold_reason: str | None,
    ) -> None:
        changed = (
            behavior != self._behavior
            or owner != self._owner
            or moving_expected != self._moving_expected
            or hold_reason != self._hold_reason
        )
        self._behavior = behavior
        self._owner = owner
        self._moving_expected = moving_expected
        self._hold_reason = hold_reason
        now = time.monotonic()
        period = 1.0 / max(self.config.activity_hz, 0.1)
        if not changed and now - self._last_activity_publish < period:
            return
        snapshot = self._activity_snapshot()
        self._last_activity_publish = now
        try:
            self.activity.publish(snapshot)
        except Exception:
            logger.exception("curiosity activity publish failed")
        if changed:
            logger.info(
                "robot activity",
                behavior=behavior.value,
                owner=owner,
                moving_expected=moving_expected,
                hold_reason=hold_reason,
            )

    def _activity_snapshot(self) -> RobotActivity:
        mode = self._modes().snapshot()
        return RobotActivity(
            ts=time.time(),
            behavior=self._behavior,
            owner=self._owner,
            moving_expected=self._moving_expected,
            last_motion_ts=self._last_motion_ts,
            hold_reason=self._hold_reason,
            map_phase=self._map_phase,
            battery_soc=self._battery_soc,
            operating_mode=mode.mode,
            mode_epoch=mode.epoch,
            interaction_state=mode.interaction_state,
        )


# Compatibility imports used by older local scripts/tests.
IdleBehavior = CuriositySupervisor
IdleConfig = CuriosityConfig
