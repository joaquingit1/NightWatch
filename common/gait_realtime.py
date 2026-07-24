from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from sklearn.covariance import LedoitWolf

from .mediapipe_pose import PoseLandmarks
from .pose_gait_data import (
    LOWER_BODY_JOINTS,
    extract_sequence_features,
    normalize_sequence,
    resample_sequence,
)


LEFT_SHOULDER = 11
RIGHT_SHOULDER = 12
LEFT_HIP = 23
RIGHT_HIP = 24
LEFT_KNEE = 25
RIGHT_KNEE = 26
LEFT_ANKLE = 27
RIGHT_ANKLE = 28
LEFT_HEEL = 29
RIGHT_HEEL = 30
LEFT_FOOT = 31
RIGHT_FOOT = 32

POSE_DRAW_EDGES = (
    (LEFT_SHOULDER, RIGHT_SHOULDER),
    (LEFT_SHOULDER, LEFT_HIP),
    (RIGHT_SHOULDER, RIGHT_HIP),
    (LEFT_HIP, RIGHT_HIP),
    (LEFT_HIP, LEFT_KNEE),
    (LEFT_KNEE, LEFT_ANKLE),
    (LEFT_ANKLE, LEFT_HEEL),
    (LEFT_HEEL, LEFT_FOOT),
    (RIGHT_HIP, RIGHT_KNEE),
    (RIGHT_KNEE, RIGHT_ANKLE),
    (RIGHT_ANKLE, RIGHT_HEEL),
    (RIGHT_HEEL, RIGHT_FOOT),
)

REQUIRED_POSE_INDICES = (
    LEFT_SHOULDER,
    RIGHT_SHOULDER,
    LEFT_HIP,
    RIGHT_HIP,
    LEFT_KNEE,
    RIGHT_KNEE,
    LEFT_ANKLE,
    RIGHT_ANKLE,
    LEFT_FOOT,
    RIGHT_FOOT,
)


def pose_quality(landmarks: PoseLandmarks) -> float:
    confidence = np.minimum(landmarks.visibility, landmarks.presence)
    return float(np.min(confidence[list(REQUIRED_POSE_INDICES)]))


def pose_to_lower_body(
    landmarks: PoseLandmarks, *, view: str = "side"
) -> np.ndarray:
    """Map MediaPipe's 33 landmarks to the training pipeline's 10 joints."""
    source = landmarks.world
    pelvis = 0.5 * (source[LEFT_HIP] + source[RIGHT_HIP])
    chest = 0.5 * (source[LEFT_SHOULDER] + source[RIGHT_SHOULDER])
    left_foot = 0.5 * (source[LEFT_HEEL] + source[LEFT_FOOT])
    right_foot = 0.5 * (source[RIGHT_HEEL] + source[RIGHT_FOOT])
    points = np.stack(
        (
            pelvis,
            chest,
            source[LEFT_HIP],
            source[LEFT_KNEE],
            source[LEFT_ANKLE],
            left_foot,
            source[RIGHT_HIP],
            source[RIGHT_KNEE],
            source[RIGHT_ANKLE],
            right_foot,
        )
    ).astype(np.float32)
    converted = np.empty_like(points)
    if view == "side":
        # In a side view, image/world X is the sagittal (forward) direction.
        converted[:, 0] = points[:, 2]
        converted[:, 1] = -points[:, 1]
        converted[:, 2] = points[:, 0]
    elif view == "front":
        converted[:, 0] = points[:, 0]
        converted[:, 1] = -points[:, 1]
        converted[:, 2] = -points[:, 2]
    else:
        raise ValueError(f"未知视角：{view}")
    return converted


class PoseSmoother:
    def __init__(self, alpha: float = 0.35, maximum_gap_seconds: float = 0.5):
        self.alpha = alpha
        self.maximum_gap_seconds = maximum_gap_seconds
        self._state: np.ndarray | None = None
        self._timestamp: float | None = None

    def update(self, skeleton: np.ndarray, timestamp: float) -> np.ndarray:
        if (
            self._state is None
            or self._timestamp is None
            or timestamp - self._timestamp > self.maximum_gap_seconds
        ):
            self._state = skeleton.astype(np.float32, copy=True)
        else:
            self._state = (
                self.alpha * skeleton + (1.0 - self.alpha) * self._state
            ).astype(np.float32)
        self._timestamp = timestamp
        return self._state.copy()

    def reset(self) -> None:
        self._state = None
        self._timestamp = None


class GaitCycleBuffer:
    """Extract one gait cycle between two same-direction foot-phase crossings."""

    def __init__(
        self,
        *,
        output_frames: int = 150,
        minimum_seconds: float = 0.45,
        maximum_seconds: float = 2.0,
        minimum_phase_amplitude: float = 0.08,
        maximum_gap_seconds: float = 0.4,
    ) -> None:
        self.output_frames = output_frames
        self.minimum_seconds = minimum_seconds
        self.maximum_seconds = maximum_seconds
        self.minimum_phase_amplitude = minimum_phase_amplitude
        self.maximum_gap_seconds = maximum_gap_seconds
        self.completed_cycles = 0
        self._last_timestamp: float | None = None
        self._last_phase: float | None = None
        self._cycle_start: float | None = None
        self._frames: list[np.ndarray] = []
        self._phases: list[float] = []

    @property
    def collecting(self) -> bool:
        return self._cycle_start is not None

    def reset_partial(self) -> None:
        self._last_timestamp = None
        self._last_phase = None
        self._cycle_start = None
        self._frames.clear()
        self._phases.clear()

    def reset(self) -> None:
        self.reset_partial()
        self.completed_cycles = 0

    def update(
        self, skeleton: np.ndarray, timestamp: float
    ) -> np.ndarray | None:
        if (
            self._last_timestamp is not None
            and timestamp - self._last_timestamp > self.maximum_gap_seconds
        ):
            self.reset_partial()

        phase = float(skeleton[5, 2] - skeleton[9, 2])
        crossing = (
            self._last_phase is not None
            and self._last_phase < 0.0
            and phase >= 0.0
        )
        if self.collecting:
            self._frames.append(skeleton.astype(np.float32, copy=True))
            self._phases.append(phase)

        completed: np.ndarray | None = None
        if crossing:
            if self.collecting and self._cycle_start is not None:
                duration = timestamp - self._cycle_start
                amplitude = (
                    float(np.ptp(self._phases)) if self._phases else 0.0
                )
                if (
                    self.minimum_seconds <= duration <= self.maximum_seconds
                    and len(self._frames) >= 8
                    and amplitude >= self.minimum_phase_amplitude
                ):
                    completed = resample_sequence(
                        np.stack(self._frames), self.output_frames
                    )
                    self.completed_cycles += 1
            self._cycle_start = timestamp
            self._frames = [skeleton.astype(np.float32, copy=True)]
            self._phases = [phase]
        elif (
            self._cycle_start is not None
            and timestamp - self._cycle_start > self.maximum_seconds * 1.5
        ):
            self._cycle_start = None
            self._frames.clear()
            self._phases.clear()

        self._last_phase = phase
        self._last_timestamp = timestamp
        return completed


@dataclass
class MahalanobisResult:
    ready: bool
    calibration_progress: float
    distance: float | None = None
    ewma: float | None = None
    threshold: float | None = None
    ratio: float | None = None
    fatigued: bool = False


class OnlineMahalanobis:
    """Calibrate from known-normal gait cycles, then score baseline deviation."""

    def __init__(
        self,
        feature_names: list[str],
        *,
        baseline_cycles: int,
        alpha: float,
        calibration_fraction: float,
        threshold_margin: float,
    ) -> None:
        if baseline_cycles < 6:
            raise ValueError("baseline_cycles 至少为 6")
        self.feature_names = feature_names
        self.baseline_cycles = baseline_cycles
        self.alpha = alpha
        self.calibration_fraction = calibration_fraction
        self.threshold_margin = threshold_margin
        self.reset()

    def reset(self) -> None:
        self._baseline: list[np.ndarray] = []
        self._model: LedoitWolf | None = None
        self._threshold: float | None = None
        self._ewma: float | None = None

    def vector(self, features: dict[str, float]) -> np.ndarray:
        return np.asarray(
            [features.get(name, 0.0) for name in self.feature_names],
            dtype=np.float64,
        )

    def _fit(self) -> None:
        matrix = np.stack(self._baseline)
        model_count = max(
            3, int(math.floor(len(matrix) * self.calibration_fraction))
        )
        model_count = min(model_count, len(matrix) - 2)
        model = LedoitWolf().fit(matrix[:model_count])
        validation = matrix[model_count:]
        centered = validation - model.location_
        squared = np.einsum(
            "ni,ij,nj->n", centered, model.precision_, centered
        )
        distances = np.sqrt(np.maximum(squared, 0.0))
        median = float(np.median(distances))
        mad = float(np.median(np.abs(distances - median)))
        self._threshold = max(
            float(np.quantile(distances, 0.99)),
            median + 4.0 * max(mad, 1e-3),
        ) * self.threshold_margin
        self._threshold = max(self._threshold, 1e-6)
        self._model = model

    def update(self, features: dict[str, float]) -> MahalanobisResult:
        vector = self.vector(features)
        if self._model is None:
            self._baseline.append(vector)
            if len(self._baseline) >= self.baseline_cycles:
                self._fit()
            return MahalanobisResult(
                ready=self._model is not None,
                calibration_progress=min(
                    len(self._baseline) / self.baseline_cycles, 1.0
                ),
            )

        assert self._threshold is not None
        centered = vector - self._model.location_
        squared = float(centered @ self._model.precision_ @ centered)
        distance = math.sqrt(max(squared, 0.0))
        self._ewma = (
            distance
            if self._ewma is None
            else self.alpha * distance + (1.0 - self.alpha) * self._ewma
        )
        ratio = self._ewma / self._threshold
        return MahalanobisResult(
            ready=True,
            calibration_progress=1.0,
            distance=distance,
            ewma=self._ewma,
            threshold=self._threshold,
            ratio=ratio,
            fatigued=ratio > 1.0,
        )


@dataclass(frozen=True)
class TrainingReference:
    mahalanobis_balanced_accuracy: float | None
    xgboost_balanced_accuracy: float | None
    stgcn_balanced_accuracy: float | None


@dataclass
class GaitInference:
    cycle_index: int
    mahalanobis: MahalanobisResult
    xgboost_probability: float | None
    xgboost_threshold: float | None
    stgcn_probability: float | None
    stgcn_threshold: float | None
    fusion_score: float | None
    fusion_threshold: float
    consecutive_fatigue_cycles: int
    status: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        return payload


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _relative_probability(probability: float, threshold: float) -> float:
    epsilon = 1e-6
    probability = float(np.clip(probability, epsilon, 1.0 - epsilon))
    threshold = float(np.clip(threshold, epsilon, 1.0 - epsilon))
    margin = math.log(probability / (1.0 - probability)) - math.log(
        threshold / (1.0 - threshold)
    )
    return 1.0 / (1.0 + math.exp(-float(np.clip(margin, -30.0, 30.0))))


class RealtimeGaitModels:
    """Load all trained artifacts and evaluate one completed gait cycle."""

    def __init__(
        self,
        *,
        project_root: Path,
        baseline_cycles: int,
        fusion_threshold: float,
        alarm_cycles: int,
        enable_xgboost: bool = True,
        enable_stgcn: bool = True,
        xgboost_model: Path | None = None,
        stgcn_model: Path | None = None,
        device: str = "auto",
    ) -> None:
        self.project_root = project_root
        self.fusion_threshold = fusion_threshold
        self.alarm_cycles = alarm_cycles
        self.cycle_index = 0
        self.consecutive_fatigue_cycles = 0
        self.fusion_history: deque[float] = deque(maxlen=80)

        mahalanobis_config = _read_json(
            project_root
            / "05_mahalanobis_ewma"
            / "outputs"
            / "optimized_config.json"
        )
        mahalanobis_parameters = mahalanobis_config["best_parameters"]
        self._baseline_cycles = baseline_cycles
        self._mahalanobis_parameters = dict(mahalanobis_parameters)
        xgboost_results = _read_json(
            project_root
            / "06_xgboost_features"
            / "outputs"
            / "tuning_results.json"
        )
        self.feature_names = list(xgboost_results["feature_names"])
        self.mahalanobis = OnlineMahalanobis(
            self.feature_names,
            baseline_cycles=baseline_cycles,
            alpha=float(mahalanobis_parameters["alpha"]),
            calibration_fraction=float(
                mahalanobis_parameters["calibration_fraction"]
            ),
            threshold_margin=float(
                mahalanobis_parameters["threshold_margin"]
            ),
        )
        self.training_reference = self._load_training_reference(project_root)

        # Import XGBoost before PyTorch on macOS to avoid duplicate OpenMP crashes.
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        self._xgboost = None
        self._xgboost_threshold: float | None = None
        if enable_xgboost:
            import xgboost as xgb

            model_path = xgboost_model or (
                project_root
                / "06_xgboost_features"
                / "outputs"
                / "model_optimized.ubj"
            )
            config = _read_json(
                project_root
                / "06_xgboost_features"
                / "outputs"
                / "optimized_config.json"
            )
            self._xgboost_threshold = float(config["decision_threshold"])
            self._xgboost = xgb.XGBClassifier()
            self._xgboost.load_model(model_path)

        self._torch = None
        self._stgcn = None
        self._stgcn_threshold: float | None = None
        self._device = None
        if enable_stgcn:
            self._load_stgcn(
                stgcn_model
                or (
                    project_root
                    / "07_stgcn_skeleton"
                    / "outputs"
                    / "model_optimized.pt"
                ),
                device,
            )

    def fork(self) -> "RealtimeGaitModels":
        """Create independent per-person state while sharing loaded weights."""
        clone = object.__new__(RealtimeGaitModels)
        clone.project_root = self.project_root
        clone.fusion_threshold = self.fusion_threshold
        clone.alarm_cycles = self.alarm_cycles
        clone.cycle_index = 0
        clone.consecutive_fatigue_cycles = 0
        clone.fusion_history = deque(maxlen=80)
        clone.feature_names = self.feature_names
        clone._baseline_cycles = self._baseline_cycles
        clone._mahalanobis_parameters = self._mahalanobis_parameters
        clone.mahalanobis = OnlineMahalanobis(
            clone.feature_names,
            baseline_cycles=clone._baseline_cycles,
            alpha=float(clone._mahalanobis_parameters["alpha"]),
            calibration_fraction=float(
                clone._mahalanobis_parameters["calibration_fraction"]
            ),
            threshold_margin=float(
                clone._mahalanobis_parameters["threshold_margin"]
            ),
        )
        clone.training_reference = self.training_reference
        clone._xgboost = self._xgboost
        clone._xgboost_threshold = self._xgboost_threshold
        clone._torch = self._torch
        clone._stgcn = self._stgcn
        clone._stgcn_threshold = self._stgcn_threshold
        clone._device = self._device
        return clone

    @staticmethod
    def _load_training_reference(project_root: Path) -> TrainingReference:
        mahalanobis = _read_json(
            project_root
            / "05_mahalanobis_ewma"
            / "outputs"
            / "optimized_config.json"
        )
        xgboost = _read_json(
            project_root
            / "06_xgboost_features"
            / "outputs"
            / "optimized_config.json"
        )
        stgcn = _read_json(
            project_root
            / "07_stgcn_skeleton"
            / "outputs"
            / "optimized_config.json"
        )
        return TrainingReference(
            mahalanobis_balanced_accuracy=float(
                mahalanobis["nested_loso_metrics"]["balanced_accuracy"]
            ),
            xgboost_balanced_accuracy=float(
                xgboost["nested_loso_fixed_0_5_metrics"][
                    "balanced_accuracy"
                ]
            ),
            stgcn_balanced_accuracy=float(
                stgcn["held_out_test_metrics"]["balanced_accuracy"]
            ),
        )

    def _load_stgcn(self, checkpoint_path: Path, device_name: str) -> None:
        import torch

        model_source = (
            self.project_root / "07_stgcn_skeleton" / "model.py"
        )
        module_name = "robotdog_runtime_stgcn_model"
        spec = importlib.util.spec_from_file_location(module_name, model_source)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 ST-GCN 定义：{model_source}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        if device_name == "auto":
            if torch.backends.mps.is_available():
                device = torch.device("mps")
            elif torch.cuda.is_available():
                device = torch.device("cuda")
            else:
                device = torch.device("cpu")
        else:
            device = torch.device(device_name)
        checkpoint = torch.load(
            checkpoint_path,
            map_location="cpu",
            weights_only=False,
        )
        parameters = checkpoint.get("parameters", {})
        model = module.MultiStreamSTGCN(
            len(checkpoint["joint_names"]),
            [tuple(edge) for edge in checkpoint["edges"]],
            coordinate_channels=3,
            dropout=float(parameters.get("dropout", 0.2)),
        )
        model.load_state_dict(checkpoint["state_dict"])
        model.to(device).eval()
        self._torch = torch
        self._stgcn = model
        self._device = device
        self._stgcn_threshold = float(
            parameters.get("decision_threshold", 0.5)
        )

    def reset(self) -> None:
        self.mahalanobis.reset()
        self.cycle_index = 0
        self.consecutive_fatigue_cycles = 0
        self.fusion_history.clear()

    def _stgcn_probability(self, sequence: np.ndarray) -> float | None:
        if self._stgcn is None or self._torch is None:
            return None
        torch = self._torch
        normalized = normalize_sequence(sequence, LOWER_BODY_JOINTS)
        coordinates = torch.from_numpy(
            normalized.transpose(2, 0, 1)[None]
        ).float()
        velocity = torch.diff(
            coordinates,
            dim=2,
            prepend=coordinates[:, :, :1],
        )
        with torch.inference_mode():
            logits = self._stgcn(
                coordinates.to(self._device),
                velocity.to(self._device),
            )
            return float(
                torch.softmax(logits.float(), dim=1)[0, 1].cpu()
            )

    def process_cycle(self, sequence: np.ndarray) -> GaitInference:
        self.cycle_index += 1
        features = extract_sequence_features(sequence, LOWER_BODY_JOINTS)
        vector = np.asarray(
            [[features.get(name, 0.0) for name in self.feature_names]],
            dtype=np.float32,
        )
        mahalanobis = self.mahalanobis.update(features)
        xgboost_probability = (
            float(self._xgboost.predict_proba(vector)[0, 1])
            if self._xgboost is not None
            else None
        )
        stgcn_probability = self._stgcn_probability(sequence)

        fusion_score: float | None = None
        if mahalanobis.ready and mahalanobis.ratio is not None:
            evidence = [
                (
                    0.45,
                    1.0
                    / (
                        1.0
                        + math.exp(
                            -float(
                                np.clip(
                                    3.0 * (mahalanobis.ratio - 1.0),
                                    -30.0,
                                    30.0,
                                )
                            )
                        )
                    ),
                )
            ]
            if (
                xgboost_probability is not None
                and self._xgboost_threshold is not None
            ):
                evidence.append(
                    (
                        0.40,
                        _relative_probability(
                            xgboost_probability,
                            self._xgboost_threshold,
                        ),
                    )
                )
            if (
                stgcn_probability is not None
                and self._stgcn_threshold is not None
            ):
                evidence.append(
                    (
                        0.15,
                        _relative_probability(
                            stgcn_probability,
                            self._stgcn_threshold,
                        ),
                    )
                )
            total_weight = sum(weight for weight, _score in evidence)
            fusion_score = (
                sum(weight * score for weight, score in evidence)
                / total_weight
            )
            self.fusion_history.append(fusion_score)
            if fusion_score >= self.fusion_threshold:
                self.consecutive_fatigue_cycles += 1
            else:
                self.consecutive_fatigue_cycles = 0

        if not mahalanobis.ready:
            status = "CALIBRATING"
        elif fusion_score is None:
            status = "WAITING"
        elif self.consecutive_fatigue_cycles >= self.alarm_cycles:
            status = "FATIGUED"
        elif fusion_score >= self.fusion_threshold:
            status = "POSSIBLE FATIGUE"
        else:
            status = "ALERT"
        return GaitInference(
            cycle_index=self.cycle_index,
            mahalanobis=mahalanobis,
            xgboost_probability=xgboost_probability,
            xgboost_threshold=self._xgboost_threshold,
            stgcn_probability=stgcn_probability,
            stgcn_threshold=self._stgcn_threshold,
            fusion_score=fusion_score,
            fusion_threshold=self.fusion_threshold,
            consecutive_fatigue_cycles=self.consecutive_fatigue_cycles,
            status=status,
        )
