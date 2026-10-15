"""SQLAlchemy 2.0 ORM models.

Entity graph (all FKs are ON DELETE CASCADE, PostgreSQL native enums for the
closed value sets)::

    User 1─┬─* Project 1─┬─* InspectionImage 1──* Defect 1──* DefectMap3D
           │             └─* Model3D        1──────────────*

Identifiers are UUIDv4 (client-visible, non-enumerable, safe to expose in URLs
and S3 object keys). ``gen_random_uuid()`` is built into PostgreSQL >= 13, so
raw-SQL/psql inserts work without the ORM.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

__all__ = [
    "UserRole",
    "ImageStatus",
    "DefectClass",
    "User",
    "Project",
    "InspectionImage",
    "Defect",
    "Model3D",
    "DefectMap3D",
]


# ---------------------------------------------------------------- value objects
class UserRole(str, enum.Enum):
    """RBAC roles."""

    ADMIN = "admin"
    INSPECTOR = "inspector"


class ImageStatus(str, enum.Enum):
    """Lifecycle of a single UAV frame as it moves through Celery."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DefectClass(str, enum.Enum):
    """Defect classes produced by the YOLOv9-c CUBIT-InSeg model."""

    CRACK = "crack"
    SPALLING = "spalling"


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Native PostgreSQL enum persisted by *value* (lowercase labels).

    ``values_callable`` is required because SQLAlchemy otherwise stores the
    member *names* (``ADMIN``, ``PENDING``), which would not match the API
    contract. ``validate_strings`` turns typos into errors instead of raw
    psycopg failures.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


# --------------------------------------------------------------------- mixins
class UUIDPrimaryKeyMixin:
    """UUIDv4 surrogate key."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    """Audit columns maintained by the database clock."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------------- models
class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Inspector or administrator. Owns projects.

    Account identity (``email``, ``role``, ``is_active``) is managed from the
    Admin surface, while the profile block below is what an account owner edits
    on their own profile page. Every profile field is nullable: an Admin only
    has to supply an email and a password when creating an Inspector account,
    and the inspector fills the rest in later.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        _pg_enum(UserRole, "user_role"),
        default=UserRole.INSPECTOR,
        server_default=UserRole.INSPECTOR.value,
        nullable=False,
        index=True,
    )
    # Whether the account may authenticate at all. This is a soft delete: a
    # deactivated Inspector keeps their projects, images and defect history, so
    # the Admin dashboard can list them (and their storage footprint) without
    # losing the audit trail.
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        nullable=False,
    )

    # --- profile -----------------------------------------------------------
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # ISO 3166-1 alpha-2 code ("UA"), stored upper-case so it can be rendered
    # as a flag/label by the client. Text rather than a lookup table: the set is
    # fixed and shipping it as data would only add a join.
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phone numbers are identifiers, not quantities: keep the leading zeros and
    # the '+', which a numeric type would silently eat. The column is wide
    # enough for E.164 with separators.
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Object key inside the images bucket (``users/<user>/avatar/<uuid>.<ext>``),
    # written server-side by the photo upload flow - never an arbitrary client
    # string, since the API turns it into a presigned URL.
    profile_photo_s3_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    projects: Mapped[list[Project]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # A profile edit is a place where a typo (age=200) or a broken import
        # should be rejected by the database too, not only by Pydantic.
        CheckConstraint(
            "age IS NULL OR (age >= 0 AND age <= 120)",
            name="age_range",
        ),
    )


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One inspection campaign for a single building."""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Human-readable address / object description. Swap for PostGIS geography
    # or dedicated latitude/longitude columns if spatial queries are needed.
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Free-form notes shown on the project card; TEXT rather than VARCHAR(255)
    # because inspectors paste long descriptions.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Object key of the cover photo inside the images bucket. Written by the
    # server (upload flow), never taken from client input as an opaque path.
    main_photo_s3_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Whether this project expects a .glb reconstruction to be attached.
    needs_3d_model: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    # Whether frames are run through the defect detector. Turned off for a
    # project that exists only to produce a 3D reconstruction: its frames are
    # still marked analysed, they simply carry no masks - so "nothing was found
    # on this photo" and "this photo was never looked at" stay distinguishable.
    # Defaults to true, which is what every project did before the column
    # existed, so existing rows keep their behaviour.
    detect_defects: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        nullable=False,
    )
    # Set while the user has asked this project's analysis to stop, cleared when
    # it is started again. A timestamp rather than a boolean because "since when
    # has this been stopped?" is the question a paused batch raises, and it costs
    # the same column.
    #
    # The pipeline is stopped *cooperatively*: nothing sends a signal to the
    # worker, the worker reads this column (see app.tasks.image_processing) and
    # stops between frames. A column, not a Redis key, so the state survives a
    # worker restart and is visible to the API without a second data store.
    processing_stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Set while a 3D reconstruction is queued or running, cleared when the task
    # that owns it finishes - with a model or with an error. A timestamp rather
    # than a boolean because a reconstruction runs for tens of minutes from a
    # *different* process, so the 3D page has to be able to show that something
    # is genuinely in flight (and how long it has been) after a reload, instead
    # of offering to start a second one.
    model3d_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Why the last reconstruction produced no model, or NULL when the last one
    # succeeded (or none was ever run). NodeODM cannot always solve a dataset -
    # too little overlap, a planar subject - and that outcome is otherwise only
    # visible in the worker log, which is not where a user of the 3D page is.
    model3d_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How far the running reconstruction has got, 0-100, or NULL while none is
    # running (and until the worker reports its first percentage). Written by
    # the worker, which is the only process that can ask NodeODM how far it is,
    # so that the 3D page can show a bar that moves instead of a spinner: a
    # reconstruction lasts tens of minutes and a client-side animation would
    # claim progress nobody measured.
    model3d_progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    owner: Mapped[User] = relationship(back_populates="projects")
    images: Mapped[list[InspectionImage]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    models_3d: Mapped[list[Model3D]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # Drives the "my projects, newest first" listing. PostgreSQL scans a
        # btree backwards, so an explicit DESC index is unnecessary here.
        Index("ix_projects_user_id_created_at", "user_id", "created_at"),
    )


class InspectionImage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single UAV frame uploaded to bulk storage and queued for analysis."""

    __tablename__ = "inspection_images"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Local/worker-side path or original client filename, kept for traceability.
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Object key inside the S3-compatible bucket (Garage).
    s3_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[ImageStatus] = mapped_column(
        _pg_enum(ImageStatus, "image_status"),
        default=ImageStatus.PENDING,
        server_default=ImageStatus.PENDING.value,
        nullable=False,
        index=True,
    )

    project: Mapped[Project] = relationship(back_populates="images")
    defects: Mapped[list[Defect]] = relationship(
        back_populates="image",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # The worker polls/claims frames by project + status.
        Index("ix_inspection_images_project_id_status", "project_id", "status"),
    )


class Defect(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single detected instance (crack/spalling) as a pixel mask polygon."""

    __tablename__ = "defects"

    image_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inspection_images.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    defect_class: Mapped[DefectClass] = mapped_column(
        _pg_enum(DefectClass, "defect_class"),
        nullable=False,
        index=True,
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    # One or more closed rings in normalized [0..1] image coordinates, e.g.
    # [[{"x": 0.31, "y": 0.12}, ...], [{"x": 0.51, "y": 0.42}, ...]].
    # A mask with disconnected parts is stored as several rings: joining them
    # into one ring would draw an edge across the gap that is not in the mask.
    # JSONB keeps this schema-flexible without a related table per point.
    mask_polygon: Mapped[list[list[dict[str, float]]] | None] = mapped_column(
        JSONB, nullable=True
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    # Which object on this frame the detection belongs to: rows that share the
    # value are one piece of damage, because the model reports one crack as
    # several overlapping fragments. Assigned once, when the frame's detections
    # are written (``app.services.mask_objects``), never recomputed on read - so
    # every screen counts the same objects. Index 0 is a real object; there is no
    # "unset" value, which is why the column carries no default.
    object_index: Mapped[int] = mapped_column(Integer, nullable=False)

    image: Mapped[InspectionImage] = relationship(back_populates="defects")
    maps_3d: Mapped[list[DefectMap3D]] = relationship(
        back_populates="defect",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="confidence_score_range",
        ),
        # Filtering the verification queue by image + class.
        Index("ix_defects_image_id_defect_class", "image_id", "defect_class"),
        # Counting a project's objects never filters by defect id, so the
        # counting query reads this index and the rows it needs together.
        Index("ix_defects_image_id_object_index", "image_id", "object_index"),
    )


class Model3D(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An uploaded ``.glb`` mesh of the inspected building."""

    __tablename__ = "models_3d"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    s3_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    project: Mapped[Project] = relationship(back_populates="models_3d")
    defect_maps: Mapped[list[DefectMap3D]] = relationship(
        back_populates="model_3d",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class DefectMap3D(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Spatial localization of one defect on one 3D model.

    A defect may legitimately appear at several spots on the mesh (a crack that
    spans two facets), hence the join table is 2D: it is the result of the
    photogrammetry -> mesh projection step, not a plain FK.
    """

    __tablename__ = "defect_maps_3d"

    defect_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("defects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_3d_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models_3d.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    point_x: Mapped[float] = mapped_column(Float, nullable=False)
    point_y: Mapped[float] = mapped_column(Float, nullable=False)
    point_z: Mapped[float] = mapped_column(Float, nullable=False)

    defect: Mapped[Defect] = relationship(back_populates="maps_3d")
    model_3d: Mapped[Model3D] = relationship(back_populates="defect_maps")

    __table_args__ = (
        # 3D viewer loads every marker for a model; also serves the reverse
        # "which defects sit on this model" check.
        Index("ix_defect_maps_3d_model_3d_id_defect_id", "model_3d_id", "defect_id"),
    )
