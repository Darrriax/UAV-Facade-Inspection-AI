"""add object_index to defects

Groups each frame's detections into the objects an inspector reviews.

The model runs at a low confidence threshold and reports one crack several
times, as overlapping fragments. ``object_index`` records which object a
detection belongs to - rows sharing it are one piece of damage - and it is
assigned **once**, here and when a frame is analysed, instead of by every screen
that renders the masks. The workspace, the project cards and the counts read the
same column, so they cannot disagree about how many defects a project has.

The rows that already exist are grouped by this migration, using the same
:func:`app.services.mask_objects.group_object_indices` the worker calls - which is
why that module is pure Python: a migration runs in the API image, and that image
installs none of the AI stack. Grouping in SQL is not an option, the rule being
geometric, so the pass reads every frame's masks once. Its cost is proportional
to the number of *frames* in the database, and it is the only slow step here.

The column is added nullable, backfilled and then made ``NOT NULL``, and it
deliberately carries no server default: every write path sets it explicitly
(``ImageService.store_defects`` for detections, ``DefectService.create`` for a
hand-drawn mask), and a default of ``0`` would silently fold a forgotten row into
that frame's first object. Index ``0`` is a real object, not "unset".

Revision ID: 5a1d4dbf8b9b
Revises: 4d67f009278f
Create Date: 2026-10-15 13:35:12.104512

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.services.mask_objects import group_object_indices

# revision identifiers, used by Alembic.
revision: str = "5a1d4dbf8b9b"
down_revision: str | None = "4d67f009278f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Rows per UPDATE round trip during the backfill.
UPDATE_BATCH = 500

#: One row of the backfill: ``(defect id, class, mask)``.
Row = tuple[object, str, object]


def _write(connection: sa.Connection, updates: list[dict[str, object]]) -> None:
    """Send the accumulated index assignments and empty the batch."""
    if not updates:
        return
    connection.execute(
        sa.text("UPDATE defects SET object_index = :object_index WHERE id = :defect_id"),
        updates,
    )
    updates.clear()


def _backfill_object_indices() -> None:
    """Group every frame's existing detections, one frame at a time.

    Rows come back ordered by ``(image_id, created_at, id)`` - the order the
    detection list itself uses, which is the order the detector produced them -
    so the indices assigned here are the ones the worker would write for those
    frames today.
    """
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, image_id, defect_class, mask_polygon FROM defects "
            "ORDER BY image_id, created_at, id"
        )
    ).all()
    if not rows:
        return

    updates: list[dict[str, object]] = []
    frame: list[Row] = []
    current_image: object = None

    def flush_frame() -> None:
        """Group the frame just read, and send the batch if it is full."""
        if not frame:
            return
        indices = group_object_indices([(entry[1], entry[2]) for entry in frame])
        updates.extend(
            {"defect_id": entry[0], "object_index": index}
            for entry, index in zip(frame, indices)
        )
        if len(updates) >= UPDATE_BATCH:
            _write(connection, updates)

    for row in rows:
        if row.image_id != current_image:
            flush_frame()
            frame = []
            current_image = row.image_id
        frame.append((row.id, row.defect_class, row.mask_polygon))

    flush_frame()
    _write(connection, updates)


def upgrade() -> None:
    op.add_column("defects", sa.Column("object_index", sa.Integer(), nullable=True))
    _backfill_object_indices()
    op.alter_column("defects", "object_index", nullable=False)
    op.create_index("ix_defects_image_id_object_index", "defects", ["image_id", "object_index"])


def downgrade() -> None:
    # Dropping the column loses only the grouping: the masks, their classes and
    # the review decisions all stay, and a project simply falls back to listing
    # every detection separately.
    op.drop_index("ix_defects_image_id_object_index", table_name="defects")
    op.drop_column("defects", "object_index")
