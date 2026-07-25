from __future__ import annotations

import time

from app.contracts import FatigueFactors, FatigueFrame, PolicyEvent
from app.services.ledger_memory import LedgerMemory
from app.services.policy_events import LivePolicyEventSource


def _tired_frame() -> FatigueFrame:
    return FatigueFrame(
        ts=time.time(),
        person_id="track-4",
        bbox=(20, 20, 180, 220),
        score=78.0,
        confidence=0.9,
        quality=0.85,
        factors=FatigueFactors(
            perclos=0.3,
            blink_ms_p50=250.0,
            blink_ms_p90=500.0,
            nod_count=1,
            yawn_count=0,
            slump_deg=12.0,
            eye_cnn_perclos=-1.0,
            movement_entropy=0.2,
            sedentary_hours=1.0,
        ),
        calib_state="full",
        scorer="test",
    )


def test_nap_registration_appears_in_ledger_and_route_plan() -> None:
    ledger = LedgerMemory()
    event = PolicyEvent(
        ts=time.time(),
        state="NAP_REGISTERED",
        target_person="track-4",
        utterance="nap_registered_01",
        detail="rest area reached",
    )

    ledger.record_event(event)

    snapshot = ledger.get_ledger()
    assert snapshot["nap_count"] == 1
    assert snapshot["active_naps"][0]["person_id"] == "track-4"
    assert 1190 <= snapshot["active_naps"][0]["remaining_s"] <= 1200
    assert ledger.build_plan([])["stops"][0]["tag"] == "round:track-4"


def test_audio_cue_is_emitted_only_on_state_transition() -> None:
    source = LivePolicyEventSource(
        _tired_frame,
        list,
        heartbeat_seconds=0.0,
    )

    source.tick()
    source.tick()

    events = list(source.subscribe())
    assert events[-2].state == "PRESCRIBE"
    assert events[-2].utterance == "prescribe_01"
    assert events[-1].state == "PRESCRIBE"
    assert events[-1].utterance is None
