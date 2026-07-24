from __future__ import annotations

from app.contracts import fatigue_frame_to_dict
from app.services.live_scorer import _map_result


def _person(track_id: int, score: float, confidence: float, quality: float) -> dict:
    return {
        "track_id": track_id,
        "status": "DROWSY" if score >= 60 else "ALERT",
        "bbox": {
            "x1": 10 * track_id,
            "y1": 20,
            "x2": 10 * track_id + 100,
            "y2": 180,
            "confidence": confidence,
        },
        "quality": quality,
        "landmarks_detected": True,
        "calibrating": False,
        "calibration_progress": 1.0,
        "state": {
            "fatigue_score": score,
            "perclos": 0.3 if score >= 60 else 0.05,
            "blink_duration_ms_p50": 180,
            "blink_duration_ms_p90": 450 if score >= 60 else 220,
            "nod_count": 1 if score >= 60 else 0,
            "yawn_count": 0,
            "head_pitch": -12,
            "movement_entropy": 0.4,
            "sedentary_hours": 0.5,
        },
    }


def test_live_result_preserves_all_model_tracks_and_selects_usable_primary() -> None:
    frame = _map_result(
        {
            "type": "result",
            "sequence": 9,
            "frame": {"width": 960, "height": 540},
            "processing_ms": 42.5,
            "model": {"version": "test-model-v2"},
            "people": [
                _person(1, score=92, confidence=0.95, quality=0.2),
                _person(2, score=72, confidence=0.8, quality=0.8),
            ],
        }
    )

    assert frame.person_id == "track-2"
    assert [person.person_id for person in frame.people] == ["track-1", "track-2"]
    assert frame.quality == 0.8
    assert frame.model_version == "test-model-v2"
    assert frame.processing_ms == 42.5
    assert frame.sequence == 9
    assert frame.source_status == "live"
    serialized = fatigue_frame_to_dict(frame)
    assert len(serialized["people"]) == 2
    assert serialized["people"][1]["people"] == []


def test_live_result_reports_connected_no_face_without_fake_track() -> None:
    frame = _map_result(
        {
            "type": "result",
            "sequence": 3,
            "frame": {"width": 960, "height": 540},
            "processing_ms": 12,
            "people": [],
        }
    )

    assert frame.person_id is None
    assert frame.people == []
    assert frame.source_status == "live"
    assert frame.sequence == 3
