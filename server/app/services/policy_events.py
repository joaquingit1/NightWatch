from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque

from app.contracts import FSM_STATES, PolicyEvent


class PolicyEventSource(ABC):
    @abstractmethod
    def tick(self) -> None: ...

    @abstractmethod
    def subscribe(self) -> deque[PolicyEvent]: ...


SCRIPTED_EVENTS: list[tuple[str, str, str | None]] = [
    ("PATROL", "巡逻中，扫描会场疲劳信号 | Patrolling venue for fatigue signals", None),
    ("TRIAGE", "发现可疑疲劳，准备接近 | Fatigue detected, preparing approach", "stub-person-01"),
    ("APPROACH", "正在靠近，请保持面向镜头 | Approaching, please face the camera", "stub-person-01"),
    ("DIAGNOSE", "诊断中，采集15秒生物信号 | Diagnosing, collecting 15s biometrics", "stub-person-01"),
    ("PRESCRIBE", "建议小憩20分钟 | Recommending a 20-minute nap", "stub-person-01"),
    ("ESCORT", "护送前往休息区 | Escorting to nap zone", "stub-person-01"),
    ("NAP_REGISTERED", "已登记午睡，开始守夜 | Nap registered, night watch begins", "stub-person-01"),
    ("PASS_CHECK", "巡房检查中 | Conducting pass check", "stub-person-01"),
    ("WAKE_LADDER", "温柔唤醒流程启动 | Gentle wake ladder started", "stub-person-01"),
    ("CELEBRATE", "醒来啦，状态不错 | Awake and refreshed", "stub-person-01"),
    ("RESET", "返回巡逻状态 | Returning to patrol", None),
]


class StubPolicyEventSource(PolicyEventSource):
    def __init__(self) -> None:
        self._events: deque[PolicyEvent] = deque(maxlen=100)
        self._index = 0
        self._last_emit = 0.0

    def tick(self) -> None:
        now = time.time()
        if now - self._last_emit < 4.0:
            return
        state, detail, target = SCRIPTED_EVENTS[self._index % len(SCRIPTED_EVENTS)]
        self._index += 1
        self._last_emit = now
        event = PolicyEvent(
            ts=now,
            state=state,
            target_person=target,
            utterance=f"{state.lower()}_01" if state not in {"IDLE", "RESET"} else None,
            detail=detail,
        )
        if state not in FSM_STATES:
            event.state = "PATROL"
        self._events.append(event)

    def subscribe(self) -> deque[PolicyEvent]:
        return self._events
