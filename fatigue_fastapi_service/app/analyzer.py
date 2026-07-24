from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from common.attention import (
    AttentionCalibrator,
    AttentionGeometry,
    extract_attention_geometry,
)
from common.mediapipe_face import FaceLandmarkDetector
from common.metrics import FatigueState, extract_face_metrics
from common.visuals import draw_landmark_subset
from common.yolo_face import FaceBox, YOLOFaceDetector, padded_box
from realtime_camera_fatigue import MultiFaceTracker, put_label

from .config import AnalyzerSettings


@dataclass
class AnalyzedFrame:
    payload: dict[str, Any]
    frame: np.ndarray


class FatigueInferenceEngine:
    """Own the heavyweight models and create isolated temporal sessions."""

    def __init__(self, settings: AnalyzerSettings) -> None:
        self.settings = settings
        self.face_detector = YOLOFaceDetector(
            settings.yolo_model,
            confidence=settings.detection_confidence,
            image_size=settings.image_size,
            device=settings.device,
        )
        self.landmark_detector = FaceLandmarkDetector(settings.landmark_model)
        if settings.warmup:
            side = max(64, settings.image_size)
            warmup_frame = np.zeros((side, side, 3), dtype=np.uint8)
            self.face_detector.detect(warmup_frame)
            self.landmark_detector.detect(warmup_frame)

    def new_session(self) -> "FatigueSession":
        return FatigueSession(self)

    def close(self) -> None:
        self.landmark_detector.close()


class FatigueSession:
    """Keep tracking, calibration, and fatigue history for one video stream."""

    def __init__(self, engine: FatigueInferenceEngine) -> None:
        self.engine = engine
        self.settings = engine.settings
        self.sequence = 0
        self.reset()

    def reset(self) -> None:
        self.tracker = MultiFaceTracker(
            self.settings.thresholds,
            maximum_missing_seconds=self.settings.maximum_missing_seconds,
            calibration_frames=self.settings.calibration_frames,
        )
        self.sequence = 0

    @staticmethod
    def _status(
        state: FatigueState | None, calibrating: bool
    ) -> tuple[str, tuple[int, int, int]]:
        if state is not None and state.drowsy:
            return "DROWSY", (40, 40, 240)
        if state is not None and state.inattentive:
            return "INATTENTIVE", (0, 165, 255)
        if calibrating:
            return "CALIBRATING", (220, 190, 40)
        return "ALERT", (50, 210, 70)

    @staticmethod
    def _box_payload(box: FaceBox) -> dict[str, int | float]:
        return {
            "x1": box.x1,
            "y1": box.y1,
            "x2": box.x2,
            "y2": box.y2,
            "confidence": box.confidence,
        }

    def process(
        self,
        frame: np.ndarray,
        timestamp: float,
        *,
        mirror: bool = False,
        annotate: bool = False,
    ) -> AnalyzedFrame:
        started = time.perf_counter()
        if mirror:
            frame = cv2.flip(frame, 1)
        else:
            frame = frame.copy()

        boxes = [
            box
            for box in self.engine.face_detector.detect(frame)
            if box.x2 - box.x1 >= self.settings.minimum_face_size
            and box.y2 - box.y1 >= self.settings.minimum_face_size
        ]
        boxes = sorted(
            boxes, key=lambda candidate: candidate.confidence, reverse=True
        )[: self.settings.max_faces]
        tracked_faces = self.tracker.update(boxes, timestamp)
        people: list[dict[str, Any]] = []

        for track, detected_box in tracked_faces:
            crop_box = padded_box(
                detected_box, frame.shape, self.settings.face_padding
            )
            crop = frame[
                crop_box.y1 : crop_box.y2, crop_box.x1 : crop_box.x2
            ]
            landmarks = self.engine.landmark_detector.detect(crop)
            state = track.state

            if landmarks is not None:
                if (
                    track.last_landmark_timestamp is not None
                    and timestamp - track.last_landmark_timestamp > 1.0
                ):
                    track.monitor.reset()
                crop_height, crop_width = crop.shape[:2]
                ear, mar = extract_face_metrics(
                    landmarks, (crop_width, crop_height)
                )
                geometry = extract_attention_geometry(
                    landmarks, (crop_width, crop_height)
                )
                if geometry is not None and ear <= (
                    self.settings.thresholds.ear_closed * 1.10
                ):
                    geometry = AttentionGeometry(
                        pitch=geometry.pitch,
                        yaw=geometry.yaw,
                        roll=geometry.roll,
                        gaze_horizontal=None,
                        gaze_vertical=None,
                    )
                relative_geometry = (
                    track.attention_calibrator.update(geometry)
                    if geometry is not None
                    else None
                )
                state = track.monitor.update(
                    timestamp,
                    ear,
                    mar,
                    head_pitch=(
                        relative_geometry.pitch
                        if relative_geometry is not None
                        else None
                    ),
                    head_yaw=(
                        relative_geometry.yaw
                        if relative_geometry is not None
                        else None
                    ),
                    head_roll=(
                        relative_geometry.roll
                        if relative_geometry is not None
                        else None
                    ),
                    gaze_horizontal=(
                        relative_geometry.gaze_horizontal
                        if relative_geometry is not None
                        else None
                    ),
                    gaze_vertical=(
                        relative_geometry.gaze_vertical
                        if relative_geometry is not None
                        else None
                    ),
                )
                track.state = state
                track.last_landmark_timestamp = timestamp
                if annotate:
                    draw_landmark_subset(
                        frame,
                        landmarks,
                        offset=(crop_box.x1, crop_box.y1),
                        source_size=(crop_width, crop_height),
                    )

            calibrating = not track.attention_calibrator.ready
            status, color = self._status(state, calibrating)
            if annotate:
                cv2.rectangle(
                    frame,
                    (detected_box.x1, detected_box.y1),
                    (detected_box.x2, detected_box.y2),
                    color,
                    2,
                )
                score = state.fatigue_score if state is not None else 0.0
                label_y = (
                    detected_box.y1 - 7
                    if detected_box.y1 >= 28
                    else detected_box.y1 + 20
                )
                put_label(
                    frame,
                    f"ID {track.track_id} {status} {score:.0f}",
                    (detected_box.x1, label_y),
                    color,
                )

            people.append(
                {
                    "track_id": track.track_id,
                    "status": status,
                    "bbox": self._box_payload(detected_box),
                    "landmarks_detected": landmarks is not None,
                    "calibrating": calibrating,
                    "calibration_progress": (
                        track.attention_calibrator.progress
                    ),
                    "state": state.to_dict() if state is not None else None,
                }
            )

        drowsy_count = sum(
            person["status"] == "DROWSY" for person in people
        )
        inattentive_count = sum(
            person["status"] == "INATTENTIVE" for person in people
        )
        sequence = self.sequence
        self.sequence += 1
        height, width = frame.shape[:2]
        payload: dict[str, Any] = {
            "type": "result",
            "sequence": sequence,
            "timestamp": timestamp,
            "frame": {"width": width, "height": height},
            "summary": {
                "face_count": len(people),
                "drowsy_count": drowsy_count,
                "inattentive_count": inattentive_count,
            },
            "people": people,
            "processing_ms": (time.perf_counter() - started) * 1000.0,
        }
        return AnalyzedFrame(payload=payload, frame=frame)
