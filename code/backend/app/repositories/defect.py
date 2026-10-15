"""Defect data access."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.db.models import Defect, DefectClass, InspectionImage
from app.repositories.base import BaseRepository


class DefectRepository(BaseRepository[Defect]):
    model = Defect

    def bulk_create(self, rows: Sequence[Mapping[str, Any]]) -> list[Defect]:
        """Insert many detections for one frame in a single flush.

        A single frame can yield dozens of instances, and the worker handles
        hundreds of frames: inserting one row per round trip would dominate the
        runtime. ``flush`` (not commit) leaves the transaction boundary with the
        service, as everywhere else.
        """
        defects = [self.model(**row) for row in rows]
        self.session.add_all(defects)
        self.session.flush()
        return defects

    def count_by_image(self, image_id: uuid.UUID) -> int:
        return self.count(image_id=image_id)

    def count_by_project(self, project_id: uuid.UUID) -> int:
        """Total defects across a project's frames (joins through images)."""
        return self.counts_by_project([project_id])[project_id]

    def counts_by_project(
        self,
        project_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, int]:
        """Defect *row* totals for several projects, in one grouped query.

        The batched twin of :meth:`count_by_project`, for the dashboard grid:
        one query for a whole page of projects instead of one per card. Projects
        without defects are returned as ``0`` rather than omitted.

        These are rows, which is what paging the detection list needs. For the
        count a person reads - one crack, however many fragments the model
        reported it as - use :meth:`count_objects_by_project`.
        """
        counts: dict[uuid.UUID, int] = {project_id: 0 for project_id in project_ids}
        if not counts:
            return counts

        statement = (
            select(InspectionImage.project_id, func.count())
            .select_from(Defect)
            .join(InspectionImage, Defect.image_id == InspectionImage.id)
            .where(InspectionImage.project_id.in_(list(counts)))
            .group_by(InspectionImage.project_id)
        )
        for project_id, count in self.session.execute(statement).all():
            counts[project_id] = count
        return counts

    def count_objects_by_project(
        self,
        project_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, int]:
        """Object totals for several projects, in one grouped query.

        An object is one piece of damage on one frame, not one row: the model
        reports a crack as several overlapping fragments, and ``object_index``
        records which of them are the same thing (see
        ``app.services.mask_objects``). This is the number of defects the project
        cards show, and the number the workspace lists object by object -
        counting rows instead would report the model's uncertainty as a count of
        damage on the building.

        The ``DISTINCT`` pair is what keeps it to one query per page of cards:
        the database does the grouping and only the per-project totals travel
        back. Projects without detections are returned as ``0``.
        """
        counts: dict[uuid.UUID, int] = {project_id: 0 for project_id in project_ids}
        if not counts:
            return counts

        objects = (
            select(Defect.image_id, Defect.object_index)
            .join(InspectionImage, Defect.image_id == InspectionImage.id)
            .where(InspectionImage.project_id.in_(list(counts)))
            .distinct()
            .subquery()
        )
        statement = (
            select(InspectionImage.project_id, func.count())
            .select_from(objects)
            .join(InspectionImage, InspectionImage.id == objects.c.image_id)
            .group_by(InspectionImage.project_id)
        )
        for project_id, count in self.session.execute(statement).all():
            counts[project_id] = count
        return counts

    def count_verified_objects_by_project(
        self,
        project_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, int]:
        """Confirmed *object* totals for several projects, in one grouped query.

        The reviewed half of :meth:`count_objects_by_project`, and it has to
        group the same way: an object is confirmed only once **every** detection
        it is made of is (the rule the 2D workspace applies to a reviewed object),
        so a crack whose second fragment is still unreviewed stays unconfirmed
        rather than counting as two thirds of a defect.

        ``bool_and`` in a ``HAVING`` is that rule in SQL: group the rows into
        objects, keep the groups that are entirely verified, count them.
        Projects with nothing confirmed are returned as ``0``.
        """
        counts: dict[uuid.UUID, int] = {project_id: 0 for project_id in project_ids}
        if not counts:
            return counts

        confirmed = (
            select(Defect.image_id, Defect.object_index)
            .join(InspectionImage, Defect.image_id == InspectionImage.id)
            .where(InspectionImage.project_id.in_(list(counts)))
            .group_by(Defect.image_id, Defect.object_index)
            .having(func.bool_and(Defect.is_verified))
            .subquery()
        )
        statement = (
            select(InspectionImage.project_id, func.count())
            .select_from(confirmed)
            .join(InspectionImage, InspectionImage.id == confirmed.c.image_id)
            .group_by(InspectionImage.project_id)
        )
        for project_id, count in self.session.execute(statement).all():
            counts[project_id] = count
        return counts

    def max_object_index(self, image_id: uuid.UUID) -> int:
        """The highest object index on a frame, or ``-1`` when it has none.

        A hand-drawn mask uses this to open an object of its own: the human drew
        one shape, so it is one object, and it must not silently join a detection
        the model produced.
        """
        statement = select(func.max(Defect.object_index)).where(Defect.image_id == image_id)
        highest = self.session.execute(statement).scalar()
        return -1 if highest is None else int(highest)

    def class_breakdown(self, project_id: uuid.UUID) -> dict[str, int]:
        statement = (
            select(Defect.defect_class, func.count())
            .join(InspectionImage, Defect.image_id == InspectionImage.id)
            .where(InspectionImage.project_id == project_id)
            .group_by(Defect.defect_class)
        )
        return {
            defect_class.value: count
            for defect_class, count in self.session.execute(statement).all()
        }

    def get_many_by_project(
        self,
        project_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> Sequence[Defect]:
        """A project's defects, oldest first, each with its frame preloaded.

        ``selectinload`` fetches every frame of the page in one extra query,
        rather than one query per defect: the review list needs each defect's
        object key to sign an image URL, and a facade panel can carry a dozen
        detections.
        """
        statement = (
            select(Defect)
            .join(InspectionImage, Defect.image_id == InspectionImage.id)
            .where(InspectionImage.project_id == project_id)
            .options(selectinload(Defect.image))
            # id as tie-breaker: defects written in one worker batch share a
            # timestamp, and without a total order offset paging can repeat or
            # skip rows.
            .order_by(Defect.created_at.asc(), Defect.id)
            .offset(offset)
            .limit(limit)
        )
        return self.session.execute(statement).scalars().all()

    def get_in_project(self, defect_id: uuid.UUID, project_id: uuid.UUID) -> Defect | None:
        """Fetch a defect only if it belongs to ``project_id``.

        The ownership predicate lives in SQL instead of a Python ``if``, so a
        caller cannot forget it and reach another project's defect - the same
        reasoning as the owner-scoped project queries.
        """
        statement = (
            select(Defect)
            .join(InspectionImage, Defect.image_id == InspectionImage.id)
            .where(Defect.id == defect_id, InspectionImage.project_id == project_id)
            .options(selectinload(Defect.image))
        )
        return self.session.execute(statement).scalar_one_or_none()

    def get_many_by_image(self, image_id: uuid.UUID) -> Sequence[Defect]:
        statement = (
            select(Defect)
            .where(Defect.image_id == image_id)
            .order_by(Defect.confidence_score.desc(), Defect.id)
        )
        return self.session.execute(statement).scalars().all()


__all__ = ["DefectRepository", "DefectClass"]
