"""``/users`` endpoints.

This is the *administration* surface, so account creation and listing require
the Admin role. Accounts are created here and nowhere else: an Inspector exists
only because an Admin made one, which is also what keeps the JWT layer honest,
because nobody can mint a role for themselves.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import AdminUser, CurrentUser, PaginationDep, UserServiceDep
from app.db.models import UserRole
from app.schemas.common import Page
from app.schemas.user import UserCreate, UserProfileUpdate, UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user (admin)",
    description="Admin-only. The only way to create an Inspector account.",
    responses={
        403: {"description": "Caller is not an Admin"},
        409: {"description": "Email already registered"},
    },
)
def create_user(
    payload: UserCreate,
    _: AdminUser,
    service: UserServiceDep,
) -> UserRead:
    return service.create(payload)


@router.get("", response_model=Page[UserRead], summary="List users (admin)")
def list_users(
    _: AdminUser,
    service: UserServiceDep,
    pagination: PaginationDep,
    role: Annotated[
        UserRole | None,
        Query(description="Filter by role; the Admin dashboard lists 'inspector'."),
    ] = None,
) -> Page[UserRead]:
    users, total = service.list(
        offset=pagination.offset,
        limit=pagination.limit,
        role=role,
    )
    # ORM rows are validated into UserRead by from_attributes.
    return Page[UserRead](
        items=users,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


# ------------------------------------------------------------------ self
# Declared *before* the ``/{user_id}`` routes below: FastAPI matches in
# registration order, so a later ``/me`` would be parsed as a UUID path
# parameter and answer 422 instead of returning the caller's own profile.
@router.get(
    "/me",
    response_model=UserRead,
    summary="Get my profile",
    description=(
        "The authenticated caller's own record. This is what the SPA calls after "
        "a reload to restore its session: the token carries only the user id, "
        "so the profile is one request away."
    ),
)
def read_current_user(current_user: CurrentUser) -> UserRead:
    return current_user


@router.patch(
    "/me",
    response_model=UserRead,
    summary="Update my profile",
    description=(
        "Self-service profile edit: names, country, age, phone, password and the "
        "profile-photo object key. Email, role and activation state are "
        "account-level attributes and are rejected here (422), not ignored."
    ),
)
def update_current_user(
    payload: UserProfileUpdate,
    current_user: CurrentUser,
    service: UserServiceDep,
) -> UserRead:
    return service.update(current_user, current_user.id, payload)


@router.get(
    "/{user_id}",
    response_model=UserRead,
    summary="Get a user profile",
    description="Admins may read any profile; other users only their own.",
    responses={403: {"description": "Not the caller's own account"}, 404: {"description": "User not found"}},
)
def get_user(user_id: uuid.UUID, current_user: CurrentUser, service: UserServiceDep) -> UserRead:
    return service.get(current_user, user_id)


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    summary="Update a user",
    description="Admins may edit any account; other users only their own. Changing 'role' requires Admin.",
    responses={
        403: {"description": "Not the caller's own account, or a role change without Admin"},
        404: {"description": "User not found"},
        409: {"description": "Email already registered"},
    },
)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    current_user: CurrentUser,
    service: UserServiceDep,
) -> UserRead:
    return service.update(current_user, user_id, payload)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a user",
    description="Admins may delete any account; other users may delete their own. Cascades to their projects.",
    responses={403: {"description": "Not the caller's own account"}, 404: {"description": "User not found"}},
)
def delete_user(user_id: uuid.UUID, current_user: CurrentUser, service: UserServiceDep) -> None:
    service.delete(current_user, user_id)
