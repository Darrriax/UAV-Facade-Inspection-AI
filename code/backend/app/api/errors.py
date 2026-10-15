"""Translate domain exceptions into HTTP responses.

Keeps the service layer free of FastAPI imports. The body matches FastAPI's own
``{"detail": ...}`` shape, so the Vue client can handle framework-raised errors
(422, 500) and domain errors with the same interceptor.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    StorageError,
    ValidationError,
)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthenticationError)
    async def _handle_unauthenticated(_: Request, exc: AuthenticationError) -> JSONResponse:
        # WWW-Authenticate is required by RFC 7235 for a 401 and is what makes
        # browser/HTTP clients (and Swagger) prompt for a fresh token.
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": exc.message},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(ValidationError)
    async def _handle_validation(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": exc.message},
        )

    @app.exception_handler(StorageError)
    async def _handle_storage(_: Request, exc: StorageError) -> JSONResponse:
        # The object store is an upstream dependency: 503 (retryable) rather
        # than 500, which would suggest a bug in our own code.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": exc.message},
        )

    @app.exception_handler(NotFoundError)
    async def _handle_not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": exc.message},
        )

    @app.exception_handler(ConflictError)
    async def _handle_conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": exc.message},
        )

    @app.exception_handler(PermissionDeniedError)
    async def _handle_permission_denied(
        _: Request, exc: PermissionDeniedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": exc.message},
        )
