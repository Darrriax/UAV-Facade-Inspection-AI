"""Defect business logic: the review list, the review edit and hand-drawn masks.

The workspace reads defects, edits one per reviewer decision, and - in the
annotation tools - lets an inspector draw a mask the model missed, rub part of
one out, or drop one it should not have found. Those are the only writes: a
detection is never re-classified in place, because the model's output and the
human's review are different facts and the review record keeps both.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.storage import StorageBackend, get_image_storage
from app.db.models import Defect
from app.repositories.defect import DefectRepository
from app.repositories.image import InspectionImageRepository
from app.schemas.defect import DefectCreate, DefectRead, DefectUpdate

logger = logging.getLogger(__name__)

#: Confidence recorded for a mask a human drew. There is no model probability
#: behind it, and a neutral 1.0 keeps the review list's column meaningful
#: without inventing a number the detector never produced.
HUMAN_MASK_CONFIDENCE = 1.0


class DefectService:
    def __init__(self, session: Session, storage: StorageBackend | None = None) -> None:
        self.session = session
        self.defects = DefectRepository(session)
        # Used to check that a mask is drawn on a frame of the addressed project.
        self.images = InspectionImageRepository(session)
        # Injectable so tests can substitute a fake and never touch S3.
        self.storage = storage if storage is not None else get_image_storage()

    # ------------------------------------------------------------------ read
    def list_by_project(
        self,
        project_id: uuid.UUID,
        *,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Defect], int]:
        return (
            self.defects.get_many_by_project(project_id, offset=offset, limit=limit),
            self.defects.count_by_project(project_id),
        )

    def present_many(self, defects: Sequence[Defect]) -> list[DefectRead]:
        """Attach each defect's frame metadata and a presigned image URL.

        URLs are signed **once per distinct frame**, not once per defect: a
        single cracked panel can carry a dozen detections and signing costs an
        HMAC each time. Doing it here also means the browser never sees a bucket
        credential - it only ever gets a URL that expires.
        """
        if not defects:
            return []

        urls = self._sign_frames(defects)
        readings: list[DefectRead] = []
        for defect in defects:
            image = defect.image
            readings.append(
                DefectRead.model_validate(defect).model_copy(
                    update={
                        "image_file_path": image.file_path if image is not None else "",
                        "image_url": urls.get(defect.image_id),
                        "image_url_expires_in": settings.S3_PRESIGNED_URL_TTL_SECONDS,
                    }
                )
            )
        return readings

    def present(self, defect: Defect) -> DefectRead:
        """The single-defect shape, used by the verification response."""
        return self.present_many([defect])[0]

    def _sign_frames(self, defects: Sequence[Defect]) -> dict[uuid.UUID, str]:
        """One signed URL per distinct frame that has an object key.

        A signing failure is logged and the frame is left without a URL rather
        than failing the whole list: one unreadable object must not blank out
        the review screen, and the card renders without a preview.
        """
        urls: dict[uuid.UUID, str] = {}
        for defect in defects:
            image = defect.image
            if image is None or not image.s3_path or image.id in urls:
                continue
            try:
                urls[image.id] = self.storage.presigned_url(image.s3_path)
            except Exception:  # noqa: BLE001 - one frame, not the whole response
                logger.error("Could not sign a URL for image %s", image.id, exc_info=True)
        return urls

    # ----------------------------------------------------------------- write
    def update(
        self,
        project_id: uuid.UUID,
        defect_id: uuid.UUID,
        payload: DefectUpdate,
    ) -> Defect:
        """Record a reviewer's decision about one detection, and its geometry.

        Two fields, because they are the two things a human corrects: whether the
        detection is right (``is_verified``) and *where* it is right
        (``mask_polygon``, rewritten by the eraser).

        Scoped by project as well as by defect id: the id alone would let a
        caller edit a detection on a campaign they cannot even see. Ownership is
        the caller's job (the route checks it before getting here) - this only
        guarantees the defect is inside the project being addressed.
        """
        defect = self.defects.get_in_project(defect_id, project_id)
        if defect is None:
            raise NotFoundError(f"Defect {defect_id} was not found in this project.")

        changes: dict[str, object] = {}
        if payload.is_verified is not None:
            changes["is_verified"] = payload.is_verified
        if payload.mask_polygon is not None:
            changes["mask_polygon"] = [
                [{"x": point.x, "y": point.y} for point in ring]
                for ring in payload.mask_polygon
            ]

        if changes:
            defect = self.defects.update(defect, changes)
            self.session.commit()
        return defect

    def create(
        self,
        project_id: uuid.UUID,
        image_id: uuid.UUID,
        payload: DefectCreate,
    ) -> Defect:
        """Store a mask drawn by a human on one frame of a project.

        The frame is looked up scoped to the project, so a mask can only ever
        land on the campaign it was drawn in - an image id from another project
        is a 404, not a cross-tenant write.
        """
        image = self.images.get_in_project(image_id, project_id)
        if image is None:
            raise NotFoundError(f"Image {image_id} was not found in this project.")

        defect = self.defects.create(
            {
                "image_id": image.id,
                "defect_class": payload.defect_class,
                "confidence_score": HUMAN_MASK_CONFIDENCE,
                "mask_polygon": [
                    [{"x": point.x, "y": point.y} for point in ring]
                    for ring in payload.mask_polygon
                ],
                # A hand-drawn mask is the review, not something awaiting one.
                "is_verified": True,
                # Its own object, rather than a join to whatever the model
                # happened to report nearby: the human drew one shape, and the
                # grouping the model's fragments were given is not a decision to
                # revisit when a second shape is drawn next to it.
                "object_index": self.defects.max_object_index(image.id) + 1,
            }
        )
        self.session.commit()
        return defect

    def delete(self, project_id: uuid.UUID, defect_id: uuid.UUID) -> None:
        """Remove one detection from a project.

        A hard delete on purpose: the mask is annotation data, and a flag
        "edited" row would keep showing up in the counts the project is judged
        by. A 3D localization of this defect goes with it through the foreign
        key's ON DELETE CASCADE.
        """
        defect = self.defects.get_in_project(defect_id, project_id)
        if defect is None:
            raise NotFoundError(f"Defect {defect_id} was not found in this project.")
        self.defects.delete(defect)
        self.session.commit()
