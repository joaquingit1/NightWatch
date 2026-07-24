from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod

from app.contracts import FatigueFactors, FatigueFrame


class ScoreSource(ABC):
    @abstractmethod
    def tick(self) -> None: ...

    @abstractmethod
    def latest(self) -> FatigueFrame: ...


class StubScoreSource(ScoreSource):
    def __init__(self) -> None:
        self._start = time.time()
        self._frame = self._build_frame(time.time())

    def _build_frame(self, ts: float) -> FatigueFrame:
        elapsed = ts - self._start
        score = 35 + 25 * (0.5 + 0.5 * math.sin(elapsed * 0.4))
        perclos = min(1.0, max(0.0, score / 100 * 0.8))
        cx = int(480 + 80 * math.sin(elapsed * 0.7))
        cy = int(270 + 30 * math.cos(elapsed * 0.9))
        return FatigueFrame(
            ts=ts,
            person_id="stub-person-01",
            bbox=(cx - 90, cy - 110, cx + 90, cy + 110),
            score=score,
            confidence=0.82,
            factors=FatigueFactors(
                perclos=perclos,
                blink_ms_p50=180 + 40 * math.sin(elapsed),
                blink_ms_p90=320,
                nod_count=int(max(0, 2 * math.sin(elapsed * 0.2))),
                yawn_count=int(max(0, 1 * math.sin(elapsed * 0.15))),
                slump_deg=8 + 6 * math.sin(elapsed * 0.3),
                eye_cnn_perclos=-1.0,
                movement_entropy=0.4,
                sedentary_hours=3.5,
            ),
            calib_state="quick",
            scorer="thresholds",
        )

    def tick(self) -> None:
        self._frame = self._build_frame(time.time())

    def latest(self) -> FatigueFrame:
        return self._frame
