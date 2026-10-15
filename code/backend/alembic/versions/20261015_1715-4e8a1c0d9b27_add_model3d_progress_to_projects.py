"""add 3D reconstruction progress to projects

Gives the running reconstruction a *measured* completion percentage, so the 3D
page can show a progress bar instead of a spinner that only says "something is
happening" for up to an hour.

Only the worker knows the number: it is the process holding the NodeODM task,
and it already polls that task for its status. It writes the percentage onto the
project as it goes, throttled to one write per whole percent, and the API serves
it with the rest of the project - the same shape as the two build-state columns
this revises.

* ``model3d_progress`` - 0-100 while a reconstruction is in flight, NULL when
  none is. NULL is also the state of a build that has been queued but not yet
  reported its first percentage, which the page renders as an indeterminate bar
  rather than as 0%: a bar stuck at zero and a bar that is genuinely waiting look
  identical to a reader, and the queue wait is the part nobody can measure.

Nullable with no default and nothing backfilled: no existing project has a
reconstruction in flight, and a NULL percentage is exactly what "nothing is
running" means.

Revision ID: 4e8a1c0d9b27
Revises: b7e4c2a91f08
Create Date: 2026-10-15 17:15:08.412930

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4e8a1c0d9b27"
down_revision: str | None = "b7e4c2a91f08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("model3d_progress", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    # Dropping this loses only the progress of a build in flight: the
    # reconstruction itself is unaffected and still registers its model, the 3D
    # page just goes back to reporting it without a percentage.
    op.drop_column("projects", "model3d_progress")
