from __future__ import annotations

from dataclasses import replace

import cv2
import numpy as np
from fastapi.testclient import TestClient

from fatigue_fastapi_service.app.analyzer import AnalyzedFrame
from fatigue_fastapi_service.app.config import ServiceSettings
from fatigue_fastapi_service.app.main import create_app, decode_image


class FakeSession:
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


class FakeEngine:
    def __init__(self, _settings: object) -> None:
        self.closed = False

    def new_session(self) -> FakeSession:
        return FakeSession()

    def close(self) -> None:
        self.closed = True


def encoded_test_frame() -> bytes:
    frame = np.zeros((24, 32, 3), dtype=np.uint8)
    frame[:, :, 1] = 127
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    return encoded.tobytes()


def test_decode_image_rejects_non_image_data() -> None:
    assert decode_image(b"not an image") is None


def test_health_and_websocket_protocol() -> None:
    settings = ServiceSettings.from_env()
    settings = replace(settings, max_frame_pixels=10_000)
    app = create_app(settings, FakeEngine)

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["model_loaded"] is True

        with client.websocket_connect(
            "/v1/streams/detect?mirror=false&annotated=false"
        ) as websocket:
            assert websocket.receive_json()["type"] == "ready"

            websocket.send_bytes(encoded_test_frame())
            result = websocket.receive_json()
            assert result["type"] == "result"
            assert result["sequence"] == 0
            assert result["frame"] == {"width": 32, "height": 24}
            assert result["annotated_frame_follows"] is False

            websocket.send_text('{"type":"ping"}')
            assert websocket.receive_json()["type"] == "pong"

            websocket.send_text('{"type":"reset"}')
            assert websocket.receive_json() == {"type": "reset"}

            websocket.send_bytes(encoded_test_frame())
            assert websocket.receive_json()["sequence"] == 0


def test_annotated_mode_returns_json_then_jpeg() -> None:
    app = create_app(ServiceSettings.from_env(), FakeEngine)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/streams/detect?annotated=true"
        ) as websocket:
            websocket.receive_json()
            websocket.send_bytes(encoded_test_frame())
            result = websocket.receive_json()
            annotated = websocket.receive_bytes()

    assert result["annotated_frame_follows"] is True
    assert decode_image(annotated) is not None
