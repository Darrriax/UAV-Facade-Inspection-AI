"""Project request/response contracts."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectBase(BaseModel):
    name: str = Field(min_length=1, max_length=255, examples=["Residential complex 'Soniachnyi'"])
    # Free-form address/object description; a dedicated geo type can replace it
    # later without touching the rest of the layer.
    location: str | None = Field(default=None, max_length=512, examples=["Kyiv, Velyka Vasylkivska 12"])
    # TEXT column, so no length cap beyond a sanity bound.
    description: str | None = Field(default=None, max_length=10_000)
    needs_3d_model: bool = Field(
        default=False,
        description="Whether a .glb reconstruction is expected for this project.",
    )
    detect_defects: bool = Field(
        default=True,
        description=(
            "Whether frames are analysed for defects. False is a 3D-only project: "
            "its frames are marked analysed without being passed to the detector, "
            "so they carry no masks."
        ),
    )


class ProjectCreate(ProjectBase):
    """Payload for opening an inspection campaign.

    There is deliberately no ``user_id`` field: the owner is always taken from
    the authenticated principal, never from client input. ``extra="forbid"``
    rejects a payload that tries to set it anyway.

    ``main_photo_s3_path`` is omitted on purpose - see :class:`ProjectUpdate`.
    """

    model_config = ConfigDict(extra="forbid")


class ProjectUpdate(BaseModel):
    """Partial update of a project.

    ``main_photo_s3_path`` is meant to be written by the cover-photo upload
    flow. Because the value is an object key that the API would later turn into
    a presigned URL, accepting an arbitrary string here would let a caller point
    their project at *another* project's photo. The service therefore requires
    the key to live under this project's own prefix.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    location: str | None = Field(default=None, max_length=512)
    description: str | None = Field(default=None, max_length=10_000)
    main_photo_s3_path: str | None = Field(default=None, max_length=1024)
    needs_3d_model: bool | None = None
    detect_defects: bool | None = None


class ProjectStatus(str, enum.Enum):
    """Rollup of a project's frame statuses, for the dashboard badge.

    Derived on read, never stored: a column would have to be kept in sync with
    the frames it summarises, and the YOLO/3D pipeline changes frame statuses
    from a different process (the Celery worker).
    """

    #: No frames uploaded yet.
    DRAFT = "draft"
    #: Frames are stored but none has been analysed - the wizard may still be
    #: uploading them, or the upload finished and the batch has not been queued.
    UPLOADING = "uploading"
    #: At least one frame is being analysed right now.
    PROCESSING = "processing"
    #: The user stopped the analysis and frames are still waiting, so it can be
    #: resumed. Ordered before ``processing`` because a stop wins over whatever
    #: the worker was doing when it was requested.
    PAUSED = "paused"
    #: Analysis finished; individual frames may still have failed (see ``stats``).
    COMPLETED = "completed"
    #: Nothing completed - every frame that was attempted failed.
    FAILED = "failed"


class ProjectStats(BaseModel):
    """Aggregates over one project's frames and their defects.

    The project rows themselves carry no counters, so the dashboard grid would
    otherwise need one request per card. These are computed with three grouped
    queries for a whole page of projects (see ``ProjectService.stats_for``).

    ``total_defects`` and ``verified_defects`` count *objects* - one crack on one
    frame, however many fragments the model reported it as - because that is what
    the workspace lists and what a reviewer signs off. Detection rows are an
    implementation detail of the detector; their total is available from the
    defect listing. An object is verified only once every one of its fragments
    is, which is what the reviewer sees.
    """

    total_images: int = 0
    pending_images: int = 0
    processing_images: int = 0
    completed_images: int = 0
    failed_images: int = 0
    #: Number of objects across the project's frames, not of detection rows.
    total_defects: int = 0
    #: Number of those objects the reviewer confirmed, whole objects only.
    verified_defects: int = 0


class ProjectRead(ProjectBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    main_photo_s3_path: str | None = None
    created_at: datetime
    updated_at: datetime
    stats: ProjectStats = Field(
        default_factory=ProjectStats,
        description="Frame and defect rollup for the project cards.",
    )
    status: ProjectStatus = Field(
        default=ProjectStatus.DRAFT,
        description="Badge state, derived from `stats` and the stop flag.",
    )
    processing_stopped_at: datetime | None = Field(
        default=None,
        description=(
            "When the user stopped this project's analysis, or null while it may run. "
            "A stopped project keeps its pending frames; resume it with "
            "`POST /projects/{id}/start-processing`."
        ),
    )
    model3d_requested_at: datetime | None = Field(
        default=None,
        description=(
            "When a 3D reconstruction was queued, or null when none is in flight. "
            "A running reconstruction belongs to a worker process, so this is what "
            "lets the 3D page report it - and what keeps it from offering to start "
            "a second one. Cleared when the build finishes, successfully or not."
        ),
    )
    model3d_error: str | None = Field(
        default=None,
        description=(
            "Why the last 3D reconstruction produced no model, or null when the last "
            "one succeeded. `GET /projects/{id}/model3d` answers 404 in both cases; "
            "this is what tells them apart."
        ),
    )
    model3d_progress: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "How far the running reconstruction has got, 0-100, or null when none "
            "is running - the worker writes it as NodeODM reports it. Null while "
            "`model3d_requested_at` is set means the build has not reported its "
            "first percentage yet (still queued, or fetching frames), which is a "
            "different thing from 0% and is why it is not defaulted to zero."
        ),
    )
