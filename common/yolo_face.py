from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from ultralytics import YOLO

from .assets import download_file

YOLOV8_FACE_URL = (
    "https://github.com/lindevs/yolov8-face/releases/latest/download/"
    "yolov8n-face-lindevs.pt"
)
YOLOV8_FACE_SHA256 = "b038ca653b503453a94f6e12d76feca6840b2a97d7a1322b4498c5e922f29832"


def ensure_yolov8_face_model(path: Path) -> Path:
    return download_file(
        YOLOV8_FACE_URL, path, expected_sha256=YOLOV8_FACE_SHA256, timeout=120
    )


def resolve_torch_device(requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "0"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


@dataclass(frozen=True)
class FaceBox:
    x1: int
    y1: int
    x2: int
    y2: int
    confidence: float

    @property
    def area(self) -> int:
        return max(0, self.x2 - self.x1) * max(0, self.y2 - self.y1)


class YOLOFaceDetector:
    def __init__(
        self,
        model_path: Path,
        *,
        confidence: float = 0.35,
        image_size: int = 640,
        device: str = "auto",
    ) -> None:
        self.model_path = ensure_yolov8_face_model(model_path)
        self.model = YOLO(str(self.model_path))
        self.confidence = confidence
        self.image_size = image_size
        self.device = resolve_torch_device(device)

    def detect(self, bgr_image: np.ndarray) -> list[FaceBox]:
        results = self.model.predict(
            source=bgr_image,
            conf=self.confidence,
            imgsz=self.image_size,
            max_det=20,
            verbose=False,
            device=self.device,
        )
        boxes: list[FaceBox] = []
        if not results or results[0].boxes is None:
            return boxes
        height, width = bgr_image.shape[:2]
        coordinates = results[0].boxes.xyxy.detach().cpu().numpy()
        confidences = results[0].boxes.conf.detach().cpu().numpy()
        for coords, confidence in zip(coordinates, confidences, strict=True):
            x1, y1, x2, y2 = coords.round().astype(int).tolist()
            boxes.append(
                FaceBox(
                    x1=max(0, min(width - 1, x1)),
                    y1=max(0, min(height - 1, y1)),
                    x2=max(1, min(width, x2)),
                    y2=max(1, min(height, y2)),
                    confidence=float(confidence),
                )
            )
        return boxes


def padded_box(box: FaceBox, image_shape: tuple[int, ...], padding: float) -> FaceBox:
    height, width = image_shape[:2]
    pad_x = int((box.x2 - box.x1) * padding)
    pad_y = int((box.y2 - box.y1) * padding)
    return FaceBox(
        x1=max(0, box.x1 - pad_x),
        y1=max(0, box.y1 - pad_y),
        x2=min(width, box.x2 + pad_x),
        y2=min(height, box.y2 + pad_y),
        confidence=box.confidence,
    )

