"""Defect request/response contracts.

A defect is never read on its own: reviewing one means looking at the frame it
was found on, so the response carries the frame's metadata **and** a fresh
presigned URL for its bytes alongside the polygon. Fetching that separately
would mean one extra request per card on the workspace screen.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models import DefectClass

#: A mask is a handful of polygons; these caps are abuse guards, not product
#: limits. They bound the JSONB payload a single request can write.
MAX_MASK_RINGS = 50
MAX_RING_VERTICES = 2_000


class DefectRead(BaseModel):
    """One detected instance, plus everything the review card needs.

    ``mask_polygon`` is in normalized [0, 1] image coordinates, which is what
    lets the client draw it over the frame at any display size without knowing
    the model's input resolution. It is a **list of rings** - one per connected
    part of the mask - because a detection whose mask covers two separate
    patches must not be drawn as one ring, which would close the gap between
    them with an edge that is not in the mask. Rows written before that
    distinction existed hold a single ring and are wrapped on the way out, so a
    stored result keeps rendering instead of failing validation.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    image_id: uuid.UUID
    defect_class: DefectClass
    confidence_score: float
    mask_polygon: list[list[dict[str, float]]] | None
    is_verified: bool
    #: Which object on this frame the detection belongs to: rows sharing it are
    #: one piece of damage. Assigned when the frame's detections were written
    #: (``app.services.mask_objects``), so the client groups the review list by
    #: this field instead of re-deriving the geometry, and a count of distinct
    #: values is the count of defects on the frame.
    object_index: int = 0
    created_at: datetime
    updated_at: datetime

    @field_validator("mask_polygon", mode="before")
    @classmethod
    def _as_ring_list(cls, value: object) -> object:
        """Accept the legacy single-ring shape: ``[point, ...]`` -> ``[[...]]``."""
        if not isinstance(value, (list, tuple)) or not value:
            return value
        first = value[0]
        if isinstance(first, dict):
            return [list(value)]
        return value

    # --- the frame this defect was found on ------------------------------
    #: Original client filename, shown as the card's caption.
    image_file_path: str = ""
    #: Short-lived GET URL for the frame. ``None`` when the row has no object
    #: key, which the UI renders as "no preview" rather than a broken image.
    image_url: str | None = None
    #: Lifetime of ``image_url`` in seconds, so a long-open screen knows when to
    #: ask again instead of showing a stale URL.
    image_url_expires_in: int = 0


class DefectUpdate(BaseModel):
    """Payload of the review edit: the two fields a reviewer may change.

    Both are optional, but an edit that changes nothing is rejected rather than
    silently accepted as a no-op - a request that means to change something and
    does not is a client bug, and a 422 says so.

    ``mask_polygon`` is what the eraser sends: the geometry that is left after
    the inspector rubbed part of a mask out. It has to be *replaced* rather than
    patched in place for the same reason a mask is a list of rings at all - half
    of a polygon is not a polygon.
    """

    model_config = ConfigDict(extra="forbid")

    is_verified: bool | None = Field(
        default=None,
        description="True once a human has confirmed the detection.",
    )
    #: The remaining rings after an erase; at least one, because an empty mask
    #: means the detection should be deleted, not stored as nothing.
    mask_polygon: list[list[MaskPoint]] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_MASK_RINGS,
    )

    @model_validator(mode="after")
    def _changes_something(self) -> DefectUpdate:
        if self.is_verified is None and self.mask_polygon is None:
            raise ValueError("Send is_verified, mask_polygon, or both.")
        return self

    @field_validator("mask_polygon")
    @classmethod
    def _rings_are_polygons(
        cls, value: list[list[MaskPoint]] | None
    ) -> list[list[MaskPoint]] | None:
        if value is None:
            return None
        for ring in value:
            if len(ring) < 3:
                raise ValueError("Every mask ring needs at least 3 points.")
            if len(ring) > MAX_RING_VERTICES:
                raise ValueError(
                    f"A mask ring may hold at most {MAX_RING_VERTICES} points."
                )
        return value


class MaskPoint(BaseModel):
    """One vertex of a hand-drawn mask, in normalized [0, 1] frame coordinates.

    Normalized rather than pixel coordinates for the same reason the model's
    masks are: the client draws over the photo at whatever size it is displayed.
    """

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class DefectCreate(BaseModel):
    """A mask drawn by a human over one frame.

    The client sends the geometry and the class - nothing else. ``confidence_score``
    is fixed server-side at ``1.0`` (a human drew this, so there is no model
    probability to report) and ``is_verified`` is recorded as ``true``: unlike a
    detection that awaits review, a hand-drawn mask *is* the review.
    """

    model_config = ConfigDict(extra="forbid")

    defect_class: DefectClass
    #: One closed ring per drawn polygon; three vertices are the minimum that
    #: encloses an area, so a degenerate two-point "ring" is rejected here
    #: rather than stored as a mask nothing can render.
    mask_polygon: list[list[MaskPoint]] = Field(min_length=1, max_length=MAX_MASK_RINGS)

    @field_validator("mask_polygon")
    @classmethod
    def _rings_are_polygons(
        cls, value: list[list[MaskPoint]]
    ) -> list[list[MaskPoint]]:
        for ring in value:
            if len(ring) < 3:
                raise ValueError("Every mask ring needs at least 3 points.")
            if len(ring) > MAX_RING_VERTICES:
                raise ValueError(
                    f"A mask ring may hold at most {MAX_RING_VERTICES} points."
                )
        return value
