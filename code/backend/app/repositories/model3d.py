"""Model3D data access."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.db.models import Model3D
from app.repositories.base import BaseRepository


class Model3DRepository(BaseRepository[Model3D]):
    model = Model3D

    def get_many_by_project(self, project_id: uuid.UUID) -> Sequence[Model3D]:
        statement = (
            select(Model3D)
            .where(Model3D.project_id == project_id)
            .order_by(Model3D.created_at.desc(), Model3D.id)
        )
        return self.session.execute(statement).scalars().all()

    def latest_for_project(self, project_id: uuid.UUID) -> Model3D | None:
        statement = (
            select(Model3D)
            .where(Model3D.project_id == project_id)
            .order_by(Model3D.created_at.desc(), Model3D.id)
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()


__all__ = ["Model3DRepository"]
