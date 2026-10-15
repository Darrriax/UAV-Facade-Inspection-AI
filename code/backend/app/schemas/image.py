"""Inspection-image request/response contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models import ImageStatus


class InspectionImageRead(BaseModel):
    """A stored UAV frame.

    ``s3_path`` is the object key rather than a URL: presigned URLs expire, so
    the client asks for a fresh one when it needs to display the bytes.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    file_path: str
    s3_path: str | None
    status: ImageStatus
    created_at: datetime
    updated_at: datetime


class ReviewFrameRead(BaseModel):
    """One frame as the review workspace reads it: metadata plus a signed URL.

    Deliberately separate from :class:`InspectionImageRead`, which stays
    URL-free because the progress screen polls it: signing a URL costs an HMAC
    per row, and a poll tick must not pay that for frames nobody is looking at.
    This endpoint is read once per review session instead.

    It exists so the carousel can hold **every** uploaded frame, including the
    ones with no detections at all: a reviewer has to be able to page through
    the whole sortie, and a photo the model found nothing on is a result too.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    file_path: str
    status: ImageStatus
    #: Short-lived GET URL for the frame. ``None`` when the row has no object
    #: key, which the UI renders as "no preview" rather than a broken image.
    image_url: str | None = None
    #: Lifetime of ``image_url`` in seconds from the moment it was signed.
    image_url_expires_in: int = 0


class BulkImageUploadResponse(BaseModel):
    """Result of storing one upload chunk.

    Storage and analysis are separate steps: a full sortie arrives as many
    chunked requests, and starting analysis per chunk would queue work for
    frames the following chunks have not delivered yet. So this response says
    what *this* request stored - the client accumulates the total - and nothing
    about tasks. The frames stay ``pending`` until the client calls
    ``POST /projects/{id}/start-processing``.

    ``uploaded`` counts the chunk; ``images`` are the rows it created.
    """

    uploaded: int
    images: list[InspectionImageRead]


class ProcessingStartResponse(BaseModel):
    """Acknowledgement that a project's pending frames were queued for analysis.

    ``task_id`` is the Celery task id - useful for correlating a run with the
    worker's logs. ``pending_images`` is how many frames the worker will find
    (0 when an earlier run already claimed them, which makes a repeated call a
    no-op rather than an error).
    """

    task_id: str
    pending_images: int


class ImageStatsRead(BaseModel):
    """Status breakdown of one project's frames, for the progress screen.

    A dedicated aggregate rather than a page of rows: the wizard polls this
    every few seconds, and adding up thousands of frames client-side would mean
    paging the whole collection on every tick. Every status is present even at
    zero, so the UI can render a stable set of counters.
    """

    total: int
    pending: int
    processing: int
    completed: int
    failed: int
