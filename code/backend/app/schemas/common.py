"""Shared schema primitives."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class PresignedUrlRead(BaseModel):
    """A short-lived URL for one stored object.

    Everywhere else the API hands out object *keys*, never URLs, because a
    presigned URL expires. Screens that need the bytes ask for a fresh one
    through an endpoint like ``GET /projects/{id}/main-photo``, so image
    traffic goes straight from the browser to the S3 endpoint and never proxies
    through FastAPI. ``expires_in`` is echoed back so a long-lived screen knows
    when its URL goes stale instead of rendering a broken image.
    """

    url: str
    expires_in: int


class Page(BaseModel, Generic[T]):
    """Envelope for offset/limit paginated collections.

    Returning the total up-front lets the Vue client render pager state without
    a second round trip - important once a project holds thousands of frames.
    """

    # from_attributes so ORM rows can be handed to the endpoint and validated
    # here instead of being converted by hand in every router.
    model_config = ConfigDict(from_attributes=True)

    items: list[T]
    total: int
    limit: int
    offset: int
