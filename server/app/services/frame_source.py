from __future__ import annotations

import math
import platform
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

import cv2
import numpy as np
import requests

from app.contracts import FatigueFrame


def _draw_fatigue_overlay(
    frame: np.ndarray,
    fatigue: FatigueFrame,
    frame_height: int,
    label_slot: int = 0,
) -> None:
    x1, y1, x2, y2 = fatigue.bbox
    frame_width = frame.shape[1]
    x1 = max(0, min(frame_width - 1, x1))
    x2 = max(0, min(frame_width - 1, x2))
    y1 = max(0, min(frame_height - 1, y1))
    y2 = max(0, min(frame_height - 1, y2))
    if x2 <= x1 or y2 <= y1:
        return
    color = (61, 214, 198) if fatigue.score < 60 else (93, 93, 237)
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    track_label = (fatigue.person_id or "track-?").replace("track-", "ID ")
    label = (
        f"{track_label}  R {fatigue.score:.0f}  Q {fatigue.quality * 100:.0f}%"
    )
    font_scale = 0.52
    thickness = 1
    (label_width, label_height), baseline = cv2.getTextSize(
        label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
    )
    label_x = min(x1, max(0, frame_width - label_width - 8))
    label_y = max(label_height + 6, y1 - 7 - (label_slot % 3) * 20)
    cv2.rectangle(
        frame,
        (label_x - 3, label_y - label_height - 4),
        (label_x + label_width + 4, label_y + baseline + 3),
        (10, 14, 20),
        -1,
    )
    cv2.putText(
        frame,
        label,
        (label_x, label_y),
        cv2.FONT_HERSHEY_SIMPLEX,
        font_scale,
        color,
        thickness,
        cv2.LINE_AA,
    )
    posture_parts: list[str] = []
    if fatigue.factors.slump_deg >= 8:
        posture_parts.append(f"pitch {fatigue.factors.slump_deg:.0f}")
    if fatigue.factors.nod_count > 0:
        posture_parts.append(f"nod x{fatigue.factors.nod_count}")
    if fatigue.factors.yawn_count > 0:
        posture_parts.append(f"yawn x{fatigue.factors.yawn_count}")
    if posture_parts:
        cv2.putText(
            frame,
            " · ".join(posture_parts),
            (x1, min(frame_height - 12, y2 + 22)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (139, 156, 179),
            1,
            cv2.LINE_AA,
        )
    elif fatigue.confidence < 0.5:
        cv2.putText(
            frame,
            "low confidence",
            (x1, min(frame_height - 12, y2 + 24)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (139, 156, 179),
            1,
            cv2.LINE_AA,
        )


def _draw_all_fatigue_overlays(
    frame: np.ndarray, fatigue: FatigueFrame, frame_height: int
) -> None:
    tracks = fatigue.people or (
        [fatigue] if fatigue.person_id is not None else []
    )
    for label_slot, track in enumerate(tracks):
        if track.confidence >= 0.15 and track.bbox != (0, 0, 0, 0):
            _draw_fatigue_overlay(
                frame, track, frame_height, label_slot=label_slot
            )


class FrameSource(ABC):
    @abstractmethod
    def get_pov_frame(self) -> np.ndarray: ...

    @abstractmethod
    def get_annotated_frame(self) -> np.ndarray: ...

    def close(self) -> None:
        return None


class StubFrameSource(FrameSource):
    def __init__(
        self,
        width: int = 960,
        height: int = 540,
        score_provider: Callable[[], FatigueFrame] | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self._score_provider = score_provider
        self._start = time.time()

    def _base_frame(self) -> np.ndarray:
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:] = (10, 14, 20)
        t = time.time() - self._start
        for i in range(0, self.width, 40):
            offset = int(20 * math.sin(t + i * 0.05))
            cv2.line(frame, (i, 0), (i + offset, self.height), (30, 42, 58), 1)
        return frame

    def _draw_bbox(self, frame: np.ndarray, score: float) -> None:
        t = time.time() - self._start
        cx = int(self.width * 0.5 + 80 * math.sin(t * 0.7))
        cy = int(self.height * 0.45 + 30 * math.cos(t * 0.9))
        w, h = 180, 220
        x1, y1 = cx - w // 2, cy - h // 2
        x2, y2 = cx + w // 2, cy + h // 2
        color = (61, 214, 198) if score < 60 else (93, 93, 237)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            frame,
            f"RestScore {score:.0f}",
            (x1, max(24, y1 - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            color,
            2,
            cv2.LINE_AA,
        )

    def get_pov_frame(self) -> np.ndarray:
        return self._base_frame()

    def get_annotated_frame(self) -> np.ndarray:
        frame = self._base_frame()
        if self._score_provider is not None:
            _draw_all_fatigue_overlays(
                frame, self._score_provider(), self.height
            )
        else:
            score = 35 + 25 * (
                0.5 + 0.5 * math.sin((time.time() - self._start) * 0.4)
            )
            self._draw_bbox(frame, score)
        cv2.putText(
            frame,
            "STUB CAMERA",
            (16, self.height - 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (139, 156, 179),
            1,
            cv2.LINE_AA,
        )
        return frame


class WebcamFrameSource(FrameSource):
    """Owns the single physical camera handle. A dedicated background thread
    continuously grabs, resizes, and mirrors frames into a shared buffer so
    that multiple consumers (MJPEG streaming, live fatigue detection) never
    contend over the same blocking cv2.VideoCapture.read() call -- that
    contention was the cause of the flickering feed."""

    def __init__(
        self,
        device: int | str = 0,
        width: int = 960,
        height: int = 540,
        score_provider: Callable[[], FatigueFrame] | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self._score_provider = score_provider
        backend = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_ANY
        self._cap = cv2.VideoCapture(device, backend)
        if self._cap.isOpened():
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        self._lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._stopped = False
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _capture_loop(self) -> None:
        while not self._stopped:
            if not self._cap.isOpened():
                time.sleep(0.1)
                continue
            ok, frame = self._cap.read()
            if not ok or frame is None:
                time.sleep(0.02)
                continue
            frame = cv2.resize(frame, (self.width, self.height))
            frame = cv2.flip(frame, 1)
            with self._lock:
                self._latest_frame = frame

    def close(self) -> None:
        self._stopped = True
        self._thread.join(timeout=1.0)
        self._cap.release()

    def _error_frame(self, message: str) -> np.ndarray:
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:] = (20, 20, 30)
        cv2.putText(
            frame,
            message,
            (24, self.height // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (139, 156, 179),
            2,
            cv2.LINE_AA,
        )
        return frame

    def _read_frame(self) -> np.ndarray:
        with self._lock:
            frame = self._latest_frame
        if frame is None:
            message = (
                "Camera not available"
                if not self._cap.isOpened()
                else "Waiting for first frame"
            )
            return self._error_frame(message)
        return frame.copy()

    def _draw_overlay(self, frame: np.ndarray, fatigue: FatigueFrame) -> None:
        _draw_fatigue_overlay(frame, fatigue, self.height)

    def get_pov_frame(self) -> np.ndarray:
        return self._read_frame()

    def get_annotated_frame(self) -> np.ndarray:
        frame = self._read_frame()
        if self._score_provider is not None:
            fatigue = self._score_provider()
            _draw_all_fatigue_overlays(frame, fatigue, self.height)
        return frame


class MjpegFrameSource(FrameSource):
    """Pull MJPEG frames from an HTTP multipart stream (robot or Insta360 bridge)."""

    def __init__(
        self,
        url: str,
        width: int = 960,
        height: int = 540,
        score_provider: Callable[[], FatigueFrame] | None = None,
    ) -> None:
        self.url = url
        self.width = width
        self.height = height
        self._score_provider = score_provider
        self._lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._error_message: str | None = "Connecting to robot camera..."
        self._stopped = False
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _capture_loop(self) -> None:
        backoff = 1.0
        buffer = b""
        while not self._stopped:
            try:
                with requests.get(self.url, stream=True, timeout=5) as response:
                    response.raise_for_status()
                    backoff = 1.0
                    self._error_message = None
                    for chunk in response.iter_content(chunk_size=8192):
                        if self._stopped:
                            break
                        if not chunk:
                            continue
                        buffer += chunk
                        while True:
                            start = buffer.find(b"\xff\xd8")
                            end = buffer.find(b"\xff\xd9")
                            if start == -1 or end == -1 or end <= start:
                                break
                            jpeg = buffer[start : end + 2]
                            buffer = buffer[end + 2 :]
                            frame = cv2.imdecode(
                                np.frombuffer(jpeg, dtype=np.uint8),
                                cv2.IMREAD_COLOR,
                            )
                            if frame is None:
                                continue
                            frame = cv2.resize(frame, (self.width, self.height))
                            with self._lock:
                                self._latest_frame = frame
            except Exception:  # noqa: BLE001 - reconnect loop
                self._error_message = "Robot camera unavailable"
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def close(self) -> None:
        self._stopped = True
        self._thread.join(timeout=2.0)

    def _error_frame(self, message: str) -> np.ndarray:
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:] = (20, 20, 30)
        cv2.putText(
            frame,
            message,
            (24, self.height // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (139, 156, 179),
            2,
            cv2.LINE_AA,
        )
        return frame

    def _read_frame(self) -> np.ndarray:
        with self._lock:
            frame = self._latest_frame
            error = self._error_message
        if frame is None:
            return self._error_frame(error or "Waiting for robot camera")
        return frame.copy()

    def get_pov_frame(self) -> np.ndarray:
        return self._read_frame()

    def get_annotated_frame(self) -> np.ndarray:
        frame = self._read_frame()
        if self._score_provider is not None:
            fatigue = self._score_provider()
            _draw_all_fatigue_overlays(frame, fatigue, self.height)
        return frame


RobotCameraFrameSource = MjpegFrameSource


def create_frame_source(
    camera_source: str,
    score_provider: Callable[[], FatigueFrame] | None = None,
    robot_camera_url: str | None = None,
    insta360_mjpeg_url: str | None = None,
) -> FrameSource:
    if camera_source == "stub":
        return StubFrameSource(score_provider=score_provider)

    if camera_source == "insta360":
        url = insta360_mjpeg_url or "http://127.0.0.1:5556/video"
        return MjpegFrameSource(url=url, score_provider=score_provider)

    if camera_source == "robot":
        if not robot_camera_url:
            raise ValueError("robot_camera_url is required when CAMERA_SOURCE=robot")
        return MjpegFrameSource(url=robot_camera_url, score_provider=score_provider)

    device: int | str
    if camera_source == "webcam":
        device = 0
    elif camera_source.isdigit():
        device = int(camera_source)
    else:
        device = camera_source

    return WebcamFrameSource(device=device, score_provider=score_provider)
