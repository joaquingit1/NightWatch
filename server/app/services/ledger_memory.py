from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any

from app.contracts import CaptureRecord, PolicyEvent


class LedgerMemory:
    def __init__(self) -> None:
        self.persons: dict[str, dict[str, Any]] = {}
        self.naps: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.passes: list[dict[str, Any]] = []
        self.peak_scores: dict[str, float] = {}

    def observe_score(self, person_id: str, score: float) -> None:
        current = self.peak_scores.get(person_id, 0.0)
        if score > current:
            self.peak_scores[person_id] = score

    def register_intake(
        self,
        *,
        session_id: str,
        name_alias: str | None,
        tiredness: str,
        wants_escort: bool,
    ) -> None:
        alias = name_alias or f"访客 {session_id[:6]}"
        self.persons[session_id] = {
            "person_id": session_id,
            "name_alias": alias,
            "adopted_ts": time.time(),
            "baseline_json": "{}",
            "source": "intake",
        }
        baseline = 62.0 if tiredness == "tired" else 28.0
        self.peak_scores[session_id] = max(self.peak_scores.get(session_id, 0.0), baseline)
        if wants_escort:
            self.record_event(
                PolicyEvent(
                    ts=time.time(),
                    state="ESCORT",
                    target_person=session_id,
                    utterance="escort_01",
                    detail=f"{alias} 请求护送休息 | {alias} requested escort to rest area",
                )
            )
        elif tiredness == "tired":
            self.record_event(
                PolicyEvent(
                    ts=time.time(),
                    state="TRIAGE",
                    target_person=session_id,
                    utterance="triage_01",
                    detail=f"{alias} 自报疲劳 | {alias} self-reported tiredness",
                )
            )

    def record_event(self, event: PolicyEvent) -> None:
        self.events.append(
            {
                "ts": event.ts,
                "state": event.state,
                "person_id": event.target_person,
                "detail": event.detail,
            }
        )
        if event.state == "NAP_REGISTERED" and event.target_person:
            self.naps.append(
                {
                    "nap_id": str(uuid.uuid4()),
                    "person_id": event.target_person,
                    "start_ts": event.ts,
                    "onset_ts": event.ts + 120,
                    "wake_deadline": event.ts + 1200,
                    "woke_ts": None,
                    "outcome": None,
                }
            )
        if event.state == "PASS_CHECK" and event.target_person:
            self.passes.append(
                {
                    "person_id": event.target_person,
                    "ts": event.ts,
                    "detail": event.detail,
                }
            )

    def record_adopt(self, payload: dict[str, Any]) -> dict[str, Any]:
        person_id = payload.get("person_id") or str(uuid.uuid4())
        alias = payload.get("name_alias", "志愿者")
        self.persons[person_id] = {
            "person_id": person_id,
            "name_alias": alias,
            "adopted_ts": time.time(),
            "baseline_json": "{}",
            "source": "adopt",
        }
        self.record_event(
            PolicyEvent(
                ts=time.time(),
                state="TRIAGE",
                target_person=person_id,
                utterance="adopt_01",
                detail=f"{alias} 领养守夜犬 | {alias} adopted Night Watch",
            )
        )
        return {"person_id": person_id, "name_alias": alias}

    def record_capture(self, record: CaptureRecord) -> dict[str, Any]:
        self.record_event(
            PolicyEvent(
                ts=time.time(),
                state="DIAGNOSE",
                target_person=record.person_id,
                utterance=None,
                detail=f"采集会话 {record.session_id} KSS={record.kss} | Capture session recorded",
            )
        )
        return {"session_id": record.session_id, "stored": True}

    def record_outcome(self, payload: dict[str, Any]) -> dict[str, Any]:
        nap_id = payload.get("nap_id")
        outcome = payload.get("outcome", "better")
        for nap in self.naps:
            if nap_id is None or nap["nap_id"] == nap_id:
                nap["outcome"] = outcome
                nap["woke_ts"] = time.time()
                break
        self.record_event(
            PolicyEvent(
                ts=time.time(),
                state="CELEBRATE",
                target_person=payload.get("person_id"),
                utterance="celebrate_01",
                detail=f"醒来反馈: {outcome} | Wake outcome: {outcome}",
            )
        )
        return {"ok": True, "outcome": outcome}

    def get_ledger(self) -> dict[str, Any]:
        return {
            "events": list(self.events),
            "nap_count": len(self.naps),
            "pass_count": len(self.passes),
        }

    def get_leaderboard(self) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        person_ids = set(self.peak_scores) | set(self.persons)
        for person_id in person_ids:
            peak = self.peak_scores.get(person_id, 0.0)
            if peak <= 0:
                continue
            person = self.persons.get(person_id, {})
            alias = person.get("name_alias")
            if not alias:
                if person_id.startswith("track-"):
                    alias = f"访客 {person_id.split('-', 1)[-1]}"
                else:
                    alias = f"访客 {person_id[:6]}"
            nap_count = len([n for n in self.naps if n["person_id"] == person_id])
            entries.append(
                {
                    "name_alias": alias,
                    "peak_score": round(peak),
                    "nap_count": nap_count,
                }
            )
        entries.sort(key=lambda entry: entry["peak_score"], reverse=True)
        return {"entries": entries[:20]}

    def build_plan(self, pending_escorts: list[Any]) -> dict[str, Any]:
        stops: list[dict[str, Any]] = []
        eta = 20.0

        for escort in pending_escorts:
            alias = getattr(escort, "name_alias", None) or f"guest-{escort.session_id[:6]}"
            stops.append(
                {
                    "tag": f"rest_area:{alias}",
                    "eta_s": eta,
                    "kind": "nap",
                }
            )
            eta += 75.0

        active_naps = [nap for nap in self.naps if nap.get("woke_ts") is None]
        for nap in active_naps:
            person = self.persons.get(nap["person_id"], {})
            alias = person.get("name_alias") or nap["person_id"][:8]
            stops.append(
                {
                    "tag": f"round:{alias}",
                    "eta_s": eta,
                    "kind": "patrol",
                }
            )
            eta += 60.0

        if not stops:
            stops.append(
                {
                    "tag": "booth_patrol",
                    "eta_s": 15.0,
                    "kind": "patrol",
                }
            )

        stops.append(
            {
                "tag": "booth_home",
                "eta_s": eta + 90.0,
                "kind": "home",
            }
        )
        return {"stops": stops, "updated_ts": time.time()}

    def capture_record_dict(self, record: CaptureRecord) -> dict[str, Any]:
        return asdict(record)
