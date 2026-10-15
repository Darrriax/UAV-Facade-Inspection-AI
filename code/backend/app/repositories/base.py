"""Generic repository providing the CRUD primitives shared by every entity.

Transaction boundaries belong to the **service**, not here: repositories only
``flush()`` (which assigns generated keys and surfaces constraint violations
immediately, inside the service's ``try``), while the service decides when to
``commit()``. That keeps a multi-step operation atomic - relevant later, when
creating an image row and enqueuing its Celery task must succeed or fail
together.

Repositories speak in plain mappings rather than Pydantic models, so the
data-access layer stays free of transport concerns.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.db.database import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """CRUD helpers generic over a SQLAlchemy model."""

    #: Set by every concrete repository, e.g. ``model = User``.
    model: type[ModelType]

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ read
    def get(self, entity_id: uuid.UUID) -> ModelType | None:
        """Fetch by primary key (identity-map aware, so no query if loaded)."""
        return self.session.get(self.model, entity_id)

    def get_by(self, **filters: Any) -> ModelType | None:
        """Fetch the first row matching simple equality filters."""
        statement = select(self.model).filter_by(**filters).limit(1)
        return self.session.execute(statement).scalar_one_or_none()

    def get_many(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        order_by: ColumnElement[Any] | None = None,
        **filters: Any,
    ) -> Sequence[ModelType]:
        statement = select(self.model).filter_by(**filters)
        if order_by is not None:
            statement = statement.order_by(order_by)
        statement = statement.offset(offset).limit(limit)
        return self.session.execute(statement).scalars().all()

    def count(self, **filters: Any) -> int:
        statement = select(func.count()).select_from(self.model).filter_by(**filters)
        return self.session.execute(statement).scalar_one()

    def exists(self, **filters: Any) -> bool:
        statement = select(self.model).filter_by(**filters).limit(1)
        return self.session.execute(statement).scalar_one_or_none() is not None

    # ----------------------------------------------------------------- write
    def create(self, data: Mapping[str, Any]) -> ModelType:
        entity = self.model(**data)
        self.session.add(entity)
        self.session.flush()
        return entity

    def update(self, entity: ModelType, data: Mapping[str, Any]) -> ModelType:
        for field, value in data.items():
            setattr(entity, field, value)
        self.session.add(entity)
        self.session.flush()
        return entity

    def delete(self, entity: ModelType) -> None:
        self.session.delete(entity)
        self.session.flush()
