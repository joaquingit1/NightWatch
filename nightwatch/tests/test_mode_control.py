from __future__ import annotations

import time

import pytest
from types import SimpleNamespace
from threading import RLock

from dimos.navigation.base import NavigationState

from nightwatch.contracts import BehaviorKind, InteractionState, OperatingMode
from nightwatch.curiosity import CuriosityConfig, CuriositySupervisor
from nightwatch.mode_control import ModeController


class Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


def test_public_mode_aliases_resolve_to_one_authoritative_mode() -> None:
    assert ModeController.parse_mode("exploration") is OperatingMode.AUTONOMOUS
    assert ModeController.parse_mode("cruise") is OperatingMode.SLEEP_ANALYSIS
    assert ModeController.parse_mode("sleep-analysis") is OperatingMode.SLEEP_ANALYSIS
    assert ModeController.parse_mode("manual") is OperatingMode.MANUAL
    with pytest.raises(ValueError):
        ModeController.parse_mode("confused")


def test_transition_increments_epoch_and_clears_in_flight_interaction() -> None:
    controller = ModeController()
    first = controller.transition("sleep_analysis")
    assert first.changed is True
    assert first.epoch == 1
    assert controller.set_interaction(InteractionState.WAITING_FORM) is True

    manual = controller.transition("manual")
    snapshot = controller.snapshot()
    assert manual.changed is True
    assert manual.epoch == 2
    assert snapshot.mode is OperatingMode.MANUAL
    assert snapshot.interaction_state is InteractionState.IDLE
    assert snapshot.scan_requested is False


def test_same_mode_transition_is_idempotent_and_keeps_epoch() -> None:
    controller = ModeController(OperatingMode.MANUAL)
    transition = controller.transition("manual")
    assert transition.changed is False
    assert transition.epoch == 0


def test_manual_override_resumes_the_previous_autonomous_mission() -> None:
    controller = ModeController(OperatingMode.SLEEP_ANALYSIS)
    controller.transition("manual")
    resumed = controller.resume_autonomy()
    assert resumed.current is OperatingMode.SLEEP_ANALYSIS
    assert resumed.epoch == 2


def test_manual_commands_require_current_epoch_and_increasing_sequence() -> None:
    clock = Clock()
    controller = ModeController(clock=clock)
    epoch = controller.transition("manual").epoch

    assert controller.accept_manual(epoch=epoch - 1, sequence=1) == (
        False,
        "stale mode epoch",
    )
    assert controller.accept_manual(epoch=epoch, sequence=1)[0] is True
    assert controller.accept_manual(epoch=epoch, sequence=1) == (
        False,
        "stale manual sequence",
    )
    assert controller.accept_manual(epoch=epoch, sequence=2)[0] is True


def test_manual_watchdog_expires_after_half_a_second() -> None:
    clock = Clock()
    controller = ModeController(clock=clock)
    epoch = controller.transition("manual").epoch
    assert controller.accept_manual(epoch=epoch, sequence=1)[0] is True
    clock.now += 0.5
    assert controller.manual_expired(timeout_s=0.5) is False
    clock.now += 0.001
    assert controller.manual_expired(timeout_s=0.5) is True


def test_scan_is_only_available_in_sleep_analysis_and_is_cancelled_by_mode_change() -> None:
    controller = ModeController()
    assert controller.request_scan()[0] is False

    controller.transition("sleep_analysis")
    assert controller.request_scan()[0] is True
    assert controller.consume_scan_request() is True
    assert controller.consume_scan_request() is False
    assert controller.begin_scan(forced=True) is True
    assert controller.snapshot().interaction_state is InteractionState.FORCED_SCAN

    controller.transition("autonomous")
    snapshot = controller.snapshot()
    assert snapshot.interaction_state is InteractionState.IDLE
    assert snapshot.scan_requested is False
    assert controller.begin_scan(forced=False) is False


def test_second_person_cannot_replace_active_interaction() -> None:
    controller = ModeController(OperatingMode.SLEEP_ANALYSIS)
    assert controller.set_interaction(InteractionState.APPROACHING) is True
    assert controller.request_scan() == (
        False,
        "a person interaction or escort is active",
    )
    assert controller.begin_scan(forced=False) is False


def test_sleep_scan_is_bounded_keeps_balanced_stance_and_resumes(
    monkeypatch,
) -> None:
    supervisor = object.__new__(CuriositySupervisor)
    supervisor.config = CuriosityConfig(
        sleep_scan_interval_s=10.0,
        sleep_scan_duration_s=7.0,
    )
    supervisor._mode_control = ModeController(OperatingMode.SLEEP_ANALYSIS)
    supervisor._sleep_active_elapsed_s = 10.0
    supervisor._sleep_scan_interval_s = 10.0
    supervisor._sleep_scan_duration_s = 7.0
    supervisor._face_observation_until = 0.0
    supervisor._face_observation_pitch_active = False
    supervisor._scan_kind = None
    supervisor._scan_started_at = 0.0
    supervisor._connection = SimpleNamespace()
    supervisor._navigation = SimpleNamespace(cancel_goal=lambda: True)
    calls: list[object] = []
    supervisor._stop_face_observation = (
        lambda reason: calls.append(("cleanup", reason))
        if not supervisor._scan_kind
        else CuriositySupervisor._stop_face_observation(supervisor, reason)
    )
    supervisor._interrupt_dog_expression = lambda reason: calls.append(
        ("interrupt", reason)
    )
    supervisor._stop_exploration = lambda reason, *, cancel_goal: calls.append(
        ("explore_stop", reason, cancel_goal)
    )
    supervisor._stop_patrol = lambda reason: calls.append(("patrol_stop", reason))
    supervisor._publish_stop = lambda: calls.append("zero")
    supervisor._set_activity = lambda *args: calls.append(("activity", *args))
    supervisor._retry_not_before = 0.0
    monkeypatch.setattr("nightwatch.curiosity.ensure_motion_ready", lambda _c: True)
    # The scan window is timed from a fresh monotonic read taken AFTER the
    # blocking stand/posture sequence (the tick's stale `now` used to eat most
    # of the 7 s budget). Freeze curiosity's clock at the tick time so the
    # window arithmetic stays assertable.
    monkeypatch.setattr(
        "nightwatch.curiosity.time",
        SimpleNamespace(monotonic=lambda: 100.0, time=time.time, sleep=time.sleep),
    )

    assert (
        supervisor._maybe_sleep_scan(100.0, NavigationState.FOLLOWING_PATH)
        is True
    )
    assert supervisor._scan_kind == "scheduled"
    assert supervisor._face_observation_until == 107.0
    assert supervisor._face_observation_pitch_active is False
    assert supervisor._modes().snapshot().interaction_state is (
        InteractionState.SCHEDULED_SCAN
    )

    assert supervisor._maybe_sleep_scan(108.0, NavigationState.IDLE) is False
    assert supervisor._scan_kind is None
    assert supervisor._sleep_active_elapsed_s == 0.0
    assert supervisor._modes().snapshot().interaction_state is InteractionState.IDLE


def _sleep_mode_switch_supervisor() -> CuriositySupervisor:
    supervisor = object.__new__(CuriositySupervisor)
    supervisor.config = CuriosityConfig()
    supervisor._lock = RLock()
    supervisor._leases = {}
    supervisor._preempt_requested = False
    supervisor._retry_not_before = 0.0
    supervisor._intake_wait_until = 0.0
    supervisor._sleep_active_elapsed_s = 0.0
    supervisor._sleep_scan_interval_s = 25.0
    supervisor._sleep_scan_duration_s = 7.0
    supervisor._mode_control = ModeController(OperatingMode.AUTONOMOUS)
    return supervisor


def test_entering_sleep_analysis_makes_first_scan_promptly_due() -> None:
    """The first scan lands one hard-minimum interval after mode entry.

    Live sessions showed operators leaving Sleep Analysis within ~15-30 s
    because nothing visibly happened; the first scan must not wait one full
    cadence interval.
    """
    supervisor = _sleep_mode_switch_supervisor()

    supervisor.set_operating_mode("sleep_analysis")
    assert supervisor._sleep_active_elapsed_s == pytest.approx(
        25.0 - supervisor.config.sleep_scan_interval_min_s
    )

    # Leaving Sleep Analysis always clears the clock so re-entry cannot
    # fire a stale scan instantly.
    supervisor.set_operating_mode("manual")
    assert supervisor._sleep_active_elapsed_s == 0.0

    # Resuming autonomy back into Sleep Analysis is a fresh entry and gets
    # the same prompt first scan.
    supervisor.set_control_mode("autonomous")
    assert supervisor._modes().snapshot().mode is OperatingMode.SLEEP_ANALYSIS
    assert supervisor._sleep_active_elapsed_s == pytest.approx(
        25.0 - supervisor.config.sleep_scan_interval_min_s
    )


def test_sleep_scan_clock_accrues_during_patrol_planner_waits(
    monkeypatch,
) -> None:
    """Scan cadence is wall-clock Sleep Analysis time, not moving-only ticks.

    Real patrol alternates route legs with planner waits (navigation IDLE).
    The old accrual gate skipped IDLE ticks, so a 25 s interval could take
    minutes of wall time and the mode looked inert.
    """
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
    curiosity._map_phase = "MAPPED"
    curiosity._exploring = False
    curiosity._patrolling = True
    curiosity._curious_follow_started = 0.0
    curiosity._leases = {}
    curiosity._refresh_battery = lambda _now: None
    curiosity._is_following = lambda: False
    curiosity._navigation_state = lambda: NavigationState.IDLE
    curiosity._expire_leases_locked = lambda _now: None
    curiosity._top_lease_locked = lambda: None
    curiosity._sensors_ready = lambda _now: True
    curiosity._refresh_map_phase = lambda _now: None
    curiosity._maybe_eye_contact_response = lambda *_args: False
    curiosity._maybe_wave_response = lambda *_args: False
    curiosity._maybe_face_observation = lambda *_args: False
    curiosity._maybe_hand_expression = lambda *_args: False
    curiosity._maybe_dog_expression = lambda *_args, **_kwargs: False
    curiosity._person_is_close = lambda _now: False
    curiosity._ensure_patrolling = lambda _now: None
    curiosity._maybe_escape_stall = lambda _now: False
    curiosity._enforce_liveness = lambda _now, _state: None
    curiosity._interrupt_dog_expression = lambda _reason: None
    curiosity._stop_exploration = lambda _reason, *, cancel_goal: None
    curiosity._stop_patrol = lambda _reason: None
    curiosity._publish_stop = lambda: None
    curiosity._navigation = SimpleNamespace(cancel_goal=lambda: True)
    curiosity._connection = object()
    curiosity._follow = SimpleNamespace(
        observe_person=lambda *_args: None,
        stop_following=lambda: None,
    )
    activities: list[tuple] = []
    curiosity._set_activity = lambda *args: activities.append(args)
    curiosity._face_observation_until = 0.0
    curiosity._face_observation_pitch_active = False
    curiosity._face_observation_key = None
    curiosity._face_observation_bbox = None
    curiosity._face_observation_candidate = None
    curiosity._face_observation_streak = 0
    curiosity._face_approach_until = 0.0
    curiosity._scan_kind = None
    curiosity._scan_started_at = 0.0
    curiosity._retry_not_before = 0.0
    curiosity._sleep_active_elapsed_s = 0.0
    curiosity._sleep_scan_interval_s = 3.0
    curiosity._sleep_scan_duration_s = 7.0
    curiosity._mode_control = ModeController(OperatingMode.SLEEP_ANALYSIS)
    monkeypatch.setattr(
        "nightwatch.curiosity.ensure_motion_ready", lambda *_a, **_k: True
    )

    def tick_one_second() -> None:
        # tick_elapsed is clamped to 1.0 s; backdating the previous tick
        # start guarantees the clamp is hit and the test stays exact.
        curiosity._last_tick_started_at = time.monotonic() - 2.0
        curiosity._tick()

    tick_one_second()
    tick_one_second()
    assert curiosity._scan_kind is None
    # Navigation was IDLE the whole time and the clock still advanced.
    assert curiosity._sleep_active_elapsed_s == pytest.approx(2.0)

    tick_one_second()
    assert curiosity._scan_kind == "scheduled"
    assert curiosity._face_observation_pitch_active is False
    assert curiosity._modes().snapshot().interaction_state is (
        InteractionState.SCHEDULED_SCAN
    )
    assert activities[-1][:2] == (BehaviorKind.OBSERVE, "sleep_scan")


def test_intake_wait_is_bounded_and_manual_transition_clears_it() -> None:
    supervisor = object.__new__(CuriositySupervisor)
    supervisor._lock = RLock()
    supervisor._mode_control = ModeController(OperatingMode.SLEEP_ANALYSIS)
    supervisor._preempt_requested = False
    supervisor._leases = {}
    supervisor._sleep_active_elapsed_s = 0.0
    supervisor._retry_not_before = 0.0
    supervisor._intake_wait_until = 0.0

    started = supervisor.begin_intake_wait(999.0)
    assert "180s" in started
    assert supervisor._intake_wait_until > 0.0
    assert supervisor._modes().snapshot().interaction_state is (
        InteractionState.WAITING_FORM
    )

    supervisor.set_operating_mode("manual")
    assert supervisor._intake_wait_until == 0.0
    assert supervisor._modes().snapshot().mode is OperatingMode.MANUAL
    assert supervisor._modes().snapshot().interaction_state is InteractionState.IDLE
