"""Project-specific data access.

Every read is **owner-scoped**. Filtering in SQL rather than in Python means a
missing ``user_id`` predicate cannot silently turn into a cross-tenant data
leak when a router forgets to check ownership.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.db.models import Project
from app.repositories.base import BaseRepository


class ProjectRepository(BaseRepository[Project]):
    model = Project

    def get_many_by_owner(
        self,
        owner_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> Sequence[Project]:
        statement = (
            select(Project)
            .where(Project.user_id == owner_id)
            # id as tie-breaker: without a total order, offset pagination can
            # repeat or skip rows when several projects share a timestamp.
            .order_by(Project.created_at.desc(), Project.id)
            .offset(offset)
            .limit(limit)
        )
        return self.session.execute(statement).scalars().all()

    def count_by_owner(self, owner_id: uuid.UUID) -> int:
        return self.count(user_id=owner_id)

    def get_many_any_owner(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> Sequence[Project]:
        """Every project on the installation, newest first.

        Named so that an unscoped read is impossible to mistake for the
        owner-scoped ones above: exactly one caller (the Admin overview) is
        allowed to use it, and only after the Admin role has been verified.
        """
        statement = (
            select(Project)
            .order_by(Project.created_at.desc(), Project.id)
            .offset(offset)
            .limit(limit)
        )
        return self.session.execute(statement).scalars().all()

    def get_owned(self, project_id: uuid.UUID, owner_id: uuid.UUID) -> Project | None:
        """Fetch a project only if it belongs to ``owner_id``."""
        return self.get_by(id=project_id, user_id=owner_id)
