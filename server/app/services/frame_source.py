from __future__ import annotations

import math
import platform
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

import cv2
import numpy as np

from app.contracts import FatigueFrame


class FrameSource(ABC):
    @abstractmethod
    def get_pov_frame(self) -> np.ndarray: ...

    @abstractmethod
    def get_annotated_frame(self) -> np.ndarray: ...

    def close(self) -> None:
        return None


class StubFrameSource(FrameSource):
    def __init__(self, width: int = 960, height: int = 540) -> None:
        self.width = width
        self.height = height
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
        score = 35 + 25 * (0.5 + 0.5 * math.sin((time.time() - self._start) * 0.4))
        # self._draw_bbox(frame, score)
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
        x1, y1, x2, y2 = fatigue.bbox
        color = (61, 214, 198) if fatigue.score < 60 else (93, 93, 237)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            frame,
            f"RestScore {fatigue.score:.0f}",
            (x1, max(24, y1 - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            color,
            2,
            cv2.LINE_AA,
        )
        if fatigue.confidence < 0.5:
            cv2.putText(
                frame,
                "low confidence",
                (x1, min(self.height - 12, y2 + 24)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (139, 156, 179),
                1,
                cv2.LINE_AA,
            )

    def get_pov_frame(self) -> np.ndarray:
        return self._read_frame()

    def get_annotated_frame(self) -> np.ndarray:
        frame = self._read_frame()
        # if self._score_provider is not None:
        #     fatigue = self._score_provider()
        #     if fatigue.confidence >= 0.5:
        #         self._draw_overlay(frame, fatigue)
        return frame


def create_frame_source(
    camera_source: str,
    score_provider: Callable[[], FatigueFrame] | None = None,
) -> FrameSource:
    if camera_source == "stub":
        return StubFrameSource()

    device: int | str
    if camera_source == "webcam":
        device = 0
    elif camera_source.isdigit():
        device = int(camera_source)
    else:
        device = camera_source

    return WebcamFrameSource(device=device, score_provider=score_provider)
