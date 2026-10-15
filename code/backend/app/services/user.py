"""User business logic."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.security import (
    access_token_ttl_seconds,
    create_access_token,
    dummy_password_hash,
    hash_password,
    verify_password,
)
from app.db.models import User, UserRole
from app.repositories.user import UserRepository
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.user import UserCreate, UserProfileUpdate, UserUpdate


def normalize_email(email: str) -> str:
    """Canonical form used for storage and uniqueness.

    The DB enforces a plain unique index, so ``Ivan@X.com`` and ``ivan@x.com``
    would otherwise be two accounts. Normalising on the way in and on the way
    out keeps the index effective (unlike a ``lower(email)`` functional index
    plus scattered ``func.lower`` comparisons).
    """
    return email.strip().lower()


class UserService:
    """User lifecycle operations.

    Owns the transaction boundary: repositories flush, this layer commits.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.users = UserRepository(session)

    # ------------------------------------------------------------------- auth
    def authenticate(self, payload: LoginRequest) -> TokenResponse:
        """Verify credentials and issue an access token."""
        user = self.users.get_by_email(normalize_email(str(payload.email)))

        if user is None:
            # Burn a comparable amount of time so response latency does not
            # reveal whether the email exists.
            verify_password(payload.password, dummy_password_hash())
            raise AuthenticationError("Incorrect email or password.")

        if not verify_password(payload.password, user.password_hash):
            # Same message as above: never say which half was wrong.
            raise AuthenticationError("Incorrect email or password.")

        if not user.is_active:
            # Checked *after* the password: an attacker who cannot prove they
            # hold the credentials must not be able to tell a deactivated
            # account from an unregistered one. A legitimate, correctly
            # authenticated user, on the other hand, deserves to know why
            # logging in fails.
            raise AuthenticationError(
                "This account has been deactivated. Contact an administrator."
            )

        return TokenResponse(
            access_token=create_access_token(user.id, role=user.role.value),
            expires_in=access_token_ttl_seconds(),
        )

    # ------------------------------------------------------------------ read
    def get(self, actor: User, user_id: uuid.UUID) -> User:
        """Fetch a profile. Admins may read anyone; everybody else only self."""
        user = self.users.get(user_id)
        if user is None:
            raise NotFoundError(f"User {user_id} was not found.")
        self.ensure_can_manage(actor, user_id)
        return user

    def get_by_email(self, email: str) -> User | None:
        """Return a user or ``None`` - the caller decides whether that is fatal."""
        return self.users.get_by_email(normalize_email(email))

    def list(
        self,
        *,
        offset: int,
        limit: int,
        role: UserRole | None = None,
    ) -> tuple[Sequence[User], int]:
        """List accounts, optionally narrowed to a single role.

        ``role`` is how the Admin dashboard asks for Inspectors only; filtering
        in SQL keeps the total it renders consistent with the page it fetched.
        Admin-only, enforced by the router dependency.
        """
        filters: dict[str, object] = {} if role is None else {"role": role}
        users = self.users.get_many(
            offset=offset,
            limit=limit,
            order_by=User.created_at.desc(),
            **filters,
        )
        return users, self.users.count(**filters)

    # ----------------------------------------------------------------- write
    def create(self, payload: UserCreate) -> User:
        email = normalize_email(str(payload.email))
        if self.users.email_exists(email):
            raise ConflictError(f"A user with email '{email}' already exists.")

        # Every remaining field (role plus the optional profile block) is taken
        # from the validated payload, so adding a column to UserCreate does not
        # require touching this method. `password` is the only field whose name
        # differs from its column.
        data = payload.model_dump(exclude={"password"})
        data["email"] = email
        # Never store the plaintext password.
        data["password_hash"] = hash_password(payload.password)

        user = self.users.create(data)
        self.session.commit()
        return user

    def update(
        self,
        actor: User,
        user_id: uuid.UUID,
        payload: UserUpdate | UserProfileUpdate,
    ) -> User:
        """Apply a partial update.

        ``UserUpdate`` (Admin surface) and ``UserProfileUpdate`` (self-service
        profile) share one code path on purpose: the privilege rules below are
        then impossible to apply to one contract but forget for the other.
        """
        user = self.get(actor, user_id)
        data = payload.model_dump(exclude_unset=True)

        # Role is the privilege boundary, so it is checked before anything is
        # written: otherwise a caller could change their own password *and*
        # escalate in one request.
        if "role" in data and actor.role != UserRole.ADMIN:
            raise PermissionDeniedError("Only administrators can change a user's role.")

        if "is_active" in data:
            if actor.role != UserRole.ADMIN:
                raise PermissionDeniedError(
                    "Only administrators can activate or deactivate an account."
                )
            if data["is_active"] is False and user_id == actor.id:
                # Otherwise the last Admin can lock themselves (and everyone
                # else) out of the administration surface in one request.
                raise ValidationError("You cannot deactivate your own account.")

        if data.get("profile_photo_s3_path"):
            # The key is later turned into a presigned URL, so an unchecked
            # value would let one account point its avatar at another account's
            # object. Pin it to this user's own prefix.
            expected_prefix = f"users/{user_id}/"
            if not str(data["profile_photo_s3_path"]).startswith(expected_prefix):
                raise ValidationError(
                    f"profile_photo_s3_path must be an object key under '{expected_prefix}'."
                )

        if not data:
            return user

        if "email" in data:
            email = normalize_email(str(data["email"]))
            if self.users.email_exists(email, exclude_user_id=user_id):
                raise ConflictError(f"A user with email '{email}' already exists.")
            data["email"] = email

        if "password" in data:
            # Translate the transport concept (password) into the stored one.
            data["password_hash"] = hash_password(data.pop("password"))

        user = self.users.update(user, data)
        self.session.commit()
        # updated_at is computed by the database, so re-read it before the row
        # is serialised into the response.
        self.session.refresh(user)
        return user

    def delete(self, actor: User, user_id: uuid.UUID) -> None:
        user = self.get(actor, user_id)
        self.users.delete(user)
        # ON DELETE CASCADE removes the user's projects and their images.
        self.session.commit()

    # ---------------------------------------------------------------- private
    def ensure_can_manage(self, actor: User, target_id: uuid.UUID) -> None:
        """Admins manage anyone; an Inspector manages only their own account."""
        if actor.role != UserRole.ADMIN and actor.id != target_id:
            raise PermissionDeniedError("You may only manage your own account.")
