"""add detect_defects to projects

Lets a project skip defect detection and keep only its 3D reconstruction.

The column is added ``NOT NULL`` with a server default of ``true``, in one
statement: every project that exists today was analysed for defects, so they all
migrate to the behaviour they already had, without a backfill pass over the rows
and without a nullable window in which a constraint could be violated.

When it is false the worker never loads the detector - the frames are marked
``completed`` with no detections - and the photogrammetry step runs exactly as
before. The frames are still marked, rather than left ``pending``, on purpose:
the progress screen and the project badge read frame statuses, and a frame that
was never going to be looked at has to read as *done*, not as awaiting work.

Revision ID: 314008937372
Revises: 5a1d4dbf8b9b
Create Date: 2026-10-15 14:30:11.482913

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "314008937372"
down_revision: str | None = "5a1d4dbf8b9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "detect_defects",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    # Dropping the column loses only the setting: the frames, their statuses,
    # their masks and the 3D models all live in their own tables. A project that
    # was 3D-only would go back to being analysed as well, which is exactly the
    # behaviour the column was introduced to change.
    op.drop_column("projects", "detect_defects")
