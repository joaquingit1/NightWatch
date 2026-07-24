from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass
from math import hypot

import numpy as np

# Six points in the conventional EAR order: outer corner, upper lid x2,
# inner corner, lower lid x2.
LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)
MOUTH = (78, 13, 308, 14)  # left, upper, right, lower inner-lip points


def _distance(a: np.ndarray, b: np.ndarray) -> float:
    return hypot(float(a[0] - b[0]), float(a[1] - b[1]))


def eye_aspect_ratio(points: np.ndarray) -> float:
    """Return EAR for six ordered 2D eye points."""
    if points.shape != (6, 2):
        raise ValueError(f"Expected eye points with shape (6, 2), got {points.shape}")
    horizontal = _distance(points[0], points[3])
    if horizontal < 1e-8:
        return 0.0
    return (
        _distance(points[1], points[5]) + _distance(points[2], points[4])
    ) / (2.0 * horizontal)


def mouth_aspect_ratio(points: np.ndarray) -> float:
    """Return inner-lip height divided by inner-mouth width."""
    if points.shape != (4, 2):
        raise ValueError(f"Expected mouth points with shape (4, 2), got {points.shape}")
    width = _distance(points[0], points[2])
    if width < 1e-8:
        return 0.0
    return _distance(points[1], points[3]) / width


def pixel_landmarks(
    normalized_landmarks: np.ndarray, image_size: tuple[int, int]
) -> np.ndarray:
    """Convert MediaPipe normalized points to pixel coordinates."""
    width, height = image_size
    result = np.asarray(normalized_landmarks, dtype=np.float32).copy()
    result[:, 0] *= width
    result[:, 1] *= height
    return result


def extract_face_metrics(
    normalized_landmarks: np.ndarray, image_size: tuple[int, int]
) -> tuple[float, float]:
    points = pixel_landmarks(normalized_landmarks, image_size)
    left_ear = eye_aspect_ratio(points[list(LEFT_EYE), :2])
    right_ear = eye_aspect_ratio(points[list(RIGHT_EYE), :2])
    mar = mouth_aspect_ratio(points[list(MOUTH), :2])
    return (left_ear + right_ear) / 2.0, mar


@dataclass(frozen=True)
class FatigueThresholds:
    ear_closed: float = 0.20
    mar_yawn: float = 0.35
    eye_alarm_seconds: float = 1.20
    yawn_alarm_seconds: float = 1.50
    perclos_window_seconds: float = 30.0
    perclos_alarm: float = 0.28
    minimum_perclos_history_seconds: float = 5.0
    head_yaw_away_degrees: float = 25.0
    head_down_pitch_degrees: float = 18.0
    gaze_horizontal_away: float = 0.45
    gaze_vertical_away: float = 0.45
    attention_alarm_seconds: float = 2.0
    head_down_alarm_seconds: float = 1.5
    nod_pitch_degrees: float = 12.0
    nod_return_degrees: float = 5.0
    nod_min_seconds: float = 0.15
    nod_max_seconds: float = 2.0
    nod_window_seconds: float = 10.0
    nod_alarm_count: int = 2
    blink_history_size: int = 40
    movement_window_seconds: float = 20.0
    movement_bins: int = 8
    movement_pose_range_degrees: float = 45.0
    yawn_grace_seconds: float = 0.45


@dataclass(frozen=True)
class FatigueState:
    timestamp: float
    ear: float
    mar: float
    eye_closed: bool
    yawning: bool
    eye_closed_seconds: float
    yawn_seconds: float
    perclos: float
    history_seconds: float
    blink_count: int
    yawn_count: int
    head_pitch: float | None
    head_yaw: float | None
    head_roll: float | None
    gaze_horizontal: float | None
    gaze_vertical: float | None
    attention_away: bool
    attention_away_seconds: float
    head_down: bool
    head_down_seconds: float
    nod_count: int
    recent_nod_count: int
    inattentive: bool
    fatigue_score: float
    drowsy: bool
    reasons: tuple[str, ...]
    blink_duration_ms_p50: float
    blink_duration_ms_p90: float
    movement_entropy: float
    sedentary_hours: float

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["reasons"] = list(self.reasons)
        return data


class DrowsinessMonitor:
    """Fuse eyes, mouth, head pose and gaze into an interpretable temporal state."""

    def __init__(self, thresholds: FatigueThresholds | None = None) -> None:
        self.thresholds = thresholds or FatigueThresholds()
        self._eye_closed_since: float | None = None
        self._yawn_since: float | None = None
        self._yawn_last_open: float | None = None
        self._last_timestamp: float | None = None
        self._history: deque[tuple[float, bool]] = deque()
        self._blink_count = 0
        self._yawn_count = 0
        self._attention_away_since: float | None = None
        self._head_down_since: float | None = None
        self._nod_started_at: float | None = None
        self._nod_events: deque[float] = deque()
        self._nod_count = 0
        self._blink_durations: deque[float] = deque(
            maxlen=self.thresholds.blink_history_size
        )
        self._pose_samples: deque[tuple[float, float, float]] = deque()
        self._session_start: float | None = None

    def reset(self) -> None:
        self.__init__(self.thresholds)

    def _time_weighted_perclos(self, now: float) -> tuple[float, float]:
        if not self._history:
            return 0.0, 0.0
        cutoff = now - self.thresholds.perclos_window_seconds
        while len(self._history) > 1 and self._history[1][0] <= cutoff:
            self._history.popleft()

        start = max(cutoff, self._history[0][0])
        total = max(0.0, now - start)
        if total <= 1e-8:
            return float(self._history[-1][1]), 0.0

        closed_time = 0.0
        samples = list(self._history)
        for index, (sample_time, is_closed) in enumerate(samples):
            segment_start = max(start, sample_time)
            segment_end = now if index + 1 == len(samples) else min(now, samples[index + 1][0])
            if is_closed and segment_end > segment_start:
                closed_time += segment_end - segment_start
        return min(1.0, closed_time / total), total

    @staticmethod
    def _update_duration_start(
        active: bool, timestamp: float, started_at: float | None
    ) -> float | None:
        if active:
            return timestamp if started_at is None else started_at
        return None

    def _blink_percentiles(self) -> tuple[float, float]:
        if not self._blink_durations:
            return 0.0, 0.0
        durations_ms = sorted(seconds * 1000.0 for seconds in self._blink_durations)
        p50 = float(np.percentile(durations_ms, 50))
        p90 = float(np.percentile(durations_ms, 90))
        return p50, p90

    def _movement_entropy(self, now: float) -> float:
        cutoff = now - self.thresholds.movement_window_seconds
        while self._pose_samples and self._pose_samples[0][0] < cutoff:
            self._pose_samples.popleft()
        if len(self._pose_samples) < 5:
            return 0.0

        bins = max(2, self.thresholds.movement_bins)
        pose_range = self.thresholds.movement_pose_range_degrees
        yaw_edges = np.linspace(-pose_range, pose_range, bins + 1)
        pitch_edges = np.linspace(-pose_range, pose_range, bins + 1)
        yaw_values = [sample[1] for sample in self._pose_samples]
        pitch_values = [sample[2] for sample in self._pose_samples]
        histogram, _, _ = np.histogram2d(
            yaw_values, pitch_values, bins=(yaw_edges, pitch_edges)
        )
        total = histogram.sum()
        if total <= 0:
            return 0.0
        probabilities = histogram.flatten() / total
        nonzero = probabilities[probabilities > 0]
        entropy = float(-np.sum(nonzero * np.log2(nonzero)))
        max_entropy = math.log2(bins * bins)
        return min(1.0, entropy / max_entropy) if max_entropy > 0 else 0.0

    def update(
        self,
        timestamp: float,
        ear: float,
        mar: float,
        *,
        head_pitch: float | None = None,
        head_yaw: float | None = None,
        head_roll: float | None = None,
        gaze_horizontal: float | None = None,
        gaze_vertical: float | None = None,
    ) -> FatigueState:
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ValueError("Timestamps must be monotonically non-decreasing")
        self._last_timestamp = timestamp
        if self._session_start is None:
            self._session_start = timestamp
        threshold = self.thresholds
        eye_closed = ear < threshold.ear_closed
        yawning = mar > threshold.mar_yawn
        head_turned = (
            head_yaw is not None
            and abs(head_yaw) >= threshold.head_yaw_away_degrees
        )
        gaze_away = (
            gaze_horizontal is not None
            and abs(gaze_horizontal) >= threshold.gaze_horizontal_away
        ) or (
            gaze_vertical is not None
            and abs(gaze_vertical) >= threshold.gaze_vertical_away
        )
        attention_away = head_turned or gaze_away
        head_down = (
            head_pitch is not None
            and head_pitch <= -threshold.head_down_pitch_degrees
        )

        self._attention_away_since = self._update_duration_start(
            attention_away, timestamp, self._attention_away_since
        )
        self._head_down_since = self._update_duration_start(
            head_down, timestamp, self._head_down_since
        )

        if head_pitch is not None:
            if (
                self._nod_started_at is None
                and head_pitch <= -threshold.nod_pitch_degrees
            ):
                self._nod_started_at = timestamp
            elif self._nod_started_at is not None:
                nod_duration = timestamp - self._nod_started_at
                if head_pitch >= -threshold.nod_return_degrees:
                    if threshold.nod_min_seconds <= nod_duration <= threshold.nod_max_seconds:
                        self._nod_events.append(timestamp)
                        self._nod_count += 1
                    self._nod_started_at = None
                elif nod_duration > threshold.nod_max_seconds:
                    self._nod_started_at = None

        nod_cutoff = timestamp - threshold.nod_window_seconds
        while self._nod_events and self._nod_events[0] < nod_cutoff:
            self._nod_events.popleft()

        if head_yaw is not None and head_pitch is not None:
            self._pose_samples.append((timestamp, head_yaw, head_pitch))

        if eye_closed:
            if self._eye_closed_since is None:
                self._eye_closed_since = timestamp
        elif self._eye_closed_since is not None:
            closure = timestamp - self._eye_closed_since
            if 0.08 <= closure <= 0.80:
                self._blink_count += 1
                self._blink_durations.append(closure)
            self._eye_closed_since = None

        if yawning:
            if self._yawn_since is None:
                self._yawn_since = timestamp
            self._yawn_last_open = timestamp
        elif self._yawn_since is not None:
            # Tolerate brief single-frame dropouts (landmark jitter, motion
            # blur) mid-yawn instead of resetting the streak immediately --
            # a wide-open yawning mouth deforms fast enough that one noisy
            # frame below threshold used to wipe out the whole accumulated
            # duration and silently prevent the count from ever landing.
            gap = timestamp - (self._yawn_last_open or self._yawn_since)
            if gap > threshold.yawn_grace_seconds:
                if (
                    (self._yawn_last_open or self._yawn_since) - self._yawn_since
                    >= threshold.yawn_alarm_seconds
                ):
                    self._yawn_count += 1
                self._yawn_since = None
                self._yawn_last_open = None

        self._history.append((timestamp, eye_closed))
        perclos, history_seconds = self._time_weighted_perclos(timestamp)
        eye_seconds = (
            timestamp - self._eye_closed_since if self._eye_closed_since is not None else 0.0
        )
        yawn_seconds = timestamp - self._yawn_since if self._yawn_since is not None else 0.0
        attention_seconds = (
            timestamp - self._attention_away_since
            if self._attention_away_since is not None
            else 0.0
        )
        head_down_seconds = (
            timestamp - self._head_down_since
            if self._head_down_since is not None
            else 0.0
        )
        perclos_ready = history_seconds >= threshold.minimum_perclos_history_seconds

        fatigue_reasons: list[str] = []
        if eye_seconds >= threshold.eye_alarm_seconds:
            fatigue_reasons.append(f"eyes closed {eye_seconds:.1f}s")
        if yawn_seconds >= threshold.yawn_alarm_seconds:
            fatigue_reasons.append(f"yawn {yawn_seconds:.1f}s")
        if perclos_ready and perclos >= threshold.perclos_alarm:
            fatigue_reasons.append(f"PERCLOS {perclos:.0%}")
        if head_down_seconds >= threshold.head_down_alarm_seconds:
            fatigue_reasons.append(f"head down {head_down_seconds:.1f}s")
        if len(self._nod_events) >= threshold.nod_alarm_count:
            fatigue_reasons.append(
                f"{len(self._nod_events)} nods/{threshold.nod_window_seconds:.0f}s"
            )

        inattentive = attention_seconds >= threshold.attention_alarm_seconds
        attention_reasons = (
            [f"looking away {attention_seconds:.1f}s"] if inattentive else []
        )
        reasons = fatigue_reasons + attention_reasons

        eye_component = min(1.0, eye_seconds / threshold.eye_alarm_seconds)
        mouth_component = min(1.0, yawn_seconds / threshold.yawn_alarm_seconds)
        perclos_component = (
            min(1.0, perclos / threshold.perclos_alarm) if perclos_ready else 0.0
        )
        base_fatigue_score = 100.0 * (
            0.50 * eye_component + 0.20 * mouth_component + 0.30 * perclos_component
        )
        head_down_component = min(
            1.0,
            head_down_seconds / max(threshold.head_down_alarm_seconds, 1e-8),
        )
        nod_component = min(
            1.0,
            len(self._nod_events) / max(threshold.nod_alarm_count, 1),
        )
        fatigue_score = base_fatigue_score + 15.0 * head_down_component + 10.0 * nod_component

        blink_p50_ms, blink_p90_ms = self._blink_percentiles()
        movement_entropy = self._movement_entropy(timestamp)
        sedentary_hours = max(0.0, timestamp - self._session_start) / 3600.0

        return FatigueState(
            timestamp=timestamp,
            ear=ear,
            mar=mar,
            eye_closed=eye_closed,
            yawning=yawning,
            eye_closed_seconds=eye_seconds,
            yawn_seconds=yawn_seconds,
            perclos=perclos,
            history_seconds=history_seconds,
            blink_count=self._blink_count,
            yawn_count=self._yawn_count,
            head_pitch=head_pitch,
            head_yaw=head_yaw,
            head_roll=head_roll,
            gaze_horizontal=gaze_horizontal,
            gaze_vertical=gaze_vertical,
            attention_away=attention_away,
            attention_away_seconds=attention_seconds,
            head_down=head_down,
            head_down_seconds=head_down_seconds,
            nod_count=self._nod_count,
            recent_nod_count=len(self._nod_events),
            inattentive=inattentive,
            fatigue_score=min(100.0, fatigue_score),
            drowsy=bool(fatigue_reasons),
            reasons=tuple(reasons),
            blink_duration_ms_p50=blink_p50_ms,
            blink_duration_ms_p90=blink_p90_ms,
            movement_entropy=movement_entropy,
            sedentary_hours=sedentary_hours,
        )
