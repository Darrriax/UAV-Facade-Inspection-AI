"""Shared FastAPI dependencies: database session, services, auth and RBAC."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.security import decode_access_token
from app.db.database import get_db
from app.db.models import User, UserRole
from app.services.defect import DefectService
from app.services.image import ImageService
from app.services.model3d import Model3DService
from app.services.project import ProjectService
from app.services.user import UserService

# --------------------------------------------------------------------- session
DbSession = Annotated[Session, Depends(get_db)]


# -------------------------------------------------------------------- services
# Each service shares the request-scoped session, so one request is one unit of
# work.
def get_user_service(session: DbSession) -> UserService:
    return UserService(session)


def get_project_service(session: DbSession) -> ProjectService:
    return ProjectService(session)


def get_image_service(session: DbSession) -> ImageService:
    return ImageService(session)


def get_defect_service(session: DbSession) -> DefectService:
    return DefectService(session)


def get_model3d_service(session: DbSession) -> Model3DService:
    return Model3DService(session)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
ImageServiceDep = Annotated[ImageService, Depends(get_image_service)]
DefectServiceDep = Annotated[DefectService, Depends(get_defect_service)]
Model3DServiceDep = Annotated[Model3DService, Depends(get_model3d_service)]


# ------------------------------------------------------------------ pagination
class Pagination:
    """Offset/limit query parameters, reusable across collection endpoints."""

    def __init__(
        self,
        offset: Annotated[int, Query(ge=0, description="Rows to skip")] = 0,
        limit: Annotated[int, Query(ge=1, le=100, description="Rows per page")] = 20,
    ) -> None:
        self.offset = offset
        self.limit = limit


PaginationDep = Annotated[Pagination, Depends(Pagination)]


# ------------------------------------------------------------------------- auth
#: ``auto_error=False`` so a missing header produces our own 401 body (with the
#: WWW-Authenticate header) instead of Starlette's bare ``{"detail": "Not
#: authenticated"}``. It also supplies the padlock button in Swagger UI.
bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")


def get_current_user(
    session: DbSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> User:
    """Resolve the acting user from a Bearer token.

    The role is read from the **database row**, not from the token claim: a
    token issued while somebody was an Admin must stop granting Admin rights the
    moment they are demoted, without waiting for it to expire. The claim is kept
    in the token only as a debugging aid.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Not authenticated.")

    payload = decode_access_token(credentials.credentials)

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Invalid authentication token.") from exc

    user = session.get(User, user_id)
    if user is None:
        # Signature was valid but the account no longer exists (deleted, or the
        # database was reset) - indistinguishable from a bad token, from the
        # client's point of view.
        raise AuthenticationError("Invalid authentication token.")

    if not user.is_active:
        # Deactivation must revoke access immediately, not when the current
        # token happens to expire: an already-issued JWT stays cryptographically
        # valid for up to JWT_ACCESS_TOKEN_EXPIRE_MINUTES. Checked here (rather
        # than as a separate guard on each router) so no endpoint can forget it.
        raise AuthenticationError("This account has been deactivated.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole):
    """Build a dependency that admits only the given roles."""

    def dependency(current_user: CurrentUser) -> User:
        if current_user.role not in roles:
            allowed = ", ".join(role.value for role in roles)
            raise PermissionDeniedError(
                f"This operation requires one of the following roles: {allowed}."
            )
        return current_user

    return dependency


#: Management endpoints (user administration) are Admin-only.
AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
#: Domain endpoints reserved for Inspectors.
InspectorUser = Annotated[User, Depends(require_roles(UserRole.INSPECTOR))]
