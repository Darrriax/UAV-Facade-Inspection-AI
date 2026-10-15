"""Inspection-image business logic.

Shared by the upload endpoint and by the Celery task, which is the whole point
of keeping services framework-agnostic: the task reuses the exact same status
transitions the API exposes.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.storage import (
    StorageBackend,
    get_image_storage,
    image_object_key,
)
from app.db.models import Defect, ImageStatus, InspectionImage, Project
from app.repositories.defect import DefectRepository
from app.repositories.image import InspectionImageRepository
from app.schemas.image import ReviewFrameRead
from app.services.inference import DetectedDefect
from app.services.mask_objects import group_object_indices

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IncomingImage:
    """One validated file from a multipart request.

    Produced by the API layer (which owns transport-level limits such as size
    and content type) and consumed here, so the service never has to know about
    ``UploadFile``.
    """

    #: Sanitised original filename, kept for traceability.
    filename: str
    #: Validated extension, derived server-side - never trusted verbatim.
    suffix: str
    #: Content type we will declare to S3 (from our own whitelist).
    content_type: str
    size: int
    fileobj: BinaryIO


class ImageService:
    def __init__(self, session: Session, storage: StorageBackend | None = None) -> None:
        self.session = session
        # Injectable so tests can substitute a fake and never touch S3.
        self.storage = storage if storage is not None else get_image_storage()
        self.images = InspectionImageRepository(session)
        self.defects = DefectRepository(session)

    # ----------------------------------------------------------------- write
    def store_uploads(
        self,
        project: Project,
        uploads: Sequence[IncomingImage],
    ) -> list[InspectionImage]:
        """Store a batch of frames and register them as ``pending``.

        All-or-nothing: the objects are written first and the database rows only
        afterwards, so a failure part-way through leaves no half-populated
        project. Objects already written are removed again on the way out.

        Note that a large batch is bounded by the request limits, not by this
        method; for truly unbounded bulk ingestion the client should upload
        straight to S3 with a presigned PUT and register the keys here.
        """
        prepared: list[tuple[uuid.UUID, str, IncomingImage]] = []
        for upload in uploads:
            # Pre-generated so the object key and the primary key agree, which
            # makes an orphaned object easy to trace back to its row.
            image_id = uuid.uuid4()
            prepared.append(
                (image_id, image_object_key(project.id, image_id, upload.suffix), upload)
            )

        stored_keys: list[str] = []
        try:
            for _, key, upload in prepared:
                upload.fileobj.seek(0)
                self.storage.upload_stream(upload.fileobj, key, upload.content_type)
                stored_keys.append(key)
        except Exception:
            self._rollback_objects(stored_keys)
            raise

        images = [
            self.images.create(
                {
                    "id": image_id,
                    "project_id": project.id,
                    "file_path": upload.filename,
                    "s3_path": key,
                    "status": ImageStatus.PENDING,
                }
            )
            for image_id, key, upload in prepared
        ]
        self.session.commit()
        return images

    def delete(self, image: InspectionImage) -> None:
        """Remove a frame from storage and the database."""
        if image.s3_path:
            self.storage.delete(image.s3_path)
        self.images.delete(image)
        self.session.commit()

    # --------------------------------------------------------------- analysis
    def claim_pending(
        self,
        project_id: uuid.UUID,
        *,
        limit: int | None = None,
    ) -> list[InspectionImage]:
        """Reserve a project's pending frames for this worker and mark them processing.

        Commits immediately: the caller is about to spend minutes on CPU-bound
        inference, and holding a transaction (and its pooled connection) open for
        that long would starve the API.
        """
        images = list(self.images.get_pending_for_update(project_id, limit=limit))
        if not images:
            return []

        for image in images:
            image.status = ImageStatus.PROCESSING
        self.session.commit()
        logger.info("Claimed %d pending image(s) for project %s", len(images), project_id)
        return images

    def release_claimed(self, image_ids: Sequence[uuid.UUID]) -> int:
        """Undo a claim for frames a stopped run never reached.

        ``claim_pending`` flips every pending frame to ``processing`` up front, so
        a stop has to put the untouched ones back - otherwise they would sit in
        ``processing`` forever and the resumed run would find nothing to claim.

        Only rows that are still ``processing`` move, so a frame that finished
        (or failed) in the meantime keeps its result.
        """
        released = self.images.release_processing(list(image_ids))
        self.session.commit()
        return released

    def requeue_stale(self, project_id: uuid.UUID, *, older_than_seconds: int) -> int:
        """Reclaim the frames a run that died is still holding.

        Wrapper around the repository statement, kept here so the caller (the
        Celery task) never talks to a repository directly - the same shape as
        :meth:`release_claimed`. The count is returned so a caller can report
        that a previous run had to be cleaned up after, which is a different
        event from "the project was simply finished".
        """
        requeued = self.images.requeue_stale_processing(
            project_id, older_than_seconds=older_than_seconds
        )
        self.session.commit()
        return requeued

    def download(self, image: InspectionImage, directory: Path) -> Path:
        """Fetch a frame from object storage into ``directory``.

        Named after the object key rather than the original filename, so two
        frames that happened to share a client-side name cannot collide on disk.
        """
        if not image.s3_path:
            raise ValueError(f"Image {image.id} has no stored object key")
        return self.storage.download_to(image.s3_path, directory / Path(image.s3_path).name)

    def store_defects(
        self,
        image: InspectionImage,
        detections: Sequence[DetectedDefect],
    ) -> list[Defect]:
        """Persist detections for one frame and mark it completed.

        The rows are grouped before they are written: at a low confidence
        threshold the model reports one crack as several overlapping fragments,
        and ``defects.object_index`` is what records which of them are the same
        damage (see :mod:`app.services.mask_objects`). Deciding it here, next to
        the inference that produced the masks, is what lets every screen count
        the same objects without any of them running geometry.

        One transaction per frame: if frame 70 of 100 explodes, the first 69
        detections survive rather than being rolled back with it.
        """
        rows = [detection.as_row(image.id) for detection in detections]
        for row, object_index in zip(
            rows,
            group_object_indices([(d.defect_class.value, d.rings) for d in detections]),
            strict=True,
        ):
            row["object_index"] = object_index

        defects = self.defects.bulk_create(rows)
        image.status = ImageStatus.COMPLETED
        self.session.commit()
        return defects

    def mark_completed(self, image: InspectionImage) -> None:
        """Mark a frame analysed while storing no detection at all.

        The one path that produces a `completed` frame with zero `Defect` rows,
        and it is deliberate rather than a gap: a project can be set up to skip
        defect detection entirely (`Project.detect_defects`), and its frames
        still have to reach a terminal status - otherwise the progress screen
        waits forever on frames nothing is going to look at, and the project
        never leaves `processing`.

        Nothing is written to ``defects``, so such a frame has no masks and
        counts as zero objects, which is exactly what the workspace has to show
        for it. Distinct from :meth:`store_defects` with an empty list only in
        intent - this one asserts that no detector ran.
        """
        image.status = ImageStatus.COMPLETED
        self.session.commit()

    def mark_failed(self, image: InspectionImage, reason: str) -> None:
        """Flag a frame as failed.

        Committed separately so a later failure cannot roll this back, and
        logged with the reason because ``InspectionImage`` has no error column.
        """
        image.status = ImageStatus.FAILED
        self.session.commit()
        logger.error("Image %s (%s) failed: %s", image.id, image.file_path, reason)

    # ------------------------------------------------------------------ read
    def stored_frames(self, project_id: uuid.UUID) -> Sequence[InspectionImage]:
        """Every frame of a project that can be downloaded, oldest first.

        A reconstruction reads this instead of a page of frames: NodeODM solves
        the camera network from the whole set, so handing it a page would not be
        a smaller model but a worse one. The order is upload order, which is the
        order the sortie was flown.
        """
        return self.images.get_many_stored_by_project(project_id)

    def list_by_project(
        self,
        project_id: uuid.UUID,
        *,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[InspectionImage], int]:
        return (
            self.images.get_many_by_project(project_id, offset=offset, limit=limit),
            self.images.count_by_project(project_id),
        )

    def present_frames(self, images: Sequence[InspectionImage]) -> list[ReviewFrameRead]:
        """Attach a freshly signed URL to each frame, for the review carousel.

        A signing failure is logged and the frame is left without a URL rather
        than failing the whole page: one unreadable object must not blank out the
        reviewer, and the carousel renders that frame without a preview.
        """
        readings: list[ReviewFrameRead] = []
        for image in images:
            url: str | None = None
            if image.s3_path:
                try:
                    url = self.storage.presigned_url(image.s3_path)
                except Exception:  # noqa: BLE001 - one frame, not the whole response
                    logger.error("Could not sign a URL for image %s", image.id, exc_info=True)
            readings.append(
                ReviewFrameRead.model_validate(image).model_copy(
                    update={
                        "image_url": url,
                        "image_url_expires_in": (
                            settings.S3_PRESIGNED_URL_TTL_SECONDS if url else 0
                        ),
                    }
                )
            )
        return readings

    def status_breakdown(self, project_id: uuid.UUID) -> dict[str, int]:
        return self.images.status_breakdown(project_id)

    def defect_breakdown(self, project_id: uuid.UUID) -> dict[str, int]:
        return self.defects.class_breakdown(project_id)

    # ---------------------------------------------------------------- private
    def _rollback_objects(self, keys: Sequence[str]) -> None:
        """Best-effort cleanup of objects written before a failure.

        Logged rather than raised: the caller is already handling the original
        error, and a leftover object is cheaper than hiding the real cause.
        """
        for key in keys:
            try:
                self.storage.delete(key)
            except Exception:  # noqa: BLE001 - cleanup must not mask the cause
                logger.error(
                    "Could not roll back stored object '%s' after a failed upload batch",
                    key,
                    exc_info=True,
                )
