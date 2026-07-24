"""Shared pose/gait dataset and feature utilities for fatigue experiments."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


LOWER_BODY_JOINTS = (
    "pelvis",
    "chest",
    "left_hip",
    "left_knee",
    "left_ankle",
    "left_foot",
    "right_hip",
    "right_knee",
    "right_ankle",
    "right_foot",
)

LOWER_BODY_EDGES = (
    (0, 1),
    (0, 2),
    (2, 3),
    (3, 4),
    (4, 5),
    (0, 6),
    (6, 7),
    (7, 8),
    (8, 9),
)


@dataclass
class PoseDataset:
    coordinates: np.ndarray
    labels: np.ndarray
    subjects: np.ndarray
    levels: np.ndarray
    rpe: np.ndarray
    order: np.ndarray
    joint_names: tuple[str, ...]

    def validate(self) -> "PoseDataset":
        if self.coordinates.ndim != 4:
            raise ValueError("coordinates 必须为 [样本, 时间, 关节, 坐标]")
        n_samples = self.coordinates.shape[0]
        if self.coordinates.shape[-1] not in (2, 3):
            raise ValueError("坐标维度必须为 2 或 3")
        for name, array in (
            ("labels", self.labels),
            ("subjects", self.subjects),
            ("levels", self.levels),
            ("rpe", self.rpe),
            ("order", self.order),
        ):
            if len(array) != n_samples:
                raise ValueError(f"{name} 长度与样本数不一致")
        if len(self.joint_names) != self.coordinates.shape[2]:
            raise ValueError("joint_names 长度与关节数不一致")
        return self


def load_pose_dataset(path: str | Path) -> PoseDataset:
    path = Path(path)
    with np.load(path, allow_pickle=False) as data:
        coordinates = np.asarray(data["coordinates"], dtype=np.float32)
        if coordinates.shape[-1] == 2:
            coordinates = np.pad(coordinates, ((0, 0), (0, 0), (0, 0), (0, 1)))
        n_samples = coordinates.shape[0]
        labels = np.asarray(data["labels"], dtype=np.int64)
        subjects = np.asarray(
            data["subjects"] if "subjects" in data else np.arange(n_samples).astype(str)
        ).astype(str)
        levels = np.asarray(
            data["levels"] if "levels" in data else labels, dtype=np.int64
        )
        rpe = np.asarray(
            data["rpe"] if "rpe" in data else np.full(n_samples, np.nan),
            dtype=np.float32,
        )
        order = np.asarray(
            data["order"] if "order" in data else np.arange(n_samples),
            dtype=np.int64,
        )
        if "joint_names" in data:
            joint_names = tuple(np.asarray(data["joint_names"]).astype(str).tolist())
        else:
            joint_names = tuple(f"joint_{idx}" for idx in range(coordinates.shape[2]))
    return PoseDataset(
        coordinates=coordinates,
        labels=labels,
        subjects=subjects,
        levels=levels,
        rpe=rpe,
        order=order,
        joint_names=joint_names,
    ).validate()


def save_pose_dataset(dataset: PoseDataset, path: str | Path) -> Path:
    dataset.validate()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        coordinates=dataset.coordinates.astype(np.float32),
        labels=dataset.labels.astype(np.int64),
        subjects=dataset.subjects.astype(str),
        levels=dataset.levels.astype(np.int64),
        rpe=dataset.rpe.astype(np.float32),
        order=dataset.order.astype(np.int64),
        joint_names=np.asarray(dataset.joint_names),
    )
    return path


def resample_sequence(sequence: np.ndarray, frames: int) -> np.ndarray:
    sequence = np.asarray(sequence, dtype=np.float32)
    if sequence.ndim != 3:
        raise ValueError("单个序列必须为 [时间, 关节, 坐标]")
    if sequence.shape[0] == frames:
        return sequence
    old_time = np.linspace(0.0, 1.0, sequence.shape[0])
    new_time = np.linspace(0.0, 1.0, frames)
    output = np.empty((frames, sequence.shape[1], sequence.shape[2]), dtype=np.float32)
    for joint in range(sequence.shape[1]):
        for axis in range(sequence.shape[2]):
            values = sequence[:, joint, axis]
            finite = np.isfinite(values)
            if finite.sum() < 2:
                output[:, joint, axis] = 0.0
            else:
                output[:, joint, axis] = np.interp(
                    new_time, old_time[finite], values[finite]
                )
    return output


def normalize_sequence(
    sequence: np.ndarray, joint_names: Iterable[str]
) -> np.ndarray:
    sequence = np.asarray(sequence, dtype=np.float32).copy()
    names = {name: idx for idx, name in enumerate(joint_names)}
    root_idx = names.get("pelvis", 0)
    sequence -= sequence[:, root_idx : root_idx + 1]

    scale_candidates = []
    for left, right in (
        ("left_hip", "right_hip"),
        ("left_shoulder", "right_shoulder"),
    ):
        if left in names and right in names:
            scale_candidates.append(
                np.linalg.norm(
                    sequence[:, names[left]] - sequence[:, names[right]], axis=-1
                )
            )
    if "chest" in names:
        scale_candidates.append(np.linalg.norm(sequence[:, names["chest"]], axis=-1))
    if scale_candidates:
        scale = float(np.nanmedian(np.concatenate(scale_candidates)))
        if np.isfinite(scale) and scale > 1e-6:
            sequence /= scale
    return np.nan_to_num(sequence, copy=False)


def joint_angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    ba = a - b
    bc = c - b
    denominator = np.linalg.norm(ba, axis=-1) * np.linalg.norm(bc, axis=-1)
    cosine = np.sum(ba * bc, axis=-1) / np.maximum(denominator, 1e-8)
    return np.arccos(np.clip(cosine, -1.0, 1.0))


def _coefficient_of_variation(values: np.ndarray) -> float:
    mean = float(np.mean(np.abs(values)))
    return float(np.std(values) / max(mean, 1e-6))


def _sample_entropy_proxy(values: np.ndarray) -> float:
    """A stable, inexpensive irregularity proxy for short gait windows."""
    differences = np.diff(values)
    scale = float(np.std(values))
    if scale < 1e-8:
        return 0.0
    return float(np.mean(np.abs(differences)) / scale)


def _dominant_frequency(values: np.ndarray) -> float:
    centered = values - np.mean(values)
    spectrum = np.abs(np.fft.rfft(centered))
    if spectrum.size <= 1 or np.allclose(spectrum[1:], 0):
        return 0.0
    return float(np.argmax(spectrum[1:]) + 1) / max(len(values), 1)


def extract_sequence_features(
    sequence: np.ndarray, joint_names: Iterable[str]
) -> dict[str, float]:
    names_list = tuple(joint_names)
    names = {name: idx for idx, name in enumerate(names_list)}
    x = normalize_sequence(sequence, names_list)
    velocity = np.diff(x, axis=0, prepend=x[:1])
    acceleration = np.diff(velocity, axis=0, prepend=velocity[:1])
    jerk = np.diff(acceleration, axis=0, prepend=acceleration[:1])

    speed = np.linalg.norm(velocity, axis=-1)
    acceleration_norm = np.linalg.norm(acceleration, axis=-1)
    jerk_norm = np.linalg.norm(jerk, axis=-1)
    features: dict[str, float] = {
        "speed_mean": float(np.mean(speed)),
        "speed_std": float(np.std(speed)),
        "speed_max": float(np.max(speed)),
        "acceleration_mean": float(np.mean(acceleration_norm)),
        "acceleration_std": float(np.std(acceleration_norm)),
        "jerk_mean": float(np.mean(jerk_norm)),
        "jerk_std": float(np.std(jerk_norm)),
        "jerk_max": float(np.max(jerk_norm)),
        "motion_energy": float(np.mean(np.square(velocity))),
        "still_ratio": float(np.mean(speed < max(np.median(speed) * 0.25, 1e-4))),
    }

    if "pelvis" in names and "chest" in names:
        trunk = x[:, names["chest"]] - x[:, names["pelvis"]]
        features["trunk_lean_mean"] = float(
            np.mean(np.arctan2(np.abs(trunk[:, 2]), np.abs(trunk[:, 1]) + 1e-8))
        )
        features["trunk_sway_std"] = float(np.std(trunk[:, 0]))
        features["trunk_bob_std"] = float(np.std(trunk[:, 1]))

    for side in ("left", "right"):
        hip = f"{side}_hip"
        knee = f"{side}_knee"
        ankle = f"{side}_ankle"
        foot = f"{side}_foot"
        if all(name in names for name in (hip, knee, ankle)):
            knee_angle = joint_angle(
                x[:, names[hip]], x[:, names[knee]], x[:, names[ankle]]
            )
            features[f"{side}_knee_rom"] = float(np.ptp(knee_angle))
            features[f"{side}_knee_angle_std"] = float(np.std(knee_angle))
        if foot in names:
            trajectory = x[:, names[foot]]
            features[f"{side}_foot_forward_rom"] = float(np.ptp(trajectory[:, 2]))
            features[f"{side}_foot_vertical_rom"] = float(np.ptp(trajectory[:, 1]))
            features[f"{side}_foot_speed_cv"] = _coefficient_of_variation(
                speed[:, names[foot]]
            )

    if "left_foot" in names and "right_foot" in names:
        left = x[:, names["left_foot"]]
        right = x[:, names["right_foot"]]
        phase_signal = left[:, 2] - right[:, 2]
        features["step_width_mean"] = float(np.mean(np.abs(left[:, 0] - right[:, 0])))
        features["step_width_std"] = float(np.std(left[:, 0] - right[:, 0]))
        features["gait_frequency"] = _dominant_frequency(phase_signal)
        features["gait_irregularity"] = _sample_entropy_proxy(phase_signal)
        features["foot_speed_asymmetry"] = float(
            abs(np.mean(speed[:, names["left_foot"]]) - np.mean(speed[:, names["right_foot"]]))
        )

    if "left_knee_rom" in features and "right_knee_rom" in features:
        features["knee_rom_asymmetry"] = abs(
            features["left_knee_rom"] - features["right_knee_rom"]
        )
    if (
        "left_foot_forward_rom" in features
        and "right_foot_forward_rom" in features
    ):
        features["stride_rom_asymmetry"] = abs(
            features["left_foot_forward_rom"]
            - features["right_foot_forward_rom"]
        )

    return {key: float(np.nan_to_num(value)) for key, value in features.items()}


def build_feature_matrix(dataset: PoseDataset) -> tuple[np.ndarray, list[str]]:
    rows = [
        extract_sequence_features(sequence, dataset.joint_names)
        for sequence in dataset.coordinates
    ]
    feature_names = sorted({name for row in rows for name in row})
    matrix = np.asarray(
        [[row.get(name, 0.0) for name in feature_names] for row in rows],
        dtype=np.float64,
    )
    return matrix, feature_names


def make_synthetic_dataset(
    *,
    subjects: int = 12,
    samples_per_level: int = 6,
    frames: int = 120,
    seed: int = 7,
) -> PoseDataset:
    """Generate deterministic lower-body skeletons for smoke tests and demos."""
    rng = np.random.default_rng(seed)
    all_sequences = []
    labels = []
    levels = []
    rpe = []
    subject_ids = []
    order = []

    for subject in range(subjects):
        subject_scale = rng.normal(1.0, 0.06)
        hip_width = rng.normal(0.34, 0.025)
        sequence_order = 0
        for level in range(3):
            for _sample in range(samples_per_level):
                time = np.linspace(0.0, 4.0 * np.pi, frames, dtype=np.float32)
                phase = rng.uniform(-0.15, 0.15)
                cadence = 1.0 - 0.06 * level + rng.normal(0.0, 0.01)
                gait_phase = cadence * time + phase
                amplitude = (0.46 - 0.055 * level) * subject_scale
                width = hip_width * (1.0 + 0.10 * level)
                noise = 0.004 + 0.004 * level
                trunk_lean = 0.035 + 0.055 * level

                x = np.zeros((frames, len(LOWER_BODY_JOINTS), 3), dtype=np.float32)
                x[:, 0] = np.array([0.0, 0.0, 0.0])
                x[:, 1, 1] = 0.95 * subject_scale
                x[:, 1, 2] = trunk_lean
                x[:, 2] = np.array([-width / 2, 0.0, 0.0])
                x[:, 6] = np.array([width / 2, 0.0, 0.0])

                for side, hip_idx, knee_idx, ankle_idx, foot_idx, sign in (
                    ("left", 2, 3, 4, 5, 1.0),
                    ("right", 6, 7, 8, 9, -1.0),
                ):
                    del side
                    swing = np.sin(gait_phase) * amplitude * sign
                    knee_lift = np.maximum(np.sin(gait_phase) * sign, 0.0)
                    x[:, knee_idx, 0] = x[:, hip_idx, 0]
                    x[:, knee_idx, 1] = -0.45 * subject_scale + 0.10 * knee_lift
                    x[:, knee_idx, 2] = 0.45 * swing
                    x[:, ankle_idx, 0] = x[:, hip_idx, 0] * (1.05 + 0.04 * level)
                    x[:, ankle_idx, 1] = -0.90 * subject_scale + 0.08 * knee_lift
                    x[:, ankle_idx, 2] = swing
                    x[:, foot_idx] = x[:, ankle_idx]
                    x[:, foot_idx, 1] -= 0.04
                    x[:, foot_idx, 2] += 0.12 * subject_scale

                x[:, :, 1] += 0.018 * np.sin(2.0 * gait_phase)[:, None]
                x += rng.normal(0.0, noise, size=x.shape).astype(np.float32)
                all_sequences.append(x)
                levels.append(level)
                labels.append(int(level == 2))
                rpe.append((6.0, 12.0, 17.0)[level] + rng.normal(0.0, 0.5))
                subject_ids.append(f"synthetic_{subject + 1:02d}")
                order.append(sequence_order)
                sequence_order += 1

    return PoseDataset(
        coordinates=np.asarray(all_sequences, dtype=np.float32),
        labels=np.asarray(labels, dtype=np.int64),
        subjects=np.asarray(subject_ids),
        levels=np.asarray(levels, dtype=np.int64),
        rpe=np.asarray(rpe, dtype=np.float32),
        order=np.asarray(order, dtype=np.int64),
        joint_names=LOWER_BODY_JOINTS,
    ).validate()


def write_json(data: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
