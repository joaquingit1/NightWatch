"""PerceiveLoopSkill routed through the shared VisionService.

Two reasons to replace the stock module's private VL model:

1. Stock builds create(detection_model) = MoondreamVlModel, whose load path
   (CPU device + model.compile()) crashes at inference on Apple Silicon
   with "Passed CPU tensor to MPS op". Every look_out_for would error out.
2. Even fixed, it would be a SECOND ~3.6 GB moondream copy next to
   VisionService's on a 16 GB machine. The adapter below forwards
   query_detections to VisionService over its Spec RPC instead, so the
   whole stack holds exactly one copy.

Named PerceiveLoopSkill so blueprint dedupe replaces the stock module.
"""

from typing import Any

from dimos.msgs.sensor_msgs.Image import Image
from dimos.perception.detection.type.detection2d.bbox import Detection2DBBox
from dimos.perception.detection.type.detection2d.imageDetections2D import ImageDetections2D
from dimos.perception.perceive_loop_skill import PerceiveLoopSkill as _StockPerceiveLoopSkill
from dimos.utils.logging_config import setup_logger
from nightwatch.vision import VisionSpec

logger = setup_logger()


class _VisionSpecVlAdapter:
    """Duck-types the VlModel surface PerceiveLoopSkill touches
    (start/stop/query_detections), backed by the VisionService RPC."""

    def __init__(self, module: "PerceiveLoopSkill") -> None:
        self._module = module

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def query_detections(self, image: Image, query: str) -> ImageDetections2D:
        detections: ImageDetections2D = ImageDetections2D(image)
        try:
            boxes = self._module._vision.moondream_detect(query)
        except Exception:
            logger.exception("lookout detect via VisionService failed")
            return detections
        for track_id, box in enumerate(boxes):
            detection = Detection2DBBox(
                bbox=(box[0], box[1], box[2], box[3]),
                track_id=track_id,
                class_id=-1,
                confidence=1.0,
                name=query,
                ts=image.ts,
                image=image,
            )
            if detection.is_valid():
                detections.detections.append(detection)
        return detections


class PerceiveLoopSkill(_StockPerceiveLoopSkill):
    _vision: VisionSpec

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vl_model = _VisionSpecVlAdapter(self)
