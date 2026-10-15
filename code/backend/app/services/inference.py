"""YOLOv9-c segmentation inference.

Wraps Ultralytics so the rest of the worker never touches torch. Two details are
easy to get wrong:

* **Class indices are not stable.** The mapping from prediction index to class
  name comes from the checkpoint's own ``names`` table and is resolved *by name*
  (``crack``/``spalling``), never by a hard-coded index, because the training run
  decides the order.
* **Legacy checkpoints need a module bridge.** A ``.pt`` is a pickled module
  graph, not a weights-only file, so a checkpoint trained with a patched
  ultralytics (the published CUBIT-InSeg YOLOv9 weights are) cannot be unpickled
  by upstream ultralytics at all - see :mod:`app.services.legacy_yolov9`, which
  is installed before the load. A check that the two "crack"/"spalling" names
  match must not be mistaken for a check that the architecture loads.
* **A mask is not always one ring.** ``Masks.xy`` hands back a *single* ring per
  detection: for a mask with several disconnected parts Ultralytics concatenates
  their contours and bridges them with straight lines
  (``ultralytics.data.converter.merge_multi_segment``). Drawing that ring paints
  a false straight edge across the frame wherever two parts were joined - a
  diagonal that no defect follows. The polygons here are therefore traced from
  the raster mask **per connected component**, so one detection still owns one
  row whose ``mask_polygon`` is a list of rings (one per part) and no ring ever
  contains a bridge.
* **Synthetic fallback.** When the weights file is absent the pipeline emits
  plausible-but-fake polygons instead of failing, so the end-to-end path can be
  exercised before ``best.pt`` exists. This mode is loud in the logs and is
  reported in the task result - synthetic rows must never be mistaken for real
  detections in an accuracy evaluation.

Ultralytics is imported lazily inside ``_load_model``: the API image does not
install torch, and keeping the import out of module scope means this module can
still be imported (for schemas, tests, type checks) without the ML stack.
"""

from __future__ import annotations

import logging
import math
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.db.models import DefectClass

logger = logging.getLogger(__name__)

#: Accepted spellings of the two model classes, so a checkpoint that names them
#: "Crack", "cracks" or "spalling_" still resolves.
CLASS_ALIASES: dict[str, DefectClass] = {
    "crack": DefectClass.CRACK,
    "cracks": DefectClass.CRACK,
    "cracking": DefectClass.CRACK,
    "spall": DefectClass.SPALLING,
    "spalling": DefectClass.SPALLING,
    "spallation": DefectClass.SPALLING,
}

#: Minimum vertices for a usable polygon.
MIN_POLYGON_POINTS = 3


@dataclass(slots=True, frozen=True)
class DetectedDefect:
    """One detected instance in normalised [0, 1] image coordinates.

    Normalised rather than pixel coordinates so a mask stays meaningful if the
    frontend displays the frame at a different resolution than the model saw.

    ``rings`` holds one closed ring per connected part of the mask. A detection
    whose mask covers two patches far apart is still *one* detection (one box,
    one confidence), but it is two rings - keeping them separate is what stops
    the client from joining them with an edge that is not in the mask.
    """

    defect_class: DefectClass
    confidence: float
    rings: list[list[dict[str, float]]]

    def as_row(self, image_id: Any) -> dict[str, Any]:
        return {
            "image_id": image_id,
            "defect_class": self.defect_class,
            "confidence_score": self.confidence,
            "mask_polygon": self.rings,
            "is_verified": False,
        }


class ModelUnavailable(Exception):
    """Raised when the checkpoint cannot be loaded."""


# --------------------------------------------------------------------- loading
# Cached per process: a torch model must not be shared across a Celery prefork
# boundary, and an inherited handle would also pin the parent's memory.
_model_cache: dict[str, Any] = {}
_model_cache_pid: int | None = None


def _load_model(weights_path: str) -> Any:
    """Load the Ultralytics model, or raise :class:`ModelUnavailable`."""
    global _model_cache, _model_cache_pid

    pid = os.getpid()
    if _model_cache_pid != pid:
        # Forked child process: drop anything inherited from the parent.
        _model_cache = {}
        _model_cache_pid = pid

    if weights_path in _model_cache:
        return _model_cache[weights_path]

    path = Path(weights_path)
    if not path.is_file():
        # is_file() is False both for a missing path and for a *directory*, which
        # is what Docker creates when a bind-mounted weights file does not exist.
        # Both mean "no usable weights".
        raise ModelUnavailable(f"weights are not a readable file at '{weights_path}'")

    try:
        from ultralytics import YOLO  # imported here on purpose - see module docstring

        from app.services import legacy_yolov9

        # Must happen before `YOLO(...)` unpickles the checkpoint: fork-trained
        # weights reference `ultralytics.nn.extra_modules.block`, which upstream
        # ultralytics does not define.
        legacy_yolov9.install()

        model = YOLO(weights_path)
        # `names` is the checkpoint's own class table, e.g. {0: 'crack', 1: 'spalling'}.
        logger.info(
            "Loaded YOLO weights '%s' with classes %s", weights_path, getattr(model, "names", "?")
        )
    except Exception as exc:  # noqa: BLE001 - any load failure means synthetic mode
        raise ModelUnavailable(f"could not load '{weights_path}': {exc}") from exc

    _model_cache[weights_path] = model
    return model


# ------------------------------------------------------------------ inference
class InferenceService:
    """Runs the segmentation model over a single frame."""

    def __init__(self, weights_path: str | None = None) -> None:
        self.weights_path = weights_path or settings.YOLO_MODEL_PATH
        self._model: Any | None = None
        self._unavailable_reason: str | None = None
        self._resolved = False

    # ------------------------------------------------------------------ model
    def _model_or_none(self) -> Any | None:
        if not self._resolved:
            self._resolved = True
            try:
                self._model = _load_model(self.weights_path)
            except ModelUnavailable as exc:
                self._unavailable_reason = str(exc)
                self._model = None
        return self._model

    @property
    def is_synthetic(self) -> bool:
        """True when detections are fabricated because the model is missing."""
        return self._model_or_none() is None

    @property
    def unavailable_reason(self) -> str | None:
        self._model_or_none()
        return self._unavailable_reason

    # ----------------------------------------------------------------- detect
    def detect(self, image_path: Path, *, seed: str | None = None) -> list[DetectedDefect]:
        """Detect defects in one frame.

        Raises whatever the model raises for a genuinely bad image (unreadable
        file, decode error) so the caller can mark that one frame ``failed``
        rather than silently producing an empty result.
        """
        model = self._model_or_none()

        if model is None:
            if not settings.YOLO_ALLOW_SYNTHETIC_FALLBACK:
                raise ModelUnavailable(self._unavailable_reason or "model unavailable")
            logger.warning(
                "YOLO weights unavailable (%s) - emitting SYNTHETIC defects for %s. "
                "These rows are not real detections.",
                self._unavailable_reason,
                image_path.name,
            )
            return synthetic_defects(seed or image_path.name)

        # The inference parameters come from settings so all four stay in one
        # place - they are the values the author's experiments were tuned at
        # (conf=0.01, iou=0.7, imgsz=1280, retina masks), not Ultralytics'
        # defaults, and nothing re-applies defaults downstream of here: the
        # polygons below are read straight off the result of this one call.
        results = model.predict(
            source=str(image_path),
            imgsz=settings.YOLO_IMAGE_SIZE,
            conf=settings.YOLO_CONFIDENCE_THRESHOLD,
            iou=settings.YOLO_IOU_THRESHOLD,
            retina_masks=settings.YOLO_RETINA_MASKS,
            device=settings.YOLO_DEVICE,
            verbose=False,
        )
        return self._parse(results, model)

    def _parse(self, results: list[Any], model: Any) -> list[DetectedDefect]:
        names: dict[int, str] = getattr(model, "names", {}) or {}
        detections: list[DetectedDefect] = []

        for result in results:
            boxes = getattr(result, "boxes", None)
            masks = getattr(result, "masks", None)
            if boxes is None or len(boxes) == 0:
                continue

            height, width = (getattr(result, "orig_shape", None) or (1, 1))[:2]
            # The raster mask is the primary source: tracing it part by part is
            # the only route that yields rings without bridges between them. It
            # is only usable while it is in the frame's own coordinates, which
            # is what `retina_masks=True` guarantees; with retina masks off the
            # raster stays at network resolution and the merged `xy` rings are
            # the fallback (still normalised by `orig_shape`, with the same
            # caveat the previous implementation had).
            raster = getattr(masks, "data", None) if masks is not None else None
            raster_in_frame_space = (
                raster is not None
                and tuple(getattr(raster, "shape", ())[-2:]) == (height, width)
            )

            for index in range(len(boxes)):
                raw_name = str(names.get(int(boxes.cls[index]), "")).strip().lower()
                defect_class = CLASS_ALIASES.get(raw_name)
                if defect_class is None:
                    logger.warning("Ignoring prediction with unmapped class %r", raw_name)
                    continue

                rings = self._rings_for(
                    index,
                    masks,
                    raster if raster_in_frame_space else None,
                    width=width,
                    height=height,
                )
                if not rings:
                    # A box without a mask is not usable for this task: the DB
                    # column stores polygons, not a bounding box.
                    continue

                detections.append(
                    DetectedDefect(
                        defect_class=defect_class,
                        confidence=round(float(boxes.conf[index]), 6),
                        rings=rings,
                    )
                )

                if len(detections) >= settings.YOLO_MAX_DETECTIONS_PER_IMAGE:
                    logger.warning(
                        "Reached the %d-detection cap for one frame; truncating",
                        settings.YOLO_MAX_DETECTIONS_PER_IMAGE,
                    )
                    return detections

        return detections

    @classmethod
    def _rings_for(
        cls,
        index: int,
        masks: Any,
        raster: Any | None,
        *,
        width: int,
        height: int,
    ) -> list[list[dict[str, float]]]:
        """Every ring of one detection's mask, in normalised [0, 1] coordinates.

        ``raster`` is the mask tensor when it is already in the frame's own
        coordinates; it is the source of truth. ``masks.xy`` (and its normalised
        twin ``segments``) is only read when the raster is unusable, which keeps
        the expensive contour merge out of the normal path.
        """
        if raster is not None:
            rings = cls._component_rings(raster, index, width=width, height=height)
            if rings:
                return rings

        pixel_polygons = list(getattr(masks, "xy", []) or []) if masks is not None else []
        normalised = list(getattr(masks, "segments", []) or []) if masks is not None else []

        points: Any = None
        already_normalised = False
        if index < len(pixel_polygons):
            points = pixel_polygons[index]
        elif index < len(normalised):
            points, already_normalised = normalised[index], True

        if points is None:
            return []
        ring = cls._ring(
            points, width=width, height=height, already_normalised=already_normalised
        )
        return [ring] if ring else []

    @staticmethod
    def _component_rings(
        raster: Any,
        index: int,
        *,
        width: int,
        height: int,
    ) -> list[list[dict[str, float]]]:
        """Trace the outer contour of every connected part of one mask.

        Returns rings largest-part-first, so the client draws the body of the
        defect before its splinters. A part too small to be a polygon at all
        (fewer than three points) contributes nothing.
        """
        try:
            import cv2
            import numpy as np
        except ImportError:  # pragma: no cover - the worker image ships both
            return []

        mask = raster[index]
        mask = mask.detach().cpu().numpy() if hasattr(mask, "detach") else np.asarray(mask)
        binary = (mask > 0.5).astype("uint8")
        if not binary.any():
            return []

        count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        areas = [int(stats[label, cv2.CC_STAT_AREA]) for label in range(1, count)]
        rings: list[list[dict[str, float]]] = []

        for label in sorted(range(1, count), key=lambda i: areas[i - 1], reverse=True):
            # RETR_EXTERNAL per component: one blob, one outer contour, and - the
            # whole point of this method - no line drawn to any other part.
            contours, _ = cv2.findContours(
                (labels == label).astype("uint8"), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            if not contours:
                continue
            ring = InferenceService._ring(
                max(contours, key=cv2.contourArea).reshape(-1, 2),
                width=width,
                height=height,
                already_normalised=False,
            )
            if ring:
                rings.append(ring)

        return rings

    @staticmethod
    def _ring(
        points: Any,
        *,
        width: int,
        height: int,
        already_normalised: bool,
    ) -> list[dict[str, float]]:
        """Normalise one ring of pixel (or already normalised) points."""
        polygon: list[dict[str, float]] = []
        for point in points:
            x, y = float(point[0]), float(point[1])
            if not already_normalised:
                x, y = x / max(width, 1), y / max(height, 1)
            # Clamp: a mask can legitimately touch the frame edge.
            polygon.append({"x": round(min(max(x, 0.0), 1.0), 6), "y": round(min(max(y, 0.0), 1.0), 6)})
        return polygon if len(polygon) >= MIN_POLYGON_POINTS else []


# --------------------------------------------------------------- synthetic mode
def synthetic_defects(seed: str) -> list[DetectedDefect]:
    """Fabricate crack/spalling-like polygons for pipeline testing.

    Seeded by the caller (the image id), so the same frame always yields the
    same fake detections - a rerun does not produce a different database.
    """
    rng = random.Random(seed)
    detections: list[DetectedDefect] = []

    for _ in range(rng.randint(1, 3)):
        # A thin, slightly rotated band: roughly what a crack mask looks like.
        centre_x = rng.uniform(0.15, 0.85)
        centre_y = rng.uniform(0.15, 0.85)
        half_length = rng.uniform(0.06, 0.18)
        half_width = rng.uniform(0.004, 0.014)
        angle = rng.uniform(0.0, math.pi)
        cos_a, sin_a = math.cos(angle), math.sin(angle)

        corners = [(-half_length, -half_width), (half_length, -half_width),
                   (half_length, half_width), (-half_length, half_width)]
        polygon = []
        for offset_x, offset_y in corners:
            rotated_x = centre_x + offset_x * cos_a - offset_y * sin_a
            rotated_y = centre_y + offset_x * sin_a + offset_y * cos_a
            polygon.append(
                {
                    "x": round(min(max(rotated_x, 0.0), 1.0), 6),
                    "y": round(min(max(rotated_y, 0.0), 1.0), 6),
                }
            )

        detections.append(
            DetectedDefect(
                defect_class=rng.choice([DefectClass.CRACK, DefectClass.SPALLING]),
                confidence=round(rng.uniform(0.35, 0.95), 6),
                rings=[polygon],
            )
        )

    return detections
