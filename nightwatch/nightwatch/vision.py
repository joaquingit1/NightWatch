"""Shared local vision: one moondream2 for the whole stack.

Why this exists (July 23, verified on saved live frames):

1. OpenAI models are excellent at recognizing but HALLUCINATE pixel
   coordinates: gpt-4o-mini put a confident "person" bbox on an empty water
   dispenser while local YOLO nailed both real people. So OpenAI stays the
   agent brain; localization must be local.
2. dimos already ships the right localizer, MoondreamVlModel: its native
   detect API returned tight boxes for "person", every individual water
   bottle, and "person in the white shirt" (2.9-7.7 s/query at the built-in
   512px resize). But stock loads it on CPU ("cuda else cpu", no MPS branch)
   and calls model.compile(), which crashes at inference on this Mac with
   "Passed CPU tensor to MPS op". MoondreamMps loads fp16 pinned to MPS with
   no compile (bench-verified recipe).
3. The model is ~3.6 GB resident. goto, follow, and the perceive loop all
   need detections; three module copies would exhaust a 16 GB machine. So
   ONE VisionService module owns the model and everyone else calls it
   through VisionSpec (dimos' structural Spec injection, same mechanism as
   GO2ConnectionSpec). The model warms up on a background thread at start
   so the first query doesn't pay the ~13 s load.

Device selection (updated July 24): the default is "auto", which does NOT
load any local model and does NOT touch torch or MPS. dimos hosts
VisionService inside a forkserver worker where the first Metal kernel
compile dies as a NATIVE abort inside MPSKernelDAG.mm (a failed assertion
that calls abort(), delivering SIGABRT), not as a catchable Python
exception. There is therefore no safe in-process MPS probe: any probe kills
the worker and silently collapses the whole stack. auto disables local
moondream and vision RPCs return empty results. Load a model only through
the explicit opt-ins: NIGHTWATCH_MOONDREAM_DEVICE=mps (safe only outside a
forkserver worker) or NIGHTWATCH_MOONDREAM_DEVICE=cpu.
"""

from functools import cached_property
import os
import threading
from typing import Any, Protocol

import torch
from transformers import AutoModelForCausalLM

from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import In
from dimos.models.vl.moondream import MoondreamVlModel
from dimos.msgs.sensor_msgs.Image import Image
from dimos.spec.utils import Spec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class MoondreamMps(MoondreamVlModel):
    @cached_property
    def _model(self) -> AutoModelForCausalLM:
        # fp16, everything pinned to MPS, and NO model.compile(): stock's
        # CPU + compile load path crashes at inference on Apple Silicon.
        return AutoModelForCausalLM.from_pretrained(
            self.config.model_name,
            trust_remote_code=self.config.trust_remote_code,
            torch_dtype=torch.float16,
            device_map={"": "mps"},
        )


class MoondreamCpu(MoondreamVlModel):
    @cached_property
    def _model(self) -> AutoModelForCausalLM:
        # fp32 CPU with NO model.compile(). Slow (tens of seconds per query)
        # and ~7.5 GB resident, which is why it is NEVER loaded automatically
        # on this 16 GB machine: the first auto-fallback attempt OOM-killed
        # the VisionService worker and collapsed the whole stack. Only the
        # explicit NIGHTWATCH_MOONDREAM_DEVICE=cpu opt-in reaches this class.
        return AutoModelForCausalLM.from_pretrained(
            self.config.model_name,
            trust_remote_code=self.config.trust_remote_code,
            torch_dtype=torch.float32,
            device_map={"": "cpu"},
        )


class VisionSpec(Spec, Protocol):
    def moondream_detect(self, query: str, max_objects: int = 5) -> list[list[float]]: ...
    def moondream_query(self, question: str) -> str: ...


class VisionService(Module):
    color_image: In[Image]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._model: MoondreamVlModel | None = None
        self._model_unavailable = False
        self._model_lock = threading.Lock()
        self._latest_image: Image | None = None
        self._image_lock = threading.Lock()

    @rpc
    def start(self) -> None:
        super().start()
        from reactivex.disposable import Disposable

        self.register_disposable(
            Disposable(self.color_image.subscribe(self._on_image))
        )
        threading.Thread(
            target=self._warmup, name="VisionService-warmup", daemon=True
        ).start()

    @rpc
    def stop(self) -> None:
        with self._model_lock:
            if self._model is not None:
                try:
                    self._model.stop()
                except Exception:
                    logger.exception("moondream stop failed")
                self._model = None
        super().stop()

    def _on_image(self, image: Image) -> None:
        with self._image_lock:
            self._latest_image = image

    def _warmup(self) -> None:
        try:
            self._get_model()
            logger.info("VisionService moondream warm")
        except Exception:
            logger.exception("VisionService warmup failed")

    def _get_model(self) -> MoondreamVlModel | None:
        with self._model_lock:
            if self._model is None and not self._model_unavailable:
                requested = os.environ.get(
                    "NIGHTWATCH_MOONDREAM_DEVICE", "auto"
                ).lower()
                # There is deliberately NO in-process MPS probe on the auto
                # path. dimos runs VisionService in a forkserver worker where
                # the first Metal kernel compile fails as a NATIVE abort inside
                # MPSKernelDAG.mm (a failed assertion that calls abort() and
                # delivers SIGABRT), not as a catchable Python exception. A
                # probe would therefore kill this worker (taking MovementManager
                # and NightwatchNavigation with it) and the coordinator would
                # tear the whole stack down silently. Verified live on
                # 2026-07-24: the boot log showed "Unable to reach
                # MTLCompilerService ... MPSKernelDAG.mm:1889: failed assertion"
                # followed immediately by a silent full-stack teardown. So auto
                # touches neither torch nor MPS and disables local moondream;
                # a model loads only through an explicit device opt-in.
                if requested == "cpu":
                    model: MoondreamVlModel | None = MoondreamCpu()
                elif requested == "mps":
                    model = MoondreamMps()
                else:
                    model = None
                if model is None:
                    self._model_unavailable = True
                    logger.warning(
                        "Local moondream is disabled in dimos workers on this "
                        "machine: the auto path does not probe MPS because the "
                        "failure mode is a native SIGABRT that would take the "
                        "worker and the whole stack down. Vision RPCs will "
                        "return empty results. Opt in explicitly with "
                        "NIGHTWATCH_MOONDREAM_DEVICE=mps (only when running "
                        "outside a forkserver worker) or "
                        "NIGHTWATCH_MOONDREAM_DEVICE=cpu."
                    )
                else:
                    logger.info(
                        "VisionService moondream device selected",
                        device=requested,
                        requested=requested,
                    )
                    model._model  # noqa: B018 -- force the weights load here
                    self._model = model
            return self._model

    @rpc
    def moondream_detect(self, query: str, max_objects: int = 5) -> list[list[float]]:
        """Detect `query` on the latest camera frame; pixel [x1, y1, x2, y2] boxes."""
        with self._image_lock:
            image = self._latest_image
        if image is None:
            logger.warning("moondream_detect with no camera frame yet")
            return []
        model = self._get_model()
        if model is None:
            return []
        import time as _time

        t0 = _time.monotonic()
        # Serialize inference: the model is not thread-safe and shares one GPU.
        with self._model_lock:
            detections = model.query_detections(image, query, max_objects=max_objects)
        boxes = [[float(v) for v in d.bbox] for d in detections.detections]
        logger.info(
            "moondream_detect",
            query=query,
            boxes=len(boxes),
            seconds=round(_time.monotonic() - t0, 2),
        )
        return boxes

    @rpc
    def moondream_query(self, question: str) -> str:
        """Answer one question about the latest frame using the shared model.

        Semantic area tagging uses this at a very low cadence. Keeping it in
        this service avoids loading a second multi-gigabyte VLM merely to name
        rooms.
        """
        with self._image_lock:
            image = self._latest_image
        if image is None:
            return ""
        model = self._get_model()
        if model is None:
            return ""
        import time as _time

        started = _time.monotonic()
        with self._model_lock:
            answer = model.query(image, question)
        logger.info(
            "moondream_query",
            seconds=round(_time.monotonic() - started, 2),
            answer=str(answer)[:160],
        )
        return str(answer)
