"""User-specific data access."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    def get_by_email(self, email: str) -> User | None:
        """Look up a user by email.

        Callers must pass an already-normalised (lower-cased, trimmed) address -
        the column is not ``citext``, so uniqueness is enforced on the
        normalised form written by :class:`~app.services.user.UserService`.
        """
        return self.get_by(email=email)

    def email_exists(self, email: str, *, exclude_user_id: uuid.UUID | None = None) -> bool:
        """Whether ``email`` is taken, optionally ignoring one user's own row."""
        statement = select(User.id).where(User.email == email)
        if exclude_user_id is not None:
            statement = statement.where(User.id != exclude_user_id)
        return self.session.execute(statement.limit(1)).scalar_one_or_none() is not None
