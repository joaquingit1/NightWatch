from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import requests

from app.contracts import (
    FatigueAssessment,
    FatigueFrame,
    fatigue_assessment_to_dict,
)

logger = logging.getLogger("nightwatch.robot_bridge")

MODEL_VERSION = "nightwatch-fatigue-v1"


def _factors_from_frame(frame: FatigueFrame) -> tuple[str, ...]:
    factors: list[str] = []
    f = frame.factors
    if f.perclos >= 0.15:
        factors.append("perclos")
    if f.blink_ms_p90 >= 400:
        factors.append("long_blinks")
    if f.yawn_count > 0:
        factors.append("yawning")
    if f.nod_count > 0:
        factors.append("nodding")
    if f.slump_deg >= 18:
        factors.append("slump")
    if f.movement_entropy < 0.2 and f.sedentary_hours > 0.25:
        factors.append("stillness")
    return tuple(factors)


def frame_to_assessment(
    frame: FatigueFrame,
    *,
    observation_seconds: float,
    track_id: str,
) -> FatigueAssessment:
    bbox: tuple[float, float, float, float] | None
    if frame.bbox == (0, 0, 0, 0):
        bbox = None
    else:
        bbox = tuple(float(v) for v in frame.bbox)

    return FatigueAssessment(
        assessment_id=uuid.uuid4().hex,
        ts=frame.ts,
        track_id=track_id,
        anonymous_person_id=frame.person_id,
        bbox=bbox,
        fatigue_score=min(1.0, max(0.0, frame.score / 100.0)),
        confidence=min(1.0, max(0.0, frame.confidence)),
        quality=min(1.0, max(0.0, frame.confidence)),
        factors=_factors_from_frame(frame),
        observation_seconds=observation_seconds,
        model_version=MODEL_VERSION,
    )


class RobotBridge:
    """Emit FatigueAssessment JSON lines for the robot policy handoff contract."""

    def __init__(
        self,
        assessment_path: str,
        status_url: str,
        window_seconds: float,
        get_latest_frame: Callable[[], FatigueFrame],
    ) -> None:
        self._assessment_path = Path(assessment_path)
        self._status_url = status_url
        self._window_seconds = window_seconds
        self._get_latest_frame = get_latest_frame
        self._track_started: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_forever(self) -> None:
        while not self._stopping:
            try:
                await self._emit_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep loop alive
                logger.warning("robot bridge emit failed: %s", exc)
            await asyncio.sleep(self._window_seconds)

    async def _emit_once(self) -> None:
        await asyncio.to_thread(self._poll_robot_status)
        frame = self._get_latest_frame()
        if frame.person_id is None:
            return

        track_id = frame.person_id
        now = time.time()
        if track_id not in self._track_started:
            self._track_started[track_id] = now
        observation_seconds = max(0.0, now - self._track_started[track_id])

        assessment = frame_to_assessment(
            frame,
            observation_seconds=observation_seconds,
            track_id=track_id,
        )
        await asyncio.to_thread(self._append_assessment, assessment)

    def _poll_robot_status(self) -> None:
        try:
            response = requests.get(self._status_url, timeout=2)
            response.raise_for_status()
            payload = response.json()
            logger.debug(
                "robot status: behavior=%s map_phase=%s",
                payload.get("behavior"),
                payload.get("map_phase"),
            )
        except Exception as exc:  # noqa: BLE001 - non-fatal
            logger.debug("robot status unavailable: %s", exc)

    def _append_assessment(self, assessment: FatigueAssessment) -> None:
        self._assessment_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(fatigue_assessment_to_dict(assessment), separators=(",", ":"))
        with self._assessment_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        logger.info(
            "wrote assessment track=%s score=%.2f obs=%.1fs",
            assessment.track_id,
            assessment.fatigue_score,
            assessment.observation_seconds,
        )
