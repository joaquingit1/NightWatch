from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .gait_realtime import LEFT_HIP, RIGHT_HIP
from .mediapipe_pose import PoseLandmarks


@dataclass(frozen=True)
class PoseBox:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def diagonal(self) -> float:
        return float(np.hypot(self.width, self.height))


@dataclass(frozen=True)
class PoseDetection:
    pose: PoseLandmarks
    box: PoseBox
    hip_center: np.ndarray


@dataclass
class PoseTrack:
    track_id: int
    box: PoseBox
    hip_center: np.ndarray
    velocity: np.ndarray
    created_timestamp: float
    last_seen_timestamp: float
    pose: PoseLandmarks | None = None

    @property
    def occluded(self) -> bool:
        return self.pose is None


def pose_box(pose: PoseLandmarks, padding: float = 0.04) -> PoseBox:
    confidence = np.minimum(pose.visibility, pose.presence)
    usable = confidence >= 0.20
    points = pose.normalized[usable, :2]
    if len(points) < 4:
        points = pose.normalized[:, :2]
    minimum = np.min(points, axis=0)
    maximum = np.max(points, axis=0)
    return PoseBox(
        x1=float(np.clip(minimum[0] - padding, 0.0, 1.0)),
        y1=float(np.clip(minimum[1] - padding, 0.0, 1.0)),
        x2=float(np.clip(maximum[0] + padding, 0.0, 1.0)),
        y2=float(np.clip(maximum[1] + padding, 0.0, 1.0)),
    )


def make_detection(pose: PoseLandmarks) -> PoseDetection:
    hip_center = 0.5 * (
        pose.normalized[LEFT_HIP, :2] + pose.normalized[RIGHT_HIP, :2]
    )
    return PoseDetection(
        pose=pose,
        box=pose_box(pose),
        hip_center=hip_center.astype(np.float32),
    )


def box_iou(first: PoseBox, second: PoseBox) -> float:
    intersection_width = max(
        0.0, min(first.x2, second.x2) - max(first.x1, second.x1)
    )
    intersection_height = max(
        0.0, min(first.y2, second.y2) - max(first.y1, second.y1)
    )
    intersection = intersection_width * intersection_height
    first_area = first.width * first.height
    second_area = second.width * second.height
    union = first_area + second_area - intersection
    return intersection / union if union > 0.0 else 0.0


class MultiPoseTracker:
    """Greedy pose association using body boxes, hip centers and velocity."""

    def __init__(
        self,
        *,
        maximum_missing_seconds: float = 2.0,
        center_distance_scale: float = 0.9,
        minimum_center_distance: float = 0.10,
        velocity_alpha: float = 0.45,
    ) -> None:
        self.maximum_missing_seconds = maximum_missing_seconds
        self.center_distance_scale = center_distance_scale
        self.minimum_center_distance = minimum_center_distance
        self.velocity_alpha = velocity_alpha
        self.tracks: dict[int, PoseTrack] = {}
        self.next_track_id = 1

    def reset(self) -> None:
        self.tracks.clear()
        self.next_track_id = 1

    def _score(
        self,
        track: PoseTrack,
        detection: PoseDetection,
        timestamp: float,
    ) -> float | None:
        elapsed = max(0.0, timestamp - track.last_seen_timestamp)
        predicted_center = track.hip_center + track.velocity * elapsed
        center_distance = float(
            np.linalg.norm(predicted_center - detection.hip_center)
        )
        distance_limit = max(
            self.minimum_center_distance,
            self.center_distance_scale
            * max(track.box.diagonal, detection.box.diagonal),
        )
        overlap = box_iou(track.box, detection.box)
        if overlap < 0.02 and center_distance > distance_limit:
            return None
        motion_score = max(0.0, 1.0 - center_distance / distance_limit)
        freshness = max(
            0.0, 1.0 - elapsed / max(self.maximum_missing_seconds, 1e-6)
        )
        return 2.0 * overlap + motion_score + 0.15 * freshness

    def update(
        self,
        poses: list[PoseLandmarks],
        timestamp: float,
    ) -> list[PoseTrack]:
        detections = [make_detection(pose) for pose in poses]
        stale_ids = [
            track_id
            for track_id, track in self.tracks.items()
            if timestamp - track.last_seen_timestamp
            > self.maximum_missing_seconds
        ]
        for track_id in stale_ids:
            del self.tracks[track_id]
        for track in self.tracks.values():
            track.pose = None

        candidates: list[tuple[float, int, int]] = []
        for track_id, track in self.tracks.items():
            for detection_index, detection in enumerate(detections):
                score = self._score(track, detection, timestamp)
                if score is not None:
                    candidates.append((score, track_id, detection_index))

        assigned_tracks: set[int] = set()
        assigned_detections: set[int] = set()
        for _score, track_id, detection_index in sorted(
            candidates, reverse=True
        ):
            if (
                track_id in assigned_tracks
                or detection_index in assigned_detections
            ):
                continue
            track = self.tracks[track_id]
            detection = detections[detection_index]
            elapsed = max(timestamp - track.last_seen_timestamp, 1e-3)
            measured_velocity = (
                detection.hip_center - track.hip_center
            ) / elapsed
            track.velocity = (
                self.velocity_alpha * measured_velocity
                + (1.0 - self.velocity_alpha) * track.velocity
            ).astype(np.float32)
            track.box = detection.box
            track.hip_center = detection.hip_center
            track.pose = detection.pose
            track.last_seen_timestamp = timestamp
            assigned_tracks.add(track_id)
            assigned_detections.add(detection_index)

        for detection_index, detection in enumerate(detections):
            if detection_index in assigned_detections:
                continue
            track = PoseTrack(
                track_id=self.next_track_id,
                box=detection.box,
                hip_center=detection.hip_center,
                velocity=np.zeros(2, dtype=np.float32),
                created_timestamp=timestamp,
                last_seen_timestamp=timestamp,
                pose=detection.pose,
            )
            self.tracks[track.track_id] = track
            self.next_track_id += 1

        return [self.tracks[key] for key in sorted(self.tracks)]
