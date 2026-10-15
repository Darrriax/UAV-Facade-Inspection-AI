"""Project business logic."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.core.storage import StorageBackend, cover_object_key, get_image_storage
from app.db.models import ImageStatus, Project, User, UserRole
from app.repositories.defect import DefectRepository
from app.repositories.image import InspectionImageRepository
from app.repositories.project import ProjectRepository
from app.schemas.common import PresignedUrlRead
from app.schemas.project import (
    ProjectCreate,
    ProjectRead,
    ProjectStats,
    ProjectStatus,
    ProjectUpdate,
)
from app.services.image import IncomingImage

logger = logging.getLogger(__name__)


def derive_project_status(stats: ProjectStats, *, stopped: bool = False) -> ProjectStatus:
    """Collapse a project's frame counters into the dashboard badge.

    A single ordered rule, so the badge can never disagree with the counters
    shown next to it:

    * no frames at all      -> ``draft``
    * stopped, work left    -> ``paused`` (the user asked for this, and it wins
                               over whatever the worker was doing: a stop is
                               honoured between frames, so frames can briefly
                               still read as ``processing`` after the click)
    * anything in flight    -> ``processing`` (a re-run or a second batch layered
                               on a finished project still reads as working)
    * frames not yet queued -> ``uploading``
    * nothing completed     -> ``failed`` (every attempted frame failed)
    * otherwise             -> ``completed``, even with some frames failed: a
                               partial failure is reported through ``stats``,
                               not by hiding a project's results behind a
                               ``failed`` badge.

    A stopped project with nothing left to do is deliberately *not* ``paused``:
    the flag can still be set on a finished batch (stop, then resume, then let it
    finish), and reporting ``paused`` for a project that has nothing pending
    would be a badge the user cannot act on.
    """
    if stats.total_images == 0:
        return ProjectStatus.DRAFT
    if stopped and (stats.processing_images > 0 or stats.pending_images > 0):
        return ProjectStatus.PAUSED
    if stats.processing_images > 0:
        return ProjectStatus.PROCESSING
    if stats.pending_images > 0:
        return ProjectStatus.UPLOADING
    if stats.completed_images == 0:
        return ProjectStatus.FAILED
    return ProjectStatus.COMPLETED


def _require_some_output(detect_defects: bool, needs_3d_model: bool) -> None:
    """Reject a project configuration that would produce nothing.

    Detection and the reconstruction are the pipeline's two outputs. With both
    off, a run would download every frame, mark it analysed and throw the result
    away - the user waits through a whole sortie and gets no artifact at all.
    Rejected at the edge (create *and* update) rather than silently corrected,
    because which of the two the user meant cannot be guessed.
    """
    if not detect_defects and not needs_3d_model:
        raise ValidationError(
            "Enable defect detection, the 3D model, or both - with neither, "
            "processing this project would produce no result."
        )


def _stats_from_counts(
    status_counts: dict[str, int],
    object_count: int,
    verified_count: int,
) -> ProjectStats:
    """Shape one project's grouped counts into the response model.

    Every status is filled in even at zero, so the UI renders a stable set of
    counters rather than inferring missing keys. ``object_count`` and
    ``verified_count`` are counts of objects - one crack, however many fragments
    the model reported it as - not counts of detection rows; see
    ``ProjectService.stats_for``.
    """
    return ProjectStats(
        total_images=sum(status_counts.values()),
        pending_images=status_counts.get(ImageStatus.PENDING.value, 0),
        processing_images=status_counts.get(ImageStatus.PROCESSING.value, 0),
        completed_images=status_counts.get(ImageStatus.COMPLETED.value, 0),
        failed_images=status_counts.get(ImageStatus.FAILED.value, 0),
        total_defects=object_count,
        verified_defects=verified_count,
    )


class ProjectService:
    """Inspection-campaign operations, always scoped to a principal."""

    def __init__(self, session: Session, storage: StorageBackend | None = None) -> None:
        self.session = session
        self.projects = ProjectRepository(session)
        # The dashboard rollup reads both tables; sharing the request's session
        # keeps the two grouped queries in the same unit of work as the listing.
        self.images = InspectionImageRepository(session)
        self.defects = DefectRepository(session)
        # Injectable so tests can substitute a fake and never touch S3, matching
        # ImageService. Cover photos share the images bucket: they are a handful
        # of objects per project, and a bucket of their own would only add a
        # second set of credentials to configure.
        self.storage = storage if storage is not None else get_image_storage()

    # ----------------------------------------------------------------- write
    def create(self, payload: ProjectCreate, *, current_user: User) -> Project:
        """Open a new inspection project.

        Business rule: only Inspectors may create projects. Admins manage users
        and infrastructure and do not own inspection campaigns, so they are
        rejected here rather than silently allowed.
        """
        if current_user.role != UserRole.INSPECTOR:
            raise PermissionDeniedError(
                "Only users with the 'inspector' role can create projects."
            )

        _require_some_output(payload.detect_defects, payload.needs_3d_model)

        project = self.projects.create(
            {
                "name": payload.name.strip(),
                "location": payload.location,
                "description": payload.description,
                "needs_3d_model": payload.needs_3d_model,
                "detect_defects": payload.detect_defects,
                # Owner comes from the authenticated principal, never the payload.
                "user_id": current_user.id,
            }
        )
        self.session.commit()
        return project

    def update(
        self,
        project_id: uuid.UUID,
        payload: ProjectUpdate,
        *,
        current_user: User,
    ) -> Project:
        project = self.get(project_id, current_user=current_user)
        data = payload.model_dump(exclude_unset=True)
        if not data:
            return project

        # Checked against the *merged* values, so a one-field update is judged by
        # the configuration it leaves behind rather than by what it carried.
        _require_some_output(
            data.get("detect_defects", project.detect_defects),
            data.get("needs_3d_model", project.needs_3d_model),
        )

        if "name" in data and data["name"] is not None:
            data["name"] = data["name"].strip()

        if data.get("main_photo_s3_path"):
            # The key is later turned into a presigned URL, so an unchecked
            # value would let a project point at somebody else's photo. Pin it
            # to this project's own prefix.
            expected_prefix = f"projects/{project_id}/"
            if not str(data["main_photo_s3_path"]).startswith(expected_prefix):
                raise ValidationError(
                    f"main_photo_s3_path must be an object key under '{expected_prefix}'."
                )

        project = self.projects.update(project, data)
        self.session.commit()
        self.session.refresh(project)
        return project

    def set_main_photo(
        self,
        project_id: uuid.UUID,
        upload: IncomingImage,
        *,
        current_user: User,
    ) -> Project:
        """Store a project's cover photo and point the row at it.

        The key is derived from the project id (``projects/<id>/cover<ext>``) -
        the same prefix :meth:`update` insists on - so the upload endpoint never
        gives a client a way to name an arbitrary object.

        Re-uploading replaces the cover. When the extension changes (a PNG in
        place of a JPEG) the old object is deleted too, because the key is
        derived from the extension and would otherwise survive as an orphan.
        Deletion is best-effort: the row already points at the new object by
        then, and failing the request would only make a successful upload look
        like a failure.
        """
        project = self.get(project_id, current_user=current_user)
        key = cover_object_key(project.id, upload.suffix)

        upload.fileobj.seek(0)
        self.storage.upload_stream(upload.fileobj, key, upload.content_type)

        previous_key = project.main_photo_s3_path
        try:
            project = self.projects.update(project, {"main_photo_s3_path": key})
            self.session.commit()
        except Exception:
            # The row still points at the old cover (or nothing): drop the new
            # object rather than leaving it behind unreferenced.
            self._delete_object(key)
            raise
        self.session.refresh(project)

        if previous_key and previous_key != key:
            self._delete_object(previous_key)
        return project

    def delete(self, project_id: uuid.UUID, *, current_user: User) -> None:
        project = self.get(project_id, current_user=current_user)
        self.projects.delete(project)
        # ON DELETE CASCADE clears images, defects and 3D mappings.
        self.session.commit()

    # ------------------------------------------------------- pipeline control
    def stop_processing(self, project_id: uuid.UUID, *, current_user: User) -> Project:
        """Ask the running analysis to stop, and keep it from being restarted.

        Cooperative by design: the API does not kill anything. The flag is read
        by the worker between frames (it holds an 8 GB ML image and a partially
        finished batch, so the one thing it must not do is die mid-frame), and
        any task that has not started yet exits before claiming a single frame -
        which is what makes this safe to press on a queued batch too.

        Idempotent, and the timestamp is refreshed on every call so an accidental
        double press cannot be mistaken for a second event.
        """
        project = self.get(project_id, current_user=current_user)
        project.processing_stopped_at = datetime.now(UTC)
        self.session.commit()
        self.session.refresh(project)
        logger.info("Project %s: processing stop requested by %s", project_id, current_user.email)
        return project

    def resume_processing(self, project_id: uuid.UUID, *, current_user: User) -> Project:
        """Clear the stop flag so the pending frames can be claimed again.

        Called by ``POST /start-processing``: resuming is starting, and keeping
        one endpoint for both means there is exactly one path that queues work
        (and therefore one path that has to clear this flag before it does).
        """
        project = self.get(project_id, current_user=current_user)
        if project.processing_stopped_at is not None:
            project.processing_stopped_at = None
            self.session.commit()
            self.session.refresh(project)
            logger.info("Project %s: processing resumed by %s", project_id, current_user.email)
        return project

    # ------------------------------------------------------- 3D build state
    def request_model3d(
        self,
        project_id: uuid.UUID,
        *,
        current_user: User,
    ) -> tuple[Project, bool]:
        """Flag a reconstruction as queued, and report whether it is a *new* one.

        Called before the Celery task is dispatched, so the flag exists in the
        database before anything can start working on it: the task clears the
        flag when it finishes, and a flag that landed after that would describe a
        build that is not running as one that is, forever.

        Idempotent on purpose - a second press while a build is in flight returns
        ``queued=False`` and dispatches nothing. A reconstruction costs tens of
        minutes of CPU, and the two ways a duplicate can be requested (a
        double-click, or a reload of a page that had already started one) are
        both ordinary user behaviour rather than an error to report.
        """
        project = self.get(project_id, current_user=current_user)
        if not project.needs_3d_model:
            raise ValidationError(
                "This project does not request a 3D model, so it has no "
                "reconstruction to build."
            )

        # Checked up front rather than in the worker: photogrammetry cannot start
        # below this many frames, and the user asking for it is the one who can
        # still do something about that (upload more).
        frames = self.images.count_by_project(project_id)
        if frames < settings.NODEODM_MIN_IMAGES:
            raise ValidationError(
                f"A 3D reconstruction needs at least {settings.NODEODM_MIN_IMAGES} "
                f"frames; this project has {frames}."
            )

        if project.model3d_requested_at is not None:
            return project, False

        project.model3d_requested_at = datetime.now(UTC)
        # A fresh attempt clears the verdict of the previous one: the error is
        # about *that* run, and the page would otherwise show a failure next to a
        # build that is running. The progress goes with it, and back to NULL
        # rather than 0: the new build has not reported a percentage yet, and a
        # bar that is waiting is not a bar at zero.
        project.model3d_error = None
        project.model3d_progress = None
        self.session.commit()
        self.session.refresh(project)
        logger.info(
            "Project %s: 3D reconstruction requested by %s", project_id, current_user.email
        )
        return project, True

    def finish_model3d(self, project_id: uuid.UUID, *, error: str | None) -> Project | None:
        """Close out a reconstruction, successfully or not.

        The one write the Celery task performs on the project, and the reason it
        takes no principal: the worker acts for the project, not for a user, and
        the request that queued the work is long gone by the time this runs. An
        owner predicate here would mean a reconstruction that nobody's session
        claims could never be reported as finished - leaving the page waiting on
        it forever.
        """
        project = self.projects.get(project_id)
        if project is None:
            logger.warning("Project %s disappeared before its 3D build finished", project_id)
            return None

        project.model3d_requested_at = None
        project.model3d_error = error
        # The percentage describes *that* build, so it goes down with the flag it
        # belongs to. Left behind, it would be read by the next page load as the
        # progress of whatever build comes next.
        project.model3d_progress = None
        self.session.commit()
        self.session.refresh(project)
        return project

    def note_model3d_progress(self, project_id: uuid.UUID, percent: int) -> None:
        """Record how far the reconstruction NodeODM is running has got.

        Called by the worker as NodeODM reports its percentage - the API never
        writes it, because no HTTP request knows how far a photogrammetry run is.

        Two deliberate silences. A project with no build in flight is left
        alone: the pipeline builds a model at the end of its own run, and that
        build reports progress too, but it closes itself out with the analysis
        rather than with the build flag, so a percentage written there would be
        read as the progress of a build that is not running. And a project that
        vanished, or a lost race with ``finish_model3d``, is not an error worth
        raising: the message is worth less than the reconstruction it describes.
        """
        project = self.projects.get(project_id)
        if project is None or project.model3d_requested_at is None:
            return

        # Clamped rather than trusted: the column is rendered as a bar, and a value
        # outside 0-100 would be a rendering bug manufactured in the database.
        percent = max(0, min(100, percent))
        if project.model3d_progress == percent:
            return
        project.model3d_progress = percent
        self.session.commit()

    def _delete_object(self, key: str) -> None:
        """Best-effort object cleanup, logged instead of raised."""
        try:
            self.storage.delete(key)
        except Exception:  # noqa: BLE001 - cleanup must not mask the real result
            logger.error("Could not delete stored object '%s'", key, exc_info=True)

    # ------------------------------------------------------------------ read
    def list(
        self,
        *,
        current_user: User,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Project], int]:
        """List the projects the caller may see.

        An Inspector sees only their own; an Admin sees every campaign on the
        installation - that is the Admin overview screen. This is a **read**
        widening only: ``create``/``update``/``delete`` keep their
        Inspector-only rules below, so a fuller listing cannot be mistaken for
        write access.
        """
        if current_user.role == UserRole.ADMIN:
            projects = self.projects.get_many_any_owner(offset=offset, limit=limit)
            return projects, self.projects.count()

        projects = self.projects.get_many_by_owner(
            current_user.id,
            offset=offset,
            limit=limit,
        )
        return projects, self.projects.count_by_owner(current_user.id)

    # ------------------------------------------------------------ read models
    def stats_for(
        self,
        project_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, ProjectStats]:
        """Frame and defect counters for several projects, in three queries.

        Batched on purpose: the dashboard asks for a whole page of projects at
        once, and per-project counting would make one screen cost 1 + 3N round
        trips. Every aggregate is indexed on ``project_id``, so this stays cheap
        as the frame count grows.

        ``total_defects`` and ``verified_defects`` count *objects*, not detection
        rows: the model reports one crack as several fragments and the grouping
        lives in ``defects.object_index``, so these are the numbers a card shows
        and the numbers the workspace lists (see
        :meth:`DefectRepository.count_objects_by_project` and
        :meth:`DefectRepository.count_verified_objects_by_project`). The verified
        count exists so a screen that holds no defect rows - the reconstruction
        viewer - can still print the same three totals the workspace's header
        does, rather than fetching every detection in the project for a
        subtitle.
        """
        if not project_ids:
            return {}
        status_counts = self.images.status_counts_for_projects(project_ids)
        object_counts = self.defects.count_objects_by_project(project_ids)
        verified_counts = self.defects.count_verified_objects_by_project(project_ids)
        return {
            project_id: _stats_from_counts(
                status_counts.get(project_id, {}),
                object_counts.get(project_id, 0),
                verified_counts.get(project_id, 0),
            )
            for project_id in project_ids
        }

    def present(self, project: Project) -> ProjectRead:
        """Attach the rollup to a single project row."""
        return self.present_many([project])[0]

    def present_many(self, projects: Sequence[Project]) -> list[ProjectRead]:
        """Attach the rollup to ORM rows, producing the API response shape.

        The one place project responses are assembled, which is what keeps
        ``status`` and ``stats`` consistent with each other and with the
        database on *every* endpoint - the listing, the detail read, and the
        write paths that echo a project back. A response built from the bare
        ORM row would report zeroed counters, so no endpoint should do that.
        """
        if not projects:
            return []

        stats_by_id = self.stats_for([project.id for project in projects])
        readings: list[ProjectRead] = []
        for project in projects:
            stats = stats_by_id[project.id]
            readings.append(
                ProjectRead.model_validate(project).model_copy(
                    update={
                        "stats": stats,
                        "status": derive_project_status(
                            stats, stopped=project.processing_stopped_at is not None
                        ),
                    }
                )
            )
        return readings

    def main_photo_url(
        self,
        project_id: uuid.UUID,
        *,
        current_user: User,
    ) -> PresignedUrlRead:
        """Sign a project's cover photo so the browser can fetch it directly.

        The bytes never pass through FastAPI: the app hands the client a
        short-lived URL and the S3 endpoint serves the object. Visibility is the
        same as the read path (``get_visible``), so an Admin may preview any
        project's cover while an Inspector sees only their own.
        """
        project = self.get_visible(project_id, current_user=current_user)
        if not project.main_photo_s3_path:
            # 404 rather than a null URL: "this project has no cover yet" is the
            # same class of answer as "this project does not exist", and the
            # client falls back to its placeholder for both.
            raise NotFoundError(f"Project {project_id} has no cover photo.")

        return PresignedUrlRead(
            url=self.storage.presigned_url(project.main_photo_s3_path),
            expires_in=settings.S3_PRESIGNED_URL_TTL_SECONDS,
        )

    def get(self, project_id: uuid.UUID, *, current_user: User) -> Project:
        """Owner-scoped fetch, used by the write paths.

        Admins are deliberately *not* exempt here: every mutation (edit, delete,
        image upload) goes through this method, and an Admin is not supposed to
        own inspection campaigns. Read-only admin access goes through
        :meth:`get_visible` instead.
        """
        project = self.projects.get_owned(project_id, current_user.id)
        if project is None:
            # 404 rather than 403 on purpose: a 403 would confirm that somebody
            # else's project with this id exists.
            raise NotFoundError(f"Project {project_id} was not found.")
        return project

    def get_visible(self, project_id: uuid.UUID, *, current_user: User) -> Project:
        """Read-only fetch for the GET endpoints.

        Same contract as :meth:`get`, except an Admin may open any project (the
        Admin needs to inspect what an Inspector uploaded). Kept as a separate
        method so the widening cannot leak into a mutation by accident.
        """
        if current_user.role == UserRole.ADMIN:
            project = self.projects.get(project_id)
        else:
            project = self.projects.get_owned(project_id, current_user.id)

        if project is None:
            raise NotFoundError(f"Project {project_id} was not found.")
        return project
