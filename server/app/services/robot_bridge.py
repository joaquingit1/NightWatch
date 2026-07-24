from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import requests

from app.contracts import (
    FatigueAssessment,
    FatigueFrame,
    PolicyEvent,
    fatigue_assessment_to_dict,
)

logger = logging.getLogger("nightwatch.robot_bridge")

MODEL_VERSION = "nightwatch-fatigue-v1"
_REFUSAL_PREFIXES = (
    "Tool not found:",
    "Cannot start '",
    "Cannot start the escort:",
    "Error running tool '",
    "No sleeping area is known",
    "Escort ended without reaching",
)
_STATE_UTTERANCES = {
    "TRIAGE": "triage_01",
    "APPROACH": "approach_01",
    "DIAGNOSE": "diagnose_01",
    "PRESCRIBE": "prescribe_01",
    "ESCORT": "escort_01",
    "NAP_REGISTERED": "nap_registered_01",
}


@dataclass(frozen=True)
class SkillResult:
    ok: bool
    text: str


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
        quality=min(
            1.0,
            max(
                0.0,
                frame.quality if frame.quality > 0.0 else frame.confidence,
            ),
        ),
        factors=_factors_from_frame(frame),
        observation_seconds=observation_seconds,
        model_version=(
            frame.model_version
            if frame.model_version not in {"", "unknown"}
            else MODEL_VERSION
        ),
    )


class RobotBridge:
    """Join live fatigue inference to the Nightwatch Go2 policy.

    Every scored window is persisted locally and POSTed to the robot operator
    API. Sustained high-quality evidence starts the suspicious protocol, and a
    stronger sustained result after that protocol requests an escort. Calls
    are serialized so two people can never compete for motion authority.
    """

    def __init__(
        self,
        assessment_path: str,
        status_url: str,
        assessment_url: str,
        mcp_url: str,
        window_seconds: float,
        get_latest_frame: Callable[[], FatigueFrame],
        publish_event: Callable[[PolicyEvent], None] | None = None,
        *,
        intervene_score: float = 0.65,
        escort_score: float = 0.75,
        min_confidence: float = 0.55,
        min_quality: float = 0.55,
        min_observation_seconds: float = 8.0,
        consecutive_windows: int = 2,
        person_cooldown_seconds: float = 180.0,
        update_intake_status: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._assessment_path = Path(assessment_path) if assessment_path else None
        self._status_url = status_url
        self._assessment_url = assessment_url
        self._mcp_url = mcp_url
        self._window_seconds = max(0.1, window_seconds)
        self._get_latest_frame = get_latest_frame
        self._publish_event = publish_event
        self._intervene_score = min(1.0, max(0.0, intervene_score))
        self._escort_score = min(1.0, max(self._intervene_score, escort_score))
        self._min_confidence = min(1.0, max(0.0, min_confidence))
        self._min_quality = min(1.0, max(0.0, min_quality))
        self._min_observation_seconds = max(0.0, min_observation_seconds)
        self._consecutive_windows = max(1, consecutive_windows)
        self._person_cooldown_seconds = max(0.0, person_cooldown_seconds)
        self._update_intake_status = update_intake_status

        self._track_started: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}
        self._streaks: defaultdict[str, int] = defaultdict(int)
        self._cooldown_until: defaultdict[str, float] = defaultdict(float)
        self._recent: deque[FatigueAssessment] = deque(
            maxlen=max(20, self._consecutive_windows * 6)
        )
        self._latest_status: dict[str, Any] = {
            "enabled": True,
            "connected": False,
        }
        self._task: asyncio.Task[None] | None = None
        self._policy_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._request_id = 0

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stopping = True
        tasks = [task for task in (self._task, self._policy_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._policy_task = None

    def snapshot(self) -> dict[str, Any]:
        status = dict(self._latest_status)
        status.update(
            {
                "enabled": True,
                "policy_active": (
                    self._policy_task is not None and not self._policy_task.done()
                ),
                "intervene_score": self._intervene_score,
                "escort_score": self._escort_score,
            }
        )
        return status

    def request_intake_escort(self, response_id: str, track_id: str) -> bool:
        """Dispatch an explicit, consented intake escort without blocking HTTP."""
        if self._policy_task is not None and not self._policy_task.done():
            return False
        if not self._robot_accepts_intervention():
            return False
        self._policy_task = asyncio.create_task(
            self._run_intake_escort(response_id, track_id)
        )
        return True

    async def operator_action(self, action: str) -> SkillResult:
        """Dispatch one explicitly allow-listed command-center posture action."""
        skills = {
            "lie_down": "lie_down_until_resumed",
            "stand_up": "stand_up_and_resume",
        }
        skill = skills.get(action)
        if skill is None:
            return SkillResult(False, f"Unsupported robot action: {action}")
        if not self._latest_status.get("connected", False):
            return SkillResult(False, "Robot is offline")
        return await self._call_skill(skill)

    async def _run_forever(self) -> None:
        while not self._stopping:
            started = time.monotonic()
            try:
                await self._emit_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the bridge alive
                logger.warning("robot bridge cycle failed: %s", exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, self._window_seconds - elapsed))

    async def _emit_once(self) -> None:
        status = await asyncio.to_thread(self._poll_robot_status)
        if status is not None:
            self._latest_status = {
                "enabled": True,
                "connected": True,
                **status,
            }
        else:
            self._latest_status = {
                **self._latest_status,
                "enabled": True,
                "connected": False,
            }

        frame = self._get_latest_frame()
        tracks = frame.people or (
            [frame] if frame.person_id is not None else []
        )
        if not tracks:
            self._expire_missing_tracks(time.time())
            return

        now = time.time()
        assessments: list[FatigueAssessment] = []
        actionable: set[str] = set()
        observation_subject = str(
            self._latest_status.get("face_observation_subject") or ""
        )
        stable_person_id = (
            observation_subject.removeprefix("person:")
            if observation_subject.startswith("person:")
            else None
        )
        observed_body_bbox = self._latest_status.get("face_observation_bbox")
        for tracked in tracks:
            if tracked.person_id is None:
                continue
            track_id = tracked.person_id
            if (
                stable_person_id
                and self._face_belongs_to_observed_body(
                    tracked.bbox, observed_body_bbox
                )
            ):
                track_id = stable_person_id
                tracked = replace(tracked, person_id=stable_person_id)
            self._track_started.setdefault(track_id, now)
            self._last_seen[track_id] = now
            assessment = frame_to_assessment(
                tracked,
                observation_seconds=max(
                    0.0, now - self._track_started[track_id]
                ),
                track_id=track_id,
            )
            assessments.append(assessment)
            self._recent.append(assessment)
            if tracked.looking_at_camera:
                actionable.add(assessment.assessment_id)

        writes = []
        for assessment in assessments:
            if self._assessment_path is not None:
                writes.append(
                    asyncio.to_thread(self._append_assessment, assessment)
                )
            if self._assessment_url:
                writes.append(
                    asyncio.to_thread(self._post_assessment, assessment)
                )
        if writes:
            await asyncio.gather(*writes, return_exceptions=True)

        for assessment in assessments:
            self._evaluate_candidate(
                assessment,
                direct_attention=assessment.assessment_id in actionable,
            )
        self._expire_missing_tracks(now)

    @staticmethod
    def _face_belongs_to_observed_body(
        face_bbox: tuple[int, int, int, int],
        body_bbox: Any,
    ) -> bool:
        if not isinstance(body_bbox, (list, tuple)) or len(body_bbox) != 4:
            return False
        x1, y1, x2, y2 = (float(value) for value in body_bbox)
        fx1, fy1, fx2, fy2 = (float(value) for value in face_bbox)
        cx = (fx1 + fx2) * 0.5
        cy = (fy1 + fy2) * 0.5
        return x1 <= cx <= x2 and y1 <= cy <= y2

    def _evaluate_candidate(
        self,
        assessment: FatigueAssessment,
        *,
        direct_attention: bool,
    ) -> None:
        track_id = assessment.track_id
        eligible = (
            direct_attention
            and
            assessment.fatigue_score >= self._intervene_score
            and assessment.confidence >= self._min_confidence
            and assessment.quality >= self._min_quality
            and assessment.observation_seconds >= self._min_observation_seconds
        )
        self._streaks[track_id] = self._streaks[track_id] + 1 if eligible else 0
        if self._streaks[track_id] < self._consecutive_windows:
            return

        now = time.time()
        if now < self._cooldown_until[track_id]:
            return
        if self._policy_task is not None and not self._policy_task.done():
            return
        if not self._robot_accepts_intervention():
            return

        self._streaks[track_id] = 0
        self._cooldown_until[track_id] = now + self._person_cooldown_seconds
        self._policy_task = asyncio.create_task(self._run_care_sequence(track_id))

    def _robot_accepts_intervention(self) -> bool:
        if not self._latest_status.get("connected", False):
            return False
        if self._latest_status.get("hold_reason"):
            return False
        behavior = str(self._latest_status.get("behavior", "")).lower()
        return behavior not in {"hold", "manual", "escort", "intervene"}

    async def _run_care_sequence(self, track_id: str) -> None:
        self._event(
            "TRIAGE",
            track_id,
            "检测到持续疲劳信号 | Sustained fatigue signal detected",
        )

        behavior = str(self._latest_status.get("behavior", "")).lower()
        if behavior == "follow":
            await self._call_skill("stop_following")

        self._event(
            "APPROACH",
            track_id,
            "正在接近并抬高相机 | Approaching and raising camera",
        )
        intervene = await self._call_skill(
            "potential_detected", {"query": "tired person"}
        )
        if not intervene.ok:
            self._event(
                "RESET",
                track_id,
                f"接近请求被拒绝 | Intervention refused: {intervene.text}",
            )
            return

        self._event(
            "DIAGNOSE",
            track_id,
            "近距离疲劳确认完成 | Close-range fatigue check complete",
        )
        if not self._escort_evidence_is_sustained(track_id):
            self._event(
                "RESET",
                track_id,
                "证据未持续，返回巡逻 | Evidence did not persist; returning to patrol",
            )
            return

        self._event(
            "PRESCRIBE",
            track_id,
            "建议休息并请求护送 | Recommending rest and requesting escort",
        )
        self._event(
            "ESCORT",
            track_id,
            "护送前往最近休息区 | Escorting to the nearest sleeping area",
        )
        escort = await self._call_skill("escort_to_sleeping_area")
        if not escort.ok:
            self._event(
                "RESET",
                track_id,
                f"护送请求被拒绝 | Escort refused: {escort.text}",
            )
            return
        self._event(
            "NAP_REGISTERED",
            track_id,
            "已抵达休息区，开始安静休息 | Arrived at the sleeping area; rest started",
        )

    async def _run_intake_escort(self, response_id: str, track_id: str) -> None:
        self._event(
            "ESCORT",
            track_id,
            "访客已确认，护送前往休息区 | Visitor confirmed; starting escort",
        )
        escort = await self._call_skill("escort_to_sleeping_area")
        status = "escorted" if escort.ok else "pending"
        if self._update_intake_status is not None:
            await asyncio.to_thread(
                self._update_intake_status, response_id, status
            )
        if not escort.ok:
            self._event(
                "RESET",
                track_id,
                f"护送请求被拒绝 | Escort refused: {escort.text}",
            )
            return
        self._event(
            "NAP_REGISTERED",
            track_id,
            "已抵达休息区，开始安静休息 | Arrived at the sleeping area; rest started",
        )

    def _escort_evidence_is_sustained(self, track_id: str) -> bool:
        matching = [
            item
            for item in reversed(self._recent)
            if item.track_id == track_id
        ][: self._consecutive_windows]
        if len(matching) < self._consecutive_windows:
            return False
        return all(
            item.fatigue_score >= self._escort_score
            and item.confidence >= self._min_confidence
            and item.quality >= self._min_quality
            and item.observation_seconds >= self._min_observation_seconds
            for item in matching
        )

    async def _call_skill(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> SkillResult:
        return await asyncio.to_thread(
            self._call_skill_sync, name, arguments or {}
        )

    def _call_skill_sync(self, name: str, arguments: dict[str, Any]) -> SkillResult:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        try:
            response = requests.post(self._mcp_url, json=payload, timeout=125)
            response.raise_for_status()
            body = response.json()
            if "error" in body:
                return SkillResult(False, str(body["error"]))
            parts = (body.get("result") or {}).get("content") or []
            text = "\n".join(
                str(part.get("text", ""))
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
            refused = text.startswith(_REFUSAL_PREFIXES)
            return SkillResult(not refused, text or f"{name} completed")
        except Exception as exc:  # noqa: BLE001 - converted to policy result
            logger.warning("robot skill %s failed: %s", name, exc)
            return SkillResult(False, str(exc))

    def _event(self, state: str, track_id: str | None, detail: str) -> None:
        if self._publish_event is None:
            return
        self._publish_event(
            PolicyEvent(
                ts=time.time(),
                state=state,
                target_person=track_id,
                utterance=_STATE_UTTERANCES.get(state),
                detail=detail,
            )
        )

    def _poll_robot_status(self) -> dict[str, Any] | None:
        try:
            response = requests.get(self._status_url, timeout=2)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception as exc:  # noqa: BLE001 - robot can be offline during dev
            logger.debug("robot status unavailable: %s", exc)
            return None

    def _post_assessment(self, assessment: FatigueAssessment) -> None:
        try:
            response = requests.post(
                self._assessment_url,
                json=fatigue_assessment_to_dict(assessment),
                timeout=2,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - audit log still preserves it
            logger.debug("robot assessment endpoint unavailable: %s", exc)

    def _append_assessment(self, assessment: FatigueAssessment) -> None:
        if self._assessment_path is None:
            return
        self._assessment_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(fatigue_assessment_to_dict(assessment), separators=(",", ":"))
        with self._assessment_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _expire_missing_tracks(self, now: float) -> None:
        stale_after = max(30.0, self._window_seconds * 3)
        stale = [
            track_id
            for track_id, last_seen in self._last_seen.items()
            if now - last_seen > stale_after
        ]
        for track_id in stale:
            self._last_seen.pop(track_id, None)
            self._track_started.pop(track_id, None)
            self._streaks.pop(track_id, None)
