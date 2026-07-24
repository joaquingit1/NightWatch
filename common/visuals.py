from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .attention import LEFT_IRIS, RIGHT_IRIS
from .metrics import LEFT_EYE, MOUTH, RIGHT_EYE, FatigueState, pixel_landmarks


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def parse_source(source: str) -> int | str:
    return int(source) if source.isdecimal() else source


def is_image_source(source: str) -> bool:
    return Path(source).suffix.lower() in IMAGE_SUFFIXES and Path(source).is_file()


def draw_landmark_subset(
    frame: np.ndarray,
    normalized_landmarks: np.ndarray,
    *,
    offset: tuple[int, int] = (0, 0),
    source_size: tuple[int, int] | None = None,
) -> None:
    height, width = frame.shape[:2]
    size = source_size or (width, height)
    points = pixel_landmarks(normalized_landmarks, size)
    points[:, 0] += offset[0]
    points[:, 1] += offset[1]
    for index in (*LEFT_EYE, *RIGHT_EYE, *MOUTH):
        x, y = np.rint(points[index, :2]).astype(int)
        cv2.circle(frame, (x, y), 2, (70, 230, 255), -1, cv2.LINE_AA)
    if len(points) > max(*LEFT_IRIS, *RIGHT_IRIS):
        for index in (*LEFT_IRIS, *RIGHT_IRIS):
            x, y = np.rint(points[index, :2]).astype(int)
            cv2.circle(frame, (x, y), 1, (255, 110, 220), -1, cv2.LINE_AA)


def draw_state(
    frame: np.ndarray,
    state: FatigueState | None,
    *,
    origin: tuple[int, int] = (16, 30),
    detector_label: str = "MediaPipe",
) -> None:
    x, y = origin
    scale = 0.62 if frame.shape[1] >= 640 else max(0.38, 0.62 * frame.shape[1] / 640)
    line_step = max(17, round(40 * scale))
    if state is None:
        lines = [f"{detector_label}: no face"]
        color = (0, 180, 255)
    else:
        label = "DROWSY" if state.drowsy else "ALERT"
        color = (40, 40, 240) if state.drowsy else (50, 210, 70)
        lines = [
            f"{detector_label}  {label}  score={state.fatigue_score:4.0f}",
            f"EAR={state.ear:.3f}  MAR={state.mar:.3f}  PERCLOS={state.perclos:.0%}",
            "reason: " + (", ".join(state.reasons) if state.reasons else "none"),
        ]
    for line in lines:
        cv2.putText(
            frame,
            line,
            (x, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            line,
            (x, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            color,
            1,
            cv2.LINE_AA,
        )
        y += line_step


def make_video_writer(
    output: Path, fps: float, frame_size: tuple[int, int]
) -> cv2.VideoWriter:
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(output), cv2.VideoWriter_fourcc(*"mp4v"), max(fps, 1.0), frame_size
    )
    if not writer.isOpened():
        raise RuntimeError(f"Unable to open video writer: {output}")
    return writer
