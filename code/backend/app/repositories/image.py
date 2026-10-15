"""Inspection-image data access."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from app.db.models import ImageStatus, InspectionImage
from app.repositories.base import BaseRepository


class InspectionImageRepository(BaseRepository[InspectionImage]):
    model = InspectionImage

    def get_many_by_project(
        self,
        project_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> Sequence[InspectionImage]:
        statement = (
            select(InspectionImage)
            .where(InspectionImage.project_id == project_id)
            .order_by(InspectionImage.created_at.asc(), InspectionImage.id)
            .offset(offset)
            .limit(limit)
        )
        return self.session.execute(statement).scalars().all()

    def count_by_project(self, project_id: uuid.UUID) -> int:
        return self.count(project_id=project_id)

    def get_many_stored_by_project(self, project_id: uuid.UUID) -> Sequence[InspectionImage]:
        """Every frame of a project that has bytes in object storage.

        Deliberately unpaginated, unlike :meth:`get_many_by_project`: the one
        caller is the 3D reconstruction, which needs *all* the frames to solve a
        camera network - a page of them would silently produce a partial model.
        Rows without an object key are filtered out in SQL rather than by the
        caller, since a frame with nothing to download is not a frame at all as
        far as photogrammetry is concerned.
        """
        statement = (
            select(InspectionImage)
            .where(
                InspectionImage.project_id == project_id,
                InspectionImage.s3_path.is_not(None),
            )
            .order_by(InspectionImage.created_at.asc(), InspectionImage.id)
        )
        return self.session.execute(statement).scalars().all()

    def get_in_project(
        self, image_id: uuid.UUID, project_id: uuid.UUID
    ) -> InspectionImage | None:
        """Fetch a frame only if it belongs to ``project_id``.

        The ownership predicate lives in SQL instead of a Python ``if``, so an
        endpoint that writes a mask onto a frame cannot forget to scope it - the
        same reasoning as the defect repository's ``get_in_project``.
        """
        statement = select(InspectionImage).where(
            InspectionImage.id == image_id,
            InspectionImage.project_id == project_id,
        )
        return self.session.execute(statement).scalar_one_or_none()

    def status_breakdown(self, project_id: uuid.UUID) -> dict[str, int]:
        """Counts per status - one query instead of loading every row."""
        return self.status_counts_for_projects([project_id]).get(project_id, {})

    def status_counts_for_projects(
        self,
        project_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, dict[str, int]]:
        """Counts per status for several projects, in one grouped query.

        The dashboard renders counters for every card it shows, so a per-project
        query would turn one screen into N+1 round trips. Projects with no
        frames are present with an empty mapping, which is what lets the caller
        tell "no frames" from "not asked about".
        """
        counts: dict[uuid.UUID, dict[str, int]] = {project_id: {} for project_id in project_ids}
        if not counts:
            return counts

        statement = (
            select(InspectionImage.project_id, InspectionImage.status, func.count())
            .where(InspectionImage.project_id.in_(list(counts)))
            .group_by(InspectionImage.project_id, InspectionImage.status)
        )
        for project_id, status, count in self.session.execute(statement).all():
            counts[project_id][status.value] = count
        return counts

    def release_processing(self, image_ids: Sequence[uuid.UUID]) -> int:
        """Put claimed frames back to ``pending``, and report how many moved.

        One statement rather than a loop, and guarded on the *current* status:
        a frame that completed (or failed) while the previous statement was in
        flight must not be resurrected as pending, so this can race with the
        worker it is correcting and still be correct.
        """
        ids = list(image_ids)
        if not ids:
            return 0

        statement = (
            update(InspectionImage)
            .where(
                InspectionImage.id.in_(ids),
                InspectionImage.status == ImageStatus.PROCESSING,
            )
            .values(status=ImageStatus.PENDING)
        )
        result = self.session.execute(statement)
        self.session.flush()
        return int(result.rowcount or 0)

    def requeue_stale_processing(
        self,
        project_id: uuid.UUID,
        *,
        older_than_seconds: int,
    ) -> int:
        """Put frames a dead run claimed but never finished back to ``pending``.

        A claim is a status flip, not a lease, so a worker that is SIGKILLed
        mid-run leaves its frames in ``processing`` for good - and the next run
        finds nothing to claim, which makes the project impossible to finish
        from the UI. This is the one place that assumption is corrected.

        ``updated_at`` is the claim timestamp (the row is not touched again
        until the frame completes), and the cutoff is what keeps this from
        stealing frames from a run that is simply still working: only a claim
        older than the caller's window moves. Guarded on the current status like
        :meth:`release_processing`, so a frame that completed in the meantime is
        never resurrected.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        statement = (
            update(InspectionImage)
            .where(
                InspectionImage.project_id == project_id,
                InspectionImage.status == ImageStatus.PROCESSING,
                InspectionImage.updated_at < cutoff,
            )
            .values(status=ImageStatus.PENDING)
        )
        result = self.session.execute(statement)
        self.session.flush()
        return int(result.rowcount or 0)

    def get_pending_for_update(
        self,
        project_id: uuid.UUID,
        *,
        limit: int | None = None,
    ) -> Sequence[InspectionImage]:
        """Lock a project's ``pending`` frames for one worker to process.

        ``FOR UPDATE SKIP LOCKED`` is the queue-claim primitive: a second worker
        running the same project steps over rows that are already locked instead
        of processing them twice. Deliberately not a bulk ``UPDATE`` - the worker
        needs the object keys anyway to download the frames, so loading the rows
        once and flipping their status in place is both cheaper and race-free.
        """
        statement = (
            select(InspectionImage)
            .where(
                InspectionImage.project_id == project_id,
                InspectionImage.status == ImageStatus.PENDING,
            )
            .order_by(InspectionImage.created_at.asc(), InspectionImage.id)
            .with_for_update(skip_locked=True)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return self.session.execute(statement).scalars().all()
