"""FastAPI application factory.

The factory exists so ``uvicorn app.main:app`` (used by the Dockerfile) works
and so tests can build isolated app instances with overridden dependencies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.storage import apply_storage_cors
from app.db.database import engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Configure browser access to the object store, then serve.

    The CORS rules live on the buckets, not in this process, so they have to be
    pushed out once - and doing it here is what keeps `docker compose up` from
    leaving the 3D viewer broken. The call is idempotent and non-fatal.
    """
    apply_storage_cors()
    yield
    # Release pooled DB connections on shutdown.
    engine.dispose()


def create_app() -> FastAPI:
    application = FastAPI(
        title=settings.PROJECT_NAME,
        version="0.1.0",
        debug=settings.DEBUG,
        lifespan=lifespan,
    )

    if settings.BACKEND_CORS_ORIGINS:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.BACKEND_CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Domain errors (404/409/403) raised by the service layer.
    register_exception_handlers(application)

    application.include_router(api_router, prefix=settings.API_V1_PREFIX)

    return application


app = create_app()
