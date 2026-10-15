"""Engine, session factory and declarative base.

Sessions are **synchronous** on purpose: the web layer delegates blocking work
to FastAPI's threadpool and the Celery worker (YOLOv9 inference) is
CPU-bound/synchronous anyway. This keeps a single Session type shared by both
processes instead of maintaining parallel async repositories.

If the read-heavy 3D endpoints later need true async I/O, add a second
``AsyncEngine`` + ``async_sessionmaker`` here; the ORM models stay untouched.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

# Deterministic constraint names are required for useful Alembic autogenerate
# diffs (and for ALTER operations on unnamed constraints).
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


engine = create_engine(
    settings.database_url,
    echo=settings.DB_ECHO,
    pool_pre_ping=True,  # transparently recycle connections dropped by PG
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
)

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    autocommit=False,
    # Objects stay usable after commit -> handy for building responses without
    # re-querying; repositories are responsible for loading what they need.
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session.

    The ``try/finally`` guarantees the connection returns to the pool even when
    the request raises; ``rollback`` on error keeps the session reusable after
    a failed unit of work.
    """
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
