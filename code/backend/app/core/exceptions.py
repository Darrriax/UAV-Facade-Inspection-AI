"""Domain errors raised by the service layer.

Services raise these instead of ``fastapi.HTTPException`` so that business rules
stay framework-agnostic and reusable from Celery tasks. ``app.api.errors`` maps
them onto HTTP responses at the edge.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for expected business-rule violations."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    """The requested entity does not exist, or is not visible to the caller."""


class ConflictError(DomainError):
    """The request collides with existing state (e.g. duplicate email)."""


class ValidationError(DomainError):
    """The request is well-formed but semantically invalid."""


class AuthenticationError(DomainError):
    """Credentials are missing, malformed or expired -> 401."""


class PermissionDeniedError(DomainError):
    """The caller is authenticated but not allowed to perform the operation."""


class StorageError(DomainError):
    """The object store is unreachable or rejected the operation."""
