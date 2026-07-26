from __future__ import annotations

import asyncio
import threading

import cv2
import numpy as np
import pytest
from app.routers import lidar
from app.services.frame_source import MjpegFrameSource


def _detached_mjpeg_source(jpeg: bytes) -> MjpegFrameSource:
    source = object.__new__(MjpegFrameSource)
    source.width = 64
    source.height = 32
    source._lock = threading.Lock()
    source._decode_lock = threading.Lock()
    source._latest_frame = None
    source._latest_jpeg = jpeg
    source._latest_jpeg_seq = 1
    source._decoded_jpeg_seq = -1
    source._error_message = None
    return source


def test_mjpeg_source_decodes_each_upstream_frame_at_most_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = np.full((32, 64, 3), 127, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    source = _detached_mjpeg_source(encoded.tobytes())

    original = cv2.imdecode
    calls = 0

    def counted_decode(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(cv2, "imdecode", counted_decode)
    first = source.get_pov_frame()
    second = source.get_pov_frame()

    assert first.shape == (32, 64, 3)
    assert second.shape == first.shape
    assert calls == 1


def test_annotated_stream_fans_out_the_model_jpeg_without_reencoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jpeg = b"\xff\xd8one-model-render\xff\xd9"
    source = _detached_mjpeg_source(jpeg)
    source._annotated_sample_provider = lambda: (jpeg, 123.5)

    def unexpected_encode(*_args, **_kwargs):
        raise AssertionError("cached model annotation must not be re-encoded")

    monkeypatch.setattr(cv2, "imencode", unexpected_encode)

    assert source.get_jpeg_sample(annotated=True) == (jpeg, 123.5)
    assert source.get_jpeg_sample(annotated=True) == (jpeg, 123.5)


def test_lidar_relay_disables_websocket_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_options: dict[str, object] = {}

    class Upstream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def __aiter__(self):
            async def messages():
                yield b"cloud"

            return messages()

    def fake_connect(_url: str, **kwargs):
        connect_options.update(kwargs)
        return Upstream()

    class Browser:
        async def send_text(self, _message: str) -> None:
            return None

        async def send_bytes(self, _message: bytes) -> None:
            raise asyncio.CancelledError

    monkeypatch.setattr(lidar, "connect", fake_connect)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(lidar._relay_upstream(Browser(), "ws://robot/map"))

    assert connect_options["compression"] is None
