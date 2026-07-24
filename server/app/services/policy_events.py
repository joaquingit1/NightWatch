from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from typing import Any

from app.contracts import FSM_STATES, FatigueFrame, PolicyEvent

_AUDIO_CUES_BY_STATE = {
    "TRIAGE": "triage_01",
    "APPROACH": "approach_01",
    "DIAGNOSE": "diagnose_01",
    "PRESCRIBE": "prescribe_01",
    "ESCORT": "escort_01",
    "NAP_REGISTERED": "nap_registered_01",
}


class PolicyEventSource(ABC):
    @abstractmethod
    def tick(self) -> None: ...

    @abstractmethod
    def subscribe(self) -> deque[PolicyEvent]: ...


class LivePolicyEventSource(PolicyEventSource):
    """Emit thoughts from inference, intake, and the real robot bridge."""

    def __init__(
        self,
        get_frame: Callable[[], FatigueFrame],
        get_pending_escorts: Callable[[], list[Any]],
        *,
        fatigue_threshold: float = 55.0,
        prescribe_threshold: float = 70.0,
        heartbeat_seconds: float = 20.0,
    ) -> None:
        self._get_frame = get_frame
        self._get_pending_escorts = get_pending_escorts
        self._fatigue_threshold = fatigue_threshold
        self._prescribe_threshold = prescribe_threshold
        self._heartbeat_seconds = heartbeat_seconds
        self._events: deque[PolicyEvent] = deque(maxlen=100)
        self._last_state: str | None = None
        self._last_emit = 0.0
        self._emit(
            "PATROL",
            "守夜犬上线，开始巡逻 | Night Watch online, starting patrol",
            None,
            force=True,
        )

    def _emit(
        self,
        state: str,
        detail: str,
        target: str | None,
        *,
        force: bool = False,
    ) -> None:
        if state not in FSM_STATES:
            state = "PATROL"
        now = time.time()
        if (
            not force
            and state == self._last_state
            and now - self._last_emit < self._heartbeat_seconds
        ):
            return
        state_changed = state != self._last_state
        self._last_state = state
        self._last_emit = now
        self._events.append(
            PolicyEvent(
                ts=now,
                state=state,
                target_person=target,
                utterance=_AUDIO_CUES_BY_STATE.get(state) if state_changed else None,
                detail=detail,
            )
        )

    def publish(self, event: PolicyEvent) -> None:
        """Accept authoritative events emitted by the robot control bridge."""
        if event.state not in FSM_STATES:
            event.state = "PATROL"
        self._last_state = event.state
        self._last_emit = event.ts
        self._events.append(event)

    def tick(self) -> None:
        pending = self._get_pending_escorts()
        if pending:
            guest = pending[0]
            alias = getattr(guest, "name_alias", None) or f"访客 {guest.session_id[:6]}"
            self._emit(
                "ESCORT",
                f"等待护送 {alias} | {alias} is waiting for an escort",
                guest.session_id,
            )
            return

        frame = self._get_frame()
        target = frame.person_id

        if target is None or frame.bbox == (0, 0, 0, 0) or frame.confidence < 0.15:
            self._emit(
                "PATROL",
                "巡逻中，扫描会场疲劳信号 | Patrolling venue for fatigue signals",
                None,
            )
            return

        if frame.calib_state in {"uncalibrated", "quick"}:
            self._emit(
                "DIAGNOSE",
                (
                    f"校准生物信号中 · RestScore {frame.score:.0f} "
                    "| Calibrating biometrics"
                ),
                target,
            )
            return

        if frame.score >= self._prescribe_threshold:
            self._emit(
                "PRESCRIBE",
                f"RestScore {frame.score:.0f}，建议小憩 | Recommending a nap break",
                target,
            )
            return

        if frame.score >= self._fatigue_threshold:
            self._emit(
                "TRIAGE",
                (
                    f"RestScore {frame.score:.0f}，疲劳信号上升 "
                    "| Fatigue rising, preparing triage"
                ),
                target,
            )
            return

        posture_bits: list[str] = []
        if frame.factors.slump_deg >= 12:
            posture_bits.append(f"低头 {frame.factors.slump_deg:.0f}°")
        if frame.factors.nod_count > 0:
            posture_bits.append(f"点头 x{frame.factors.nod_count}")
        if frame.factors.yawn_count > 0:
            posture_bits.append(f"哈欠 x{frame.factors.yawn_count}")
        posture = f" · {', '.join(posture_bits)}" if posture_bits else ""
        self._emit(
            "PATROL",
            f"监测中 RestScore {frame.score:.0f}{posture} | Monitoring booth visitor",
            target,
        )

    def subscribe(self) -> deque[PolicyEvent]:
        return self._events


class StubPolicyEventSource(PolicyEventSource):
    """Small offline heartbeat for environments without inference."""

    def __init__(self) -> None:
        self._events: deque[PolicyEvent] = deque(maxlen=100)
        self._last_emit = 0.0

    def tick(self) -> None:
        now = time.time()
        if now - self._last_emit < 4.0:
            return
        self._last_emit = now
        self._events.append(
            PolicyEvent(
                ts=now,
                state="PATROL",
                target_person=None,
                utterance=None,
                detail="巡逻中，扫描会场疲劳信号 | Patrolling venue for fatigue signals",
            )
        )

    def subscribe(self) -> deque[PolicyEvent]:
        return self._events
