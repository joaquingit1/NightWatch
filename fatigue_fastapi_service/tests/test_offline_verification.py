"""Offline verification harness for the fatigue model service.

Runs the REAL inference engine (YOLOv8-face + MediaPipe Face Landmarker)
in-process through the FastAPI TestClient, so nothing here touches the live
service on port 8001 or its single active stream.

Verifies, against real frames:
- a frame with real faces yields bbox + landmarks_detected + attention data,
- an empty frame yields a clean zero-face result (NO_FACE path downstream),
- the payload carries every field name that the booth server's
  server/app/services/live_scorer.py reads when building a
  server/app/contracts.py FatigueFrame.

The face image is the photo bundled with the ultralytics package
(assets/zidane.jpg: two clearly visible faces), so no webcam or network
access is needed.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
import ultralytics
from fastapi.testclient import TestClient

from fatigue_fastapi_service.app.analyzer import AnalyzedFrame
from fatigue_fastapi_service.app.config import ServiceSettings
from fatigue_fastapi_service.app.main import create_app

FACE_IMAGE = Path(ultralytics.__file__).parent / "assets" / "zidane.jpg"

# Field names server/app/services/live_scorer.py reads. If the model payload
# renames or drops any of these, the booth server silently falls back to
# zeros, so lock them down here.
TOP_LEVEL_FIELDS = {
    "type",
    "sequence",
    "timestamp",
    "frame",
    "summary",
    "model",
    "people",
    "processing_ms",
}
PERSON_FIELDS = {
    "track_id",
    "status",
    "bbox",
    "quality",
    "landmarks_detected",
    "calibrating",
    "calibration_progress",
    "looking_at_camera",
    "attention",
    "state",
}
BBOX_FIELDS = {"x1", "y1", "x2", "y2", "confidence"}
ATTENTION_FIELDS = {"head_pitch", "head_yaw", "gaze_horizontal", "gaze_vertical"}
STATE_FIELDS = {
    "fatigue_score",
    "perclos",
    "blink_duration_ms_p50",
    "blink_duration_ms_p90",
    "nod_count",
    "yawn_count",
    "head_pitch",
    "movement_entropy",
    "sedentary_hours",
}


def encode_bgr(frame: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    return encoded.tobytes()


@pytest.fixture(scope="module")
def real_client():
    settings = ServiceSettings.from_env()
    # Skip the warmup pass: the assertions below exercise real inference.
    settings = replace(settings, analyzer=replace(settings.analyzer, warmup=False))
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def _assert_result_contract(result: dict[str, Any]) -> None:
    missing = TOP_LEVEL_FIELDS - result.keys()
    assert not missing, f"payload missing top-level fields: {missing}"
    assert result["type"] == "result"
    assert set(result["frame"]) == {"width", "height"}
    assert {"face_count", "drowsy_count", "inattentive_count"} <= set(
        result["summary"]
    )
    assert "version" in result["model"]


def test_real_engine_detects_face_with_landmarks(real_client: TestClient) -> None:
    face_frame = cv2.imread(str(FACE_IMAGE))
    assert face_frame is not None, f"missing test image {FACE_IMAGE}"
    face_bytes = encode_bgr(face_frame)
    height, width = face_frame.shape[:2]

    with real_client.websocket_connect("/v1/streams/detect") as websocket:
        ready = websocket.receive_json()
        assert ready["type"] == "ready"
        assert ready["protocol_version"] == 1

        # A few frames so tracking and per-person state build up. The
        # transport uses send_json, so this also proves the payload is
        # strictly JSON-serializable (no numpy scalars leaking through).
        for _ in range(3):
            websocket.send_bytes(face_bytes)
            result = websocket.receive_json()

        _assert_result_contract(result)
        assert result["frame"] == {"width": width, "height": height}
        assert result["summary"]["face_count"] >= 1
        assert len(result["people"]) == result["summary"]["face_count"]

        with_landmarks = []
        for person in result["people"]:
            missing = PERSON_FIELDS - person.keys()
            assert not missing, f"person missing fields: {missing}"
            bbox = person["bbox"]
            assert BBOX_FIELDS <= bbox.keys()
            assert 0 <= bbox["x1"] < bbox["x2"] <= width
            assert 0 <= bbox["y1"] < bbox["y2"] <= height
            assert 0.0 < bbox["confidence"] <= 1.0
            assert 0.0 <= person["quality"] <= 1.0
            assert isinstance(person["track_id"], int)
            assert person["status"] in {
                "ALERT",
                "CALIBRATING",
                "DROWSY",
                "INATTENTIVE",
            }
            if person["landmarks_detected"]:
                with_landmarks.append(person)

        assert with_landmarks, "no face produced MediaPipe landmarks"
        primary = with_landmarks[0]
        attention = primary["attention"]
        assert attention is not None
        assert ATTENTION_FIELDS <= attention.keys()
        assert isinstance(attention["head_pitch"], float)
        assert isinstance(attention["head_yaw"], float)

        state = primary["state"]
        assert state is not None
        missing_state = STATE_FIELDS - state.keys()
        assert not missing_state, f"state missing fields: {missing_state}"
        assert 0.0 <= state["fatigue_score"] <= 100.0
        assert 0.0 <= state["perclos"] <= 1.0


def test_real_engine_returns_zero_faces_for_empty_frame(
    real_client: TestClient,
) -> None:
    empty = np.zeros((240, 320, 3), dtype=np.uint8)
    empty[:, :, 1] = 96  # flat green wall, nothing face-like

    with real_client.websocket_connect("/v1/streams/detect") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_bytes(encode_bgr(empty))
        result = websocket.receive_json()

    _assert_result_contract(result)
    assert result["summary"]["face_count"] == 0
    assert result["people"] == []
    # live_scorer maps an empty people list to its NO_FACE idle frame.


class _FakeSession:
    def __init__(self) -> None:
        self.sequence = 0

    def reset(self) -> None:
        self.sequence = 0

    def process(
        self,
        frame: np.ndarray,
        timestamp: float,
        *,
        mirror: bool,
        annotate: bool,
    ) -> AnalyzedFrame:
        sequence = self.sequence
        self.sequence += 1
        height, width = frame.shape[:2]
        return AnalyzedFrame(
            payload={
                "type": "result",
                "sequence": sequence,
                "timestamp": timestamp,
                "frame": {"width": width, "height": height},
                "summary": {
                    "face_count": 0,
                    "drowsy_count": 0,
                    "inattentive_count": 0,
                },
                "people": [],
                "processing_ms": 0.1,
            },
            frame=frame,
        )


class _FakeEngine:
    def __init__(self, _settings: object) -> None:
        pass

    def new_session(self) -> _FakeSession:
        return _FakeSession()

    def close(self) -> None:
        pass


def _tiny_frame_bytes() -> bytes:
    return encode_bgr(np.zeros((16, 16, 3), dtype=np.uint8))


def test_single_stream_slot_rejects_second_and_recovers() -> None:
    app = create_app(ServiceSettings.from_env(), _FakeEngine)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/streams/detect") as first:
            assert first.receive_json()["type"] == "ready"

            # A concurrent second connect must be refused without touching
            # the active stream.
            with client.websocket_connect("/v1/streams/detect") as second:
                rejection = second.receive_json()
                assert rejection["type"] == "error"
                assert rejection["code"] == "stream_capacity_reached"

            first.send_bytes(_tiny_frame_bytes())
            assert first.receive_json()["type"] == "result"

        # After the first client disconnects the slot must be released.
        with client.websocket_connect("/v1/streams/detect") as third:
            assert third.receive_json()["type"] == "ready"
            third.send_bytes(_tiny_frame_bytes())
            assert third.receive_json()["type"] == "result"


def test_invalid_frame_error_then_stream_continues() -> None:
    app = create_app(ServiceSettings.from_env(), _FakeEngine)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/streams/detect") as websocket:
            assert websocket.receive_json()["type"] == "ready"

            websocket.send_bytes(b"definitely not a jpeg")
            error = websocket.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "invalid_frame"

            # One bad frame must not kill the stream (live_scorer keeps the
            # same connection for the whole session).
            websocket.send_bytes(_tiny_frame_bytes())
            assert websocket.receive_json()["type"] == "result"
