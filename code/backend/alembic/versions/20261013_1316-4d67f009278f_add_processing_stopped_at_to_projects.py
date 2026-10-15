"""add processing_stopped_at to projects

Adds the cooperative stop signal for a project's analysis.

The column is nullable with no default, so every existing project reads as "not
stopped" and the migration does not touch a single row. It is set by
``POST /projects/{id}/stop-processing`` and cleared by
``POST /projects/{id}/start-processing`` (resuming is starting again), and the
Celery worker polls it between frames - see ``app.tasks.image_processing``.

A timestamp rather than a boolean: the same column then answers "since when?",
which is the first question a paused batch raises.

Revision ID: 4d67f009278f
Revises: b7d41c6a2f58
Create Date: 2026-10-13 13:16:34.182340

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d67f009278f"
down_revision: str | None = "b7d41c6a2f58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("processing_stopped_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Dropping the column loses nothing but the stop flags: the frames, the
    # defects and the 3D models all live in their own tables.
    op.drop_column("projects", "processing_stopped_at")
