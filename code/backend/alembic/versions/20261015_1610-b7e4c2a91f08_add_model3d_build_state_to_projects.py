"""add 3D build state to projects

Gives the reconstruction a state the API can report, instead of leaving "is a
model being built?" to be inferred from frame statuses.

A reconstruction is started by a Celery task that downloads the stored frames
and hands them to NodeODM, so it outlives the request that queued it, the
browser tab that asked for it, and sometimes the worker itself (a restart or a
crash mid-run). Without a persisted flag the 3D page cannot tell a build that is
genuinely in flight from one that never started - and the natural next action,
pressing "build" again, then either restarts an hour of work or does nothing at
all, depending on which of the two it is.

* ``model3d_requested_at`` - set when a build is queued, cleared when the task
  finishes. Not a boolean: the page reports how long the build has been running.
* ``model3d_error`` - why the last build produced no model (NodeODM refusing a
  dataset with too little overlap, for instance). That reason used to exist only
  in the worker log.

Both columns are nullable and default to NULL, so existing projects keep their
current behaviour: no build in flight, no recorded failure. Nothing is
backfilled, because "no model yet" is exactly what a NULL pair means.

Revision ID: b7e4c2a91f08
Revises: 314008937372
Create Date: 2026-10-15 16:10:42.119305

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e4c2a91f08"
down_revision: str | None = "314008937372"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("model3d_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("model3d_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Dropping these loses only the build state: the revisions themselves live in
    # ``models_3d``, and a finished reconstruction is unaffected. A build in
    # flight would finish and register its model as usual - it would only stop
    # reporting that it was running.
    op.drop_column("projects", "model3d_error")
    op.drop_column("projects", "model3d_requested_at")
