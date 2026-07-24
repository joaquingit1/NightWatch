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
    "No Bedroom is available",
    "Cannot set Bedroom:",
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


@dataclass
class InteractionSession:
    interaction_id: str
    track_id: str
    source: str
    started_ts: float
    deadline_ts: float
    response: dict[str, Any] | None = None


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
        intake_timeout_seconds: float = 180.0,
        update_intake_status: Callable[[str, str], Any] | None = None,
        set_detection_enabled: Callable[[bool, str | None], Any] | None = None,
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
        self._intake_timeout_seconds = max(1.0, intake_timeout_seconds)
        self._update_intake_status = update_intake_status
        self._set_detection_enabled = set_detection_enabled

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
        self._fatigue_auto_takeover = False
        self._detection_enabled = True
        self._detection_pause_reason: str | None = None
        self._interaction_state = "idle"
        self._last_manual_input_seq: int | None = None
        self._last_control_mode: str | None = None
        self._active_interaction: InteractionSession | None = None
        self._interaction_event = asyncio.Event()

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
                "fatigue_auto_takeover": self._fatigue_auto_takeover,
                "fatigue_detection": (
                    "enabled" if self._detection_enabled else "disabled"
                ),
                "fatigue_pause_reason": self._detection_pause_reason,
                "interaction_state": self._interaction_state,
                "active_interaction": self.active_interaction(),
            }
        )
        return status

    def set_fatigue_auto_takeover(self, enabled: bool) -> dict[str, Any]:
        self._fatigue_auto_takeover = bool(enabled)
        return {
            "enabled": self._fatigue_auto_takeover,
            "control_mode": self._latest_status.get("control_mode"),
        }

    def active_interaction(self) -> dict[str, Any] | None:
        session = self._active_interaction
        if session is None:
            return None
        return {
            "interaction_id": session.interaction_id,
            "track_id": session.track_id,
            "source": session.source,
            "started_ts": session.started_ts,
            "deadline_ts": session.deadline_ts,
            "remaining_s": max(0.0, session.deadline_ts - time.time()),
            "bound": session.response is not None,
        }

    def bind_intake_response(
        self, interaction_id: str | None, response: dict[str, Any]
    ) -> bool:
        """Bind only the first valid response to the one live interaction."""
        session = self._active_interaction
        if (
            session is None
            or not interaction_id
            or interaction_id != session.interaction_id
            or session.response is not None
            or time.time() > session.deadline_ts
            or self._interaction_state != "waiting_form"
        ):
            return False
        session.response = dict(response)
        self._interaction_event.set()
        return True

    def request_operator_approach(self) -> bool:
        if self._policy_task is not None and not self._policy_task.done():
            return False
        if not self._robot_accepts_intervention(operator_requested=True):
            return False
        self._policy_task = asyncio.create_task(
            self._run_care_sequence(
                "operator-nearest",
                query="person",
                source="operator",
            )
        )
        return True

    def cancel_interaction(self) -> bool:
        task = self._policy_task
        if task is None or task.done():
            return False
        task.cancel()
        return True

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

    async def set_bedroom_at(
        self, world_x: float, world_y: float, world_z: float = 0.0
    ) -> SkillResult:
        if not self._latest_status.get("connected", False):
            return SkillResult(False, "Robot is offline")
        return await self._call_skill(
            "set_bedroom_at",
            {"world_x": world_x, "world_y": world_y, "world_z": world_z},
        )

    async def set_bedroom_here(self) -> SkillResult:
        if not self._latest_status.get("connected", False):
            return SkillResult(False, "Robot is offline")
        return await self._call_skill("set_bedroom_here")

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

        manual_input_seq = int(self._latest_status.get("manual_input_seq", 0))
        control_mode = str(
            self._latest_status.get("control_mode", "autonomous")
        ).lower()
        if control_mode == "manual" and self._last_control_mode != "manual":
            self._fatigue_auto_takeover = False
        self._last_control_mode = control_mode
        if (
            self._last_manual_input_seq is not None
            and manual_input_seq != self._last_manual_input_seq
            and self._policy_task is not None
            and not self._policy_task.done()
        ):
            self._policy_task.cancel()
        self._last_manual_input_seq = manual_input_seq
        if (
            self._policy_task is not None
            and not self._policy_task.done()
            and str(self._latest_status.get("mission_mode", "")).lower()
            != "cruise"
        ):
            self._policy_task.cancel()

        self._sync_detection_gate()
        if not self._detection_enabled:
            self._streaks.clear()
            self._track_started.clear()
            self._last_seen.clear()
            return

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

    def _robot_accepts_intervention(
        self, *, operator_requested: bool = False
    ) -> bool:
        if not self._latest_status.get("connected", False):
            return False
        if self._latest_status.get("hold_reason"):
            return False
        if str(self._latest_status.get("mission_mode", "")).lower() != "cruise":
            return False
        control_mode = str(
            self._latest_status.get("control_mode", "autonomous")
        ).lower()
        if (
            control_mode == "manual"
            and not operator_requested
            and not self._fatigue_auto_takeover
        ):
            return False
        behavior = str(self._latest_status.get("behavior", "")).lower()
        disallowed = {"hold", "escort", "intervene"}
        if control_mode != "manual":
            disallowed.add("manual")
        return behavior not in disallowed

    def _sync_detection_gate(self) -> None:
        status = self._latest_status
        reason: str | None = None
        if not status.get("connected", False):
            reason = "robot_offline"
        elif str(status.get("mission_mode", "")).lower() != "cruise":
            reason = "exploration_mode"
        elif self._interaction_state not in {
            "idle",
            "scheduled_scan",
            "forced_scan",
        }:
            reason = "interaction_active"
        elif status.get("hold_reason"):
            reason = str(status["hold_reason"]).lower()
        elif str(status.get("behavior", "")).lower() in {
            "escort",
            "intervene",
            "return_home",
            "hold",
        }:
            reason = f"behavior_{str(status.get('behavior')).lower()}"

        enabled = reason is None
        if enabled == self._detection_enabled and reason == self._detection_pause_reason:
            return
        self._detection_enabled = enabled
        self._detection_pause_reason = reason
        if self._set_detection_enabled is not None:
            try:
                self._set_detection_enabled(enabled, reason)
            except Exception:
                logger.exception("failed to update fatigue detector gate")

    async def _set_interaction_state(self, state: str) -> None:
        self._interaction_state = state
        self._sync_detection_gate()
        result = await self._call_skill("set_interaction_state", {"state": state})
        if not result.ok:
            logger.warning("robot interaction-state mirror failed: %s", result.text)

    async def _speak(self, text: str) -> None:
        result = await self._call_skill(
            "speak", {"text": text, "blocking": True}
        )
        if not result.ok:
            logger.warning("robot speech failed: %s", result.text)

    async def _set_intake_status(self, response_id: Any, status: str) -> None:
        if self._update_intake_status is None or not response_id:
            return
        await asyncio.to_thread(
            self._update_intake_status, str(response_id), status
        )

    async def _farewell_and_retreat(self) -> None:
        await self._set_interaction_state("farewell")
        await self._speak("谢谢你。也请注意不要让自己太疲劳，记得适时休息。")
        await self._call_skill(
            "perform_dog_expression", {"expression": "Hello"}
        )
        await self._set_interaction_state("retreating")
        retreat = await self._call_skill(
            "relative_move", {"forward": -0.8, "left": 0.0, "degrees": 0.0}
        )
        if not retreat.ok:
            logger.info("friendly retreat skipped: %s", retreat.text)

    async def _run_care_sequence(
        self,
        track_id: str,
        *,
        query: str = "tired person",
        source: str = "fatigue",
    ) -> None:
        self._event(
            "TRIAGE",
            track_id,
            (
                "操作员请求接近最近的人 | Operator requested nearest-person approach"
                if source == "operator"
                else "检测到持续疲劳信号 | Sustained fatigue signal detected"
            ),
        )
        try:
            await self._set_interaction_state("fatigue_candidate")
            behavior = str(self._latest_status.get("behavior", "")).lower()
            if behavior == "follow":
                await self._call_skill("stop_following")

            await self._set_interaction_state("approaching")
            self._event(
                "APPROACH",
                track_id,
                "正在接近并抬高相机 | Approaching and raising camera",
            )
            skill_args: dict[str, Any] = {"query": query}
            if source == "fatigue":
                locked = next(
                    (
                        assessment
                        for assessment in reversed(self._recent)
                        if assessment.track_id == track_id
                        and assessment.bbox is not None
                    ),
                    None,
                )
                if locked is not None and locked.bbox is not None:
                    skill_args["initial_bbox"] = list(locked.bbox)
            intervene = await self._call_skill(
                "potential_detected", skill_args
            )
            if not intervene.ok:
                self._event(
                    "RESET",
                    track_id,
                    f"接近请求被拒绝 | Intervention refused: {intervene.text}",
                )
                return

            await self._set_interaction_state("offering_form")
            await self._speak(
                "你好，我是守夜犬。你看起来有些疲劳，可以扫描我身上的二维码，"
                "或者用手机碰一下NFC，填写一个简短问卷。如果你需要，我可以带你去休息。"
            )

            now = time.time()
            session = InteractionSession(
                interaction_id=uuid.uuid4().hex,
                track_id=track_id,
                source=source,
                started_ts=now,
                deadline_ts=now + self._intake_timeout_seconds,
            )
            self._active_interaction = session
            self._interaction_event = asyncio.Event()
            await self._set_interaction_state("waiting_form")
            self._event(
                "PRESCRIBE",
                track_id,
                (
                    "等待扫码或NFC问卷 | Waiting for QR or NFC form "
                    f"[interaction={session.interaction_id}]"
                ),
            )
            try:
                await asyncio.wait_for(
                    self._interaction_event.wait(),
                    timeout=self._intake_timeout_seconds,
                )
            except asyncio.TimeoutError:
                self._event(
                    "RESET",
                    track_id,
                    "三分钟内没有问卷，友好告别 | Form timed out; saying goodbye",
                )
                await self._farewell_and_retreat()
                return

            response = session.response or {}
            wants_guide = bool(
                response.get("tiredness") == "tired"
                and response.get("wants_escort")
            )
            if not wants_guide:
                await self._set_intake_status(
                    response.get("response_id"), "declined"
                )
                self._event(
                    "RESET",
                    track_id,
                    (
                        "访客不需要指引，友好告别 | Visitor declined guidance "
                        f"[response={response.get('response_id', 'unknown')}]"
                    ),
                )
                await self._farewell_and_retreat()
                return

            await self._set_interaction_state("escorting")
            await self._speak("好的，请跟着我，我带你去休息。")
            self._event(
                "ESCORT",
                track_id,
                "护送前往Bedroom | Escorting to Bedroom",
            )
            escort = await self._call_skill("escort_to_sleeping_area")
            if not escort.ok:
                await self._set_intake_status(
                    response.get("response_id"), "pending"
                )
                self._event(
                    "RESET",
                    track_id,
                    f"护送请求被拒绝 | Escort refused: {escort.text}",
                )
                await self._farewell_and_retreat()
                return

            await self._set_intake_status(
                response.get("response_id"), "escorted"
            )
            await self._set_interaction_state("arrived")
            await self._speak("已经到休息的地方了，祝你睡个好觉。")
            await self._call_skill(
                "perform_dog_expression", {"expression": "Hello"}
            )
            self._event(
                "NAP_REGISTERED",
                track_id,
                "已抵达Bedroom | Arrived at Bedroom",
            )
            await self._set_interaction_state("retreating")
            await self._call_skill(
                "relative_move", {"forward": -0.8, "left": 0.0, "degrees": 0.0}
            )
        except asyncio.CancelledError:
            self._event(
                "RESET",
                track_id,
                "操作员中断交互 | Interaction cancelled by operator",
            )
            raise
        finally:
            self._active_interaction = None
            self._interaction_event = asyncio.Event()
            self._interaction_state = "idle"
            self._sync_detection_gate()
            try:
                await self._call_skill(
                    "set_interaction_state", {"state": "idle"}
                )
            except asyncio.CancelledError:
                # Cancellation is already the terminal path; best-effort state
                # mirroring must not keep the cancelled task alive.
                pass

    async def _run_intake_escort(self, response_id: str, track_id: str) -> None:
        await self._set_interaction_state("escorting")
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
            await self._set_interaction_state("idle")
            return
        self._event(
            "NAP_REGISTERED",
            track_id,
            "已抵达休息区，开始安静休息 | Arrived at the sleeping area; rest started",
        )
        await self._set_interaction_state("idle")

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
