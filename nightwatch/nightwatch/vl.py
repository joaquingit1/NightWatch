"""Gemini-backed VL model for Night Watch skills.

Uses Gemini's OpenAI-compatible endpoint through dimos' OpenAIVlModel, with
two fixes discovered by testing (July 23):

1. Explicit base_url + key, kept separate from the OPENAI_* env vars, which
   belong to the agent LLM (OpenAgents gateway, text-only).
2. Prompt augmentation: dimos' bbox prompt never states the image size or
   that coordinates must be pixels. Qwen-VL assumes pixels; Gemini defaults
   to normalized coords and returns garbage for the stock prompt. We append
   an explicit pixel-coordinate contract with the true image dimensions.
   Verified: stock prompt -> malformed 5-element bbox; augmented prompt ->
   correct pixel bbox within ~20 px.

Free-tier note: gemini-2.5-flash allows roughly 10 requests/min and a few
hundred per day. Fine for follow-start detections and occasional queries;
do not put it inside a per-frame loop.
"""

from functools import cached_property
import os
from typing import Any

import numpy as np
from openai import OpenAI

from dimos.models.vl.openai import OpenAIVlModel
from dimos.msgs.sensor_msgs.Image import Image

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-2.5-flash-lite"


class GeminiVlModel(OpenAIVlModel):
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("model_name", GEMINI_MODEL)
        super().__init__(**kwargs)

    @cached_property
    def _client(self) -> OpenAI:
        api_key = self.config.api_key or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not set (see robot.env)")
        return OpenAI(base_url=GEMINI_BASE_URL, api_key=api_key)

    def query(self, image: Image | np.ndarray, query: str) -> str:  # type: ignore[override]
        # Gemini is heavily trained to emit [ymin, xmin, ymax, xmax] normalized
        # to 0-1000 and reverts to it regardless of instructions (observed:
        # complied with a pixel-coords request on one call, ignored it on the
        # next). So request its NATIVE format deterministically and convert to
        # pixel [x1, y1, x2, y2] here, which is what dimos consumers expect.
        if not isinstance(image, Image):
            return super().query(image, query)
        h, w = image.data.shape[:2]
        query = (
            f"{query}\n\nIMPORTANT: output any bbox in YOUR native detection "
            "format: [ymin, xmin, ymax, xmax], integers normalized to 0-1000."
        )
        response = super().query(image, query)
        return self._bbox_to_pixels(response, w, h)

    @staticmethod
    def _bbox_to_pixels(response: str, w: int, h: int) -> str:
        """Normalize Gemini detection output to {'name', 'bbox': pixel xyxy}.

        Observed shapes (all really produced by Gemini):
        - {'name': ..., 'bbox': [ymin, xmin, ymax, xmax]}       (as requested)
        - {'box_2d': [...], 'label': ...}                        (native)
        - [ {...}, {...} ]  a LIST when several objects match    (native)
        All coords normalized 0-1000. For a list we merge boxes into their
        union: 'the water bottles' means the whole cluster.
        """
        import json
        import re

        def parse_any(text: str) -> object | None:
            # dimos' extract_json_from_llm_response returns None for JSON
            # ARRAYS, which is exactly what Gemini emits for multi-object
            # detections, so we parse ourselves (fences, then bare blocks).
            m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
            if m:
                text = m.group(1)
            text = text.strip()
            try:
                return json.loads(text)
            except Exception:
                pass
            for open_, close in (("[", "]"), ("{", "}")):
                s, e = text.find(open_), text.rfind(close)
                if s != -1 and e > s:
                    try:
                        return json.loads(text[s : e + 1])
                    except Exception:
                        continue
            return None

        result = parse_any(response)

        def box_of(d: object) -> list[float] | None:
            if not isinstance(d, dict):
                return None
            raw = d.get("bbox") or d.get("box_2d")
            try:
                vals = [float(v) for v in raw]  # type: ignore[union-attr]
                return vals if len(vals) == 4 else None
            except (TypeError, ValueError):
                return None

        if isinstance(result, list):
            boxes = [b for b in (box_of(d) for d in result) if b]
            if not boxes:
                return response
            merged = [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]
            name = next(
                (d.get("name") or d.get("label") for d in result if isinstance(d, dict)),
                "object",
            )
            ymin, xmin, ymax, xmax = merged
        else:
            single = box_of(result)
            if single is None:
                return response
            ymin, xmin, ymax, xmax = single
            name = result.get("name") or result.get("label") or "object"  # type: ignore[union-attr]

        return json.dumps(
            {
                "name": name,
                "bbox": [
                    round(xmin / 1000 * w, 1),
                    round(ymin / 1000 * h, 1),
                    round(xmax / 1000 * w, 1),
                    round(ymax / 1000 * h, 1),
                ],
            }
        )
