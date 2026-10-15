"""``/login``.

The router carries no prefix, so the path is exactly ``/api/v1/login``. Add
``prefix="/auth"`` if you would rather have ``/api/v1/auth/*``.

There is deliberately no ``/register``: accounts are created by an Admin
through ``POST /api/v1/users`` (or by the CLI for the very first one), so an
Inspector only ever exists because an Admin made it.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import UserServiceDep
from app.schemas.auth import LoginRequest, TokenResponse

router = APIRouter(tags=["auth"])


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in and obtain a JWT",
    responses={401: {"description": "Incorrect email or password"}},
)
def login(payload: LoginRequest, service: UserServiceDep) -> TokenResponse:
    return service.authenticate(payload)
