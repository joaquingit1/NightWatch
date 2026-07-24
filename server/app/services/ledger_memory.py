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
        self._seed()

    def _seed(self) -> None:
        self.persons["stub-person-01"] = {
            "person_id": "stub-person-01",
            "name_alias": "夜猫子",
            "adopted_ts": time.time() - 3600,
            "baseline_json": "{}",
        }
        self.record_event(
            PolicyEvent(
                ts=time.time() - 300,
                state="NAP_REGISTERED",
                target_person="stub-person-01",
                utterance="nap_registered_01",
                detail="首次午睡登记 | First nap registered",
            )
        )
        self.record_event(
            PolicyEvent(
                ts=time.time() - 120,
                state="PASS_CHECK",
                target_person="stub-person-01",
                utterance=None,
                detail="呼吸正常，物品未动 | Breathing ok, belongings untouched",
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

    def record_adopt(self, payload: dict[str, Any]) -> dict[str, Any]:
        person_id = payload.get("person_id") or str(uuid.uuid4())
        alias = payload.get("name_alias", "志愿者")
        self.persons[person_id] = {
            "person_id": person_id,
            "name_alias": alias,
            "adopted_ts": time.time(),
            "baseline_json": "{}",
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
        entries = []
        for person in self.persons.values():
            person_naps = [n for n in self.naps if n["person_id"] == person["person_id"]]
            entries.append(
                {
                    "name_alias": person["name_alias"],
                    "peak_score": 74,
                    "nap_count": len(person_naps),
                }
            )
        entries.sort(key=lambda e: e["peak_score"], reverse=True)
        return {"entries": entries}

    def get_plan(self) -> dict[str, Any]:
        return {
            "stops": [
                {"tag": "nap_zone", "eta_s": 45, "kind": "nap"},
                {"tag": "patrol_2", "eta_s": 120, "kind": "patrol"},
                {"tag": "home", "eta_s": 210, "kind": "home"},
            ],
            "updated_ts": time.time(),
        }

    def capture_record_dict(self, record: CaptureRecord) -> dict[str, Any]:
        return asdict(record)
