"""Portable person tracker with EdgeTAM's interface.

Stock DimOS follows people with EdgeTAM segmentation, which hard-requires
CUDA (edge_tam.py raises on any non-NVIDIA machine). This tracker satisfies
the same duck-typed contract used by PersonFollowSkillContainer:

    init_track(image, box, obj_id) -> ImageDetections2D
    process_image(image)           -> ImageDetections2D

but is backed by dimos' own YoloPersonDetector (yolo11n-pose + BoT-SORT
persistent track IDs, ultralytics).  CUDA is used when available; macOS uses
CPU deliberately.  Running this model in a second Apple-MPS worker killed the
whole worker with an MTLCompilerService/MPS assertion on the live M3.
init_track locks onto the YOLO track whose bbox best overlaps
the VL-detected person; process_image returns only that track, so the
follow loop's max-volume pick is a no-op. An empty result means "lost this
frame"; BoT-SORT re-associates through brief occlusions, and the skill's
own lost-frame counter handles permanent loss.
"""

import os
import platform
from threading import RLock

import numpy as np

from dimos.msgs.sensor_msgs.Image import Image
from dimos.perception.detection.type.detection2d.imageDetections2D import ImageDetections2D

BBox = tuple[float, float, float, float]
MIN_INIT_IOU = 0.10
REACQUIRE_MIN_APPEARANCE = 0.72
REACQUIRE_MIN_SCORE = 0.68


def _iou(a: BBox, b: BBox) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def _best_device() -> str:
    configured = os.getenv("NIGHTWATCH_YOLO_DEVICE")
    if configured:
        return configured

    import torch

    if torch.cuda.is_available():
        return "cuda"
    # Ultralytics works on MPS in isolation, but two independent model
    # processes (Moondream + YOLO) repeatedly crashed Metal's compiler service
    # on the 16 GB hackathon Mac.  CPU YOLO11n is fast enough for 10 Hz follow.
    if platform.system() != "Darwin" and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _appearance_descriptor(image: Image, bbox: BBox) -> np.ndarray | None:
    """Small colour descriptor used only to recover a dropped BoT-SORT ID.

    It is deliberately much cheaper than a second neural ReID model. The
    descriptor is never used to select an arbitrary visible person: recovery
    also requires spatial and size continuity with the last confirmed box.
    """
    frame = image.to_opencv()
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    # Use the central torso region; backgrounds and moving limbs make whole
    # person box histograms unstable.
    box_w = max(1.0, x2 - x1)
    box_h = max(1.0, y2 - y1)
    left = max(0, min(width, int(x1 + 0.20 * box_w)))
    right = max(0, min(width, int(x2 - 0.20 * box_w)))
    top = max(0, min(height, int(y1 + 0.18 * box_h)))
    bottom = max(0, min(height, int(y1 + 0.68 * box_h)))
    crop = frame[top:bottom, left:right]
    if crop.size == 0:
        return None
    parts = []
    for channel in range(min(3, crop.shape[2])):
        hist, _ = np.histogram(crop[..., channel], bins=16, range=(0, 256))
        parts.append(hist.astype(np.float32))
    descriptor = np.concatenate(parts)
    norm = float(np.linalg.norm(descriptor))
    return descriptor / norm if norm > 0 else None


def _appearance_similarity(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 0.0
    return float(np.clip(np.dot(a, b), 0.0, 1.0))


class YoloFollowTracker:
    def __init__(self, device: str | None = None) -> None:
        # Import here: pulls ultralytics + weights download on first use.
        from dimos.perception.detection.detectors.person.yolo import YoloPersonDetector

        self._detector = YoloPersonDetector(device=device or _best_device())
        self._target_id: int | None = None
        self._last_bbox: BBox | None = None
        self._appearance: np.ndarray | None = None
        self._reacquired_count = 0
        self._lock = RLock()

    def init_track(self, image: Image, box, obj_id: int = 1) -> ImageDetections2D:
        with self._lock:
            target = tuple(float(v) for v in box)
            detections = self._detector.process_image(image)
            best, best_iou = None, 0.0
            for det in detections.detections:
                iou = _iou(det.bbox, target)
                if iou > best_iou:
                    best, best_iou = det, iou
            if best is None or best_iou < MIN_INIT_IOU or best.track_id < 0:
                return ImageDetections2D(image=image)
            self._target_id = best.track_id
            self._last_bbox = tuple(float(v) for v in best.bbox)
            self._appearance = _appearance_descriptor(image, self._last_bbox)
            return ImageDetections2D(image=image, detections=[best])

    def process_image(self, image: Image) -> ImageDetections2D:
        with self._lock:
            if self._target_id is None:
                return ImageDetections2D(image=image)
            detections = self._detector.process_image(image)
            mine = [d for d in detections.detections if d.track_id == self._target_id]
            if mine:
                current = mine[0]
                self._last_bbox = tuple(float(v) for v in current.bbox)
                current_appearance = _appearance_descriptor(image, self._last_bbox)
                if current_appearance is not None:
                    if self._appearance is None:
                        self._appearance = current_appearance
                    else:
                        blended = 0.9 * self._appearance + 0.1 * current_appearance
                        norm = float(np.linalg.norm(blended))
                        self._appearance = blended / norm if norm > 0 else self._appearance
                return ImageDetections2D(image=image, detections=mine)

            replacement = self._reacquire(image, detections)
            if replacement is not None:
                self._target_id = int(replacement.track_id)
                self._last_bbox = tuple(float(v) for v in replacement.bbox)
                self._reacquired_count += 1
                return ImageDetections2D(image=image, detections=[replacement])
            return ImageDetections2D(image=image, detections=mine)

    def _reacquire(self, image: Image, detections: ImageDetections2D):
        """Recover the same person after a tracker ID reset.

        A candidate must match both the target's torso colours and its recent
        location/scale. This prevents the old strict-ID failure without
        silently jumping to an unrelated person elsewhere in the frame.
        """
        if self._appearance is None or self._last_bbox is None:
            return None
        height, width = image.data.shape[:2]
        diagonal = max(1.0, float(np.hypot(width, height)))
        lx1, ly1, lx2, ly2 = self._last_bbox
        last_center = np.array([(lx1 + lx2) / 2.0, (ly1 + ly2) / 2.0])
        last_area = max(1.0, (lx2 - lx1) * (ly2 - ly1))
        best = None
        best_score = 0.0
        for det in detections.detections:
            if det.track_id < 0:
                continue
            bbox = tuple(float(v) for v in det.bbox)
            appearance = _appearance_descriptor(image, bbox)
            appearance_score = _appearance_similarity(self._appearance, appearance)
            if appearance_score < REACQUIRE_MIN_APPEARANCE:
                continue
            x1, y1, x2, y2 = bbox
            center = np.array([(x1 + x2) / 2.0, (y1 + y2) / 2.0])
            center_score = max(
                0.0, 1.0 - float(np.linalg.norm(center - last_center)) / (0.35 * diagonal)
            )
            area = max(1.0, (x2 - x1) * (y2 - y1))
            size_score = min(area, last_area) / max(area, last_area)
            overlap_score = _iou(bbox, self._last_bbox)
            score = (
                0.55 * appearance_score
                + 0.20 * center_score
                + 0.15 * size_score
                + 0.10 * overlap_score
            )
            if score > best_score:
                best, best_score = det, score
        return best if best_score >= REACQUIRE_MIN_SCORE else None

    def lock_track(self, track_id: int) -> None:
        """Lock an already-observed BoT-SORT identity without re-detecting it."""
        with self._lock:
            self._target_id = int(track_id)

    def detect_people(self, image: Image) -> ImageDetections2D:
        """Run the one shared detector without changing the locked target."""
        with self._lock:
            return self._detector.process_image(image)

    def stop(self) -> None:
        # PersonFollowSkillContainer.stop() calls tracker.stop() (EdgeTAM's
        # interface); YOLO needs no teardown but the method must exist.
        with self._lock:
            self._target_id = None
            self._last_bbox = None
            self._appearance = None

    def status(self) -> dict[str, int | None]:
        with self._lock:
            return {
                "target_id": self._target_id,
                "reacquired_count": self._reacquired_count,
            }
