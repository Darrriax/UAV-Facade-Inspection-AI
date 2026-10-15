"""Image-analysis pipeline: YOLOv9-c segmentation, then optional photogrammetry.

Transaction shape
-----------------
The task holds **no database transaction while it does work**. A reconstruction
can run for tens of minutes, and keeping a transaction (and its pooled
connection) open across that would starve the API. Instead the run is split into
short phases, each with its own session:

1. claim      - lock the pending frames, flip them to ``processing``, commit
2. download   - object storage only, no session
3. inference  - CPU/GPU only, no session
4. persist    - one small transaction *per frame*, so frame 70 failing cannot
                roll back the 69 that succeeded
5. 3D model   - NodeODM + upload, no session; a short session at the end only to
                register the finished mesh

Ordering on the failure side is deliberate too: analysis results are the
thesis's primary output, so the optional 3D step is attempted only after the
detections are safely committed, and its failure never marks the frames failed.

Rebuilding the 3D model
-----------------------
The module exposes a *second* task, ``build_model3d_task``, which reconstructs a
model from frames the project already holds. It exists because the 3D phase below
is reachable only from a run that claimed frames: an interrupted reconstruction
leaves every frame ``completed``, so the analysis task returns before it ever
reaches NodeODM and the project could never get its model. The rebuild task
claims nothing and changes no frame status - it downloads, reconstructs, and
reports the outcome on the project's own build flag, along with how far the run
has got while it works (that task feeds the 3D page's progress bar; the analysis
pipeline has no such bar to feed).

Detection off
-------------
A project can be set up to skip detection altogether (``projects.detect_defects``),
which is what a project that exists only for its 3D reconstruction does. The run
then never constructs the detector - no weights are loaded and no mask is ever
produced - and every claimed frame is marked ``completed`` on its own, because the
progress screen and the project badge read frame *statuses*: a frame nothing was
going to look at has to read as done, not as awaiting work. The frames are still
downloaded, the reconstruction being built from those same files, and the 3D step
then runs exactly as it does with detection on.

Stopping
--------
``projects.processing_stopped_at`` is the stop signal, and this module is the
only thing that reads it as one. It is checked *cooperatively* - before the
claim, between downloads, before every frame, before the 3D step and on every
NodeODM status poll - because the alternative (revoking or killing the task)
would discard a frame mid-inference and leave the batch half-stateful. On a stop,
the frames the run never reached go back to ``pending``, which is what makes
``POST /projects/{id}/start-processing`` a true resume: it clears the flag and
queues a task that claims exactly those frames.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from celery.utils.log import get_task_logger
from sqlalchemy import select

from app.core.config import settings
from app.core.storage import get_image_storage
from app.db.database import SessionLocal
from app.db.models import InspectionImage, Project
from app.repositories.project import ProjectRepository
from app.services.image import ImageService
from app.services.inference import DetectedDefect, InferenceService
from app.services.model3d import Model3DService
from app.services.photogrammetry import PhotogrammetryService
from app.services.project import ProjectService
from app.tasks.celery_app import BUILD_MODEL3D_TASK, PROCESS_IMAGES_TASK, celery_app

logger = get_task_logger(__name__)

#: How often the download phase asks whether a stop was requested.
#:
#: Downloading is I/O, not CPU: a checkpoint every N files bounds how long a stop
#: waits behind a large batch without turning the loop into a query per frame.
STOP_CHECK_INTERVAL = 25


@celery_app.task(bind=True, name=PROCESS_IMAGES_TASK)
def process_images_task(self, project_id: str) -> dict[str, object]:
    """Analyse every pending frame of a project and optionally build a 3D model.

    ``project_id`` travels as a string on purpose: Celery's JSON serializer
    cannot encode ``uuid.UUID``, so passing one would fail at publish time
    rather than in the worker.
    """
    logger.info("Processing started for project %s", project_id)

    try:
        parsed_project_id = uuid.UUID(project_id)
    except ValueError:
        # Not retryable: a malformed id will never become valid.
        logger.error("process_images_task received a malformed project id: %r", project_id)
        return {"project_id": project_id, "status": "invalid_project_id"}

    # ---------------------------------------------------------------- phase 1
    #: Frames a previous, dead run was still holding when this one started.
    requeued = 0
    with SessionLocal() as session:
        project = ProjectRepository(session).get(parsed_project_id)
        if project is None:
            logger.error("Project %s no longer exists; nothing to process", project_id)
            return {"project_id": project_id, "status": "project_not_found"}

        needs_3d_model = project.needs_3d_model
        detect_defects = project.detect_defects
        # Before the claim, which matters for a task that was queued and then
        # stopped while it waited: nothing is claimed, so nothing has to be
        # released, and the frames stay `pending` for the resume.
        if project.processing_stopped_at is not None:
            logger.info("Project %s: a stop was requested before this run started", project_id)
            return {
                "project_id": project_id,
                "status": "stopped",
                "processed": 0,
                "released_frames": 0,
                "status_breakdown": _status_breakdown(parsed_project_id),
            }

        images = ImageService(session)
        claimed = images.claim_pending(parsed_project_id)
        if not claimed:
            # Nothing is pending - but that does not mean nothing is left. A run
            # that was killed (the OOM killer is a real visitor here: a batch
            # holds the detector's memory and every frame it downloads) leaves
            # the frames it claimed sitting in `processing` with nobody working
            # on them, and the project then cannot be finished from the UI at
            # all. Age is the only evidence there is; see the repository method.
            requeued = images.requeue_stale(
                parsed_project_id,
                older_than_seconds=settings.PROCESSING_STALE_AFTER_SECONDS,
            )
            if requeued:
                logger.warning(
                    "Project %s: %d frame(s) were left in 'processing' by a previous "
                    "run; they are back in the queue",
                    project_id,
                    requeued,
                )
                claimed = images.claim_pending(parsed_project_id)
        # Detach to plain values: the session closes before the heavy work.
        work_items = [(image.id, image.s3_path, image.file_path) for image in claimed]

    if not work_items:
        logger.info("Project %s has no pending images", project_id)
        return {
            "project_id": project_id,
            "status": "nothing_to_do",
            "processed": 0,
            "requeued_frames": requeued,
            "status_breakdown": _status_breakdown(parsed_project_id),
        }

    counts = {"completed": 0, "failed": 0, "defects": 0}
    synthetic_mode = False
    model_key: str | None = None

    workdir = Path(tempfile.mkdtemp(prefix=f"facade-{project_id}-"))
    logger.info(
        "Project %s: working in %s with %d claimed image(s)",
        project_id,
        workdir,
        len(work_items),
    )
    try:
        # ------------------------------------------------------------ phase 2
        downloaded: dict[uuid.UUID, Path] = {}
        stopped = False
        for index, (image_id, s3_path, filename) in enumerate(work_items):
            if index % STOP_CHECK_INTERVAL == 0 and _stop_requested(parsed_project_id):
                stopped = True
                break
            if not s3_path:
                counts["failed"] += 1
                _mark_failed(image_id, "no object key stored for this image")
                continue
            try:
                downloaded[image_id] = _download(s3_path, workdir)
            except Exception as exc:  # noqa: BLE001 - one bad object must not sink the batch
                logger.exception("Project %s: could not download %s", project_id, s3_path)
                counts["failed"] += 1
                _mark_failed(image_id, f"download failed: {exc}")

        if stopped:
            # Nothing has been analysed yet, so every claimed frame goes back.
            counts["released"] = _release_claimed([image_id for image_id, _, _ in work_items])
            logger.info(
                "Project %s: stopped while downloading; %d frame(s) back to pending",
                project_id,
                counts["released"],
            )
            return _result(
                project_id,
                counts,
                needs_3d_model,
                model_key,
                synthetic_mode,
                detect_defects=detect_defects,
                status="stopped",
            )

        if not downloaded:
            logger.error("Project %s: no frames could be downloaded", project_id)
            return _result(
                project_id,
                counts,
                needs_3d_model,
                model_key,
                synthetic_mode,
                detect_defects=detect_defects,
            )

        # ------------------------------------------------------------ phase 3
        # Constructed only when this project asks for it: building it loads the
        # checkpoint (or decides it is missing), and a 3D-only project pays for
        # neither - not the memory, and not the warning about synthetic output
        # for detections it was never going to store.
        service: InferenceService | None = None
        if detect_defects:
            service = InferenceService()
            synthetic_mode = service.is_synthetic
            if synthetic_mode:
                logger.warning(
                    "Project %s: running in SYNTHETIC mode (%s). Detections are "
                    "fabricated and must not be used for accuracy evaluation.",
                    project_id,
                    service.unavailable_reason,
                )
        else:
            logger.info(
                "Project %s: defect detection is off; %d frame(s) will be marked "
                "analysed without running the detector",
                project_id,
                len(downloaded),
            )

        #: Frames this run has taken responsibility for - completed *or* failed.
        #: Everything else in `downloaded` is untouched and has to go back to
        #: `pending` if the run stops; a frame that failed keeps that status,
        #: because re-running it is a decision for the user, not for a stop.
        handled: set[uuid.UUID] = set()

        for image_id, path in downloaded.items():
            # Checked per frame, not per batch: this is the loop a stop has to
            # interrupt, and one primary-key read is nothing next to the seconds
            # of inference that follow it. A frame in flight always finishes - the
            # model is loaded once and killing mid-detect would waste it.
            if _stop_requested(parsed_project_id):
                stopped = True
                break

            if service is None:
                # Detection is off: nothing to infer and nothing to store, but the
                # frame still has to leave `processing` - and leave it as
                # *analysed*, which is what the progress screen and the project
                # badge read. No rows are written, so the frame carries no mask.
                if _mark_completed(image_id):
                    counts["completed"] += 1
                else:
                    counts["failed"] += 1
                handled.add(image_id)
                continue

            try:
                detections = service.detect(path, seed=str(image_id))
            except Exception as exc:  # noqa: BLE001 - per-frame isolation
                logger.exception("Project %s: inference failed for %s", project_id, path.name)
                counts["failed"] += 1
                _mark_failed(image_id, f"inference failed: {exc}")
                handled.add(image_id)
                continue

            stored = _persist_detections(image_id, detections)
            handled.add(image_id)
            if stored is None:
                counts["failed"] += 1
                continue
            counts["completed"] += 1
            counts["defects"] += stored

        logger.info(
            "Project %s: %d frame(s) completed, %d failed, %d defect(s) stored",
            project_id,
            counts["completed"],
            counts["failed"],
            counts["defects"],
        )

        if stopped:
            counts["released"] = _release_claimed(
                [image_id for image_id in downloaded if image_id not in handled]
            )
            logger.info(
                "Project %s: stopped after %d frame(s); %d frame(s) back to pending",
                project_id,
                len(handled),
                counts["released"],
            )
            return _result(
                project_id,
                counts,
                needs_3d_model,
                model_key,
                synthetic_mode,
                detect_defects=detect_defects,
                status="stopped",
            )

        # ------------------------------------------------------------ phase 4
        image_paths = list(downloaded.values())
        if not needs_3d_model:
            logger.info("Project %s: needs_3d_model is false; skipping photogrammetry", project_id)
        elif len(image_paths) < settings.NODEODM_MIN_IMAGES:
            logger.info(
                "Project %s: %d image(s) is below the %d-image minimum for "
                "photogrammetry; skipping the 3D model",
                project_id,
                len(image_paths),
                settings.NODEODM_MIN_IMAGES,
            )
        elif _stop_requested(parsed_project_id):
            # Reached between the last frame and the reconstruction: the stop wins,
            # because the 3D step is the longest part of the run and starting it
            # only to cancel it a minute later helps nobody.
            logger.info("Project %s: stopped before the 3D step; skipping photogrammetry", project_id)
        else:
            model_key, _ = _build_model(parsed_project_id, image_paths, workdir)
    finally:
        # Frames and ODM output can be gigabytes; never leave them behind.
        shutil.rmtree(workdir, ignore_errors=True)

    return _result(
        project_id,
        counts,
        needs_3d_model,
        model_key,
        synthetic_mode,
        detect_defects=detect_defects,
    )


# --------------------------------------------------- standalone reconstruction
@celery_app.task(bind=True, name=BUILD_MODEL3D_TASK)
def build_model3d_task(self, project_id: str) -> dict[str, object]:
    """Reconstruct a project's 3D model from the frames it already holds.

    The 3D phase of ``process_images_task`` runs at the end of an analysis run,
    which means it only ever happens for frames that run *claimed*. A project
    whose reconstruction was interrupted - the worker was restarted, the machine
    went to sleep, or the first run skipped the step - has all its frames
    ``completed`` and therefore nothing to claim, so starting processing again
    returns "nothing to do" before it reaches NodeODM. This task is the way back
    from that state, and it is what the 3D page's "build the model" action
    queues.

    Frames are read, never consumed: no status changes, no claim, no transaction
    held while NodeODM works. The only write is the project's build flag, closed
    out at the end with either a model or the reason there is none.
    """
    logger.info("3D reconstruction requested for project %s", project_id)

    try:
        parsed_project_id = uuid.UUID(project_id)
    except ValueError:
        logger.error("build_model3d_task received a malformed project id: %r", project_id)
        return {"project_id": project_id, "status": "invalid_project_id"}

    # ---------------------------------------------------------------- phase 1
    with SessionLocal() as session:
        project = ProjectRepository(session).get(parsed_project_id)
        if project is None:
            logger.error("Project %s no longer exists; nothing to reconstruct", project_id)
            return {"project_id": project_id, "status": "project_not_found"}

        needs_3d_model = project.needs_3d_model
        # Detached to plain values before the heavy work, like the analysis task:
        # the session is closed while the frames are being downloaded.
        frames = [
            (image.id, image.s3_path)
            for image in ImageService(session).stored_frames(parsed_project_id)
        ]

    if not needs_3d_model:
        logger.info("Project %s does not request a 3D model; nothing to build", project_id)
        _finish_model3d(parsed_project_id, error=None)
        return {"project_id": project_id, "status": "not_requested", "images": 0}

    if len(frames) < settings.NODEODM_MIN_IMAGES:
        reason = (
            f"Only {len(frames)} frame(s) are stored, and a reconstruction needs at "
            f"least {settings.NODEODM_MIN_IMAGES}."
        )
        logger.info("Project %s: %s", project_id, reason)
        _finish_model3d(parsed_project_id, error=reason)
        return {"project_id": project_id, "status": "too_few_images", "images": len(frames)}

    if not settings.NODEODM_ENABLED:
        logger.info("NodeODM is disabled (NODEODM_ENABLED=false); not building a model")
        _finish_model3d(parsed_project_id, error=None)
        return {"project_id": project_id, "status": "disabled", "images": len(frames)}

    # ---------------------------------------------------------------- phase 2
    progress = _Model3DProgress(parsed_project_id)
    model_key: str | None = None
    build_error: str | None = None
    stopped = False
    failed_downloads = 0
    image_paths: list[Path] = []
    workdir = Path(tempfile.mkdtemp(prefix=f"facade-3d-{project_id}-"))
    logger.info(
        "Project %s: reconstructing from %d stored frame(s) in %s",
        project_id,
        len(frames),
        workdir,
    )
    try:
        for index, (_, s3_path) in enumerate(frames):
            if index % STOP_CHECK_INTERVAL == 0 and _stop_requested(parsed_project_id):
                stopped = True
                break
            # 1-5%: the frames coming back out of object storage. NodeODM has
            # nothing to report yet (it has not been handed a single file), so
            # this stretch of the bar exists only here - and for a few hundred
            # frames it is minutes of a user staring at the page.
            progress(1 + int(4 * index / len(frames)))
            if not s3_path:
                continue
            try:
                image_paths.append(_download(s3_path, workdir))
            except Exception:  # noqa: BLE001 - one unreadable object must not sink the run
                logger.exception("Project %s: could not download %s", project_id, s3_path)
                failed_downloads += 1
        progress(5)

        # Below the minimum there is nothing to hand to NodeODM: the same check
        # the API made before queueing this, applied to what actually came back
        # out of object storage.
        if not stopped and len(image_paths) >= settings.NODEODM_MIN_IMAGES:
            model_key, build_error = _build_model(
                parsed_project_id, image_paths, workdir, progress
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if stopped or _stop_requested(parsed_project_id):
        logger.info("Project %s: the 3D reconstruction was stopped", project_id)
        _finish_model3d(parsed_project_id, error=None)
        return {
            "project_id": project_id,
            "status": "stopped",
            "images": len(frames),
            "model_3d_key": None,
        }

    error: str | None = None
    if model_key is None:
        # Two different failures with two different next steps, so they get two
        # different messages: frames that could not be *read* are a storage
        # problem, while frames NodeODM could not *solve* are a dataset problem.
        if failed_downloads > 0 and len(image_paths) < settings.NODEODM_MIN_IMAGES:
            error = (
                "The frames could not be downloaded from storage, so there was nothing "
                "to reconstruct. Check `docker compose logs worker` and the storage "
                "service."
            )
        elif build_error is not None:
            # The real reason, as NodeODM or the reconstruction reported it (a
            # timeout, a refused task, a dataset ODM could not solve).
            error = f"The 3D reconstruction did not finish: {build_error}"
        else:
            error = (
                "The reconstruction produced no model. Check `docker compose logs "
                "worker` for the NodeODM stage that failed."
            )
        logger.warning(
            "Project %s: no model was produced (%d frame(s), %d unreadable)",
            project_id,
            len(frames),
            failed_downloads,
        )
    _finish_model3d(parsed_project_id, error=error)

    return {
        "project_id": project_id,
        "status": "completed" if model_key else "failed",
        "images": len(frames),
        "downloaded": len(image_paths),
        "failed_downloads": failed_downloads,
        "model_3d_key": model_key,
    }


class _Model3DProgress:
    """Report a reconstruction's completion to the project row, throttled.

    The worker is the only process that can ask NodeODM how far a run has got,
    so it writes the percentage onto the project while it works and the 3D page
    polls it - without that, the page has nothing to show but a spinner, and a
    reconstruction lasts up to an hour.

    Writes are capped at one per whole percent: NodeODM holds the same value for
    minutes at a time, and the status callback that feeds this fires every few
    seconds. A failed write is logged and swallowed - a missing progress line is
    never worth failing a build that is otherwise doing fine, and the next
    percent will be along in a moment.
    """

    def __init__(self, project_id: uuid.UUID) -> None:
        self._project_id = project_id
        self._last = -1

    def __call__(self, percent: int) -> None:
        percent = max(0, min(100, percent))
        if percent == self._last:
            return
        self._last = percent
        try:
            with SessionLocal() as session:
                ProjectService(session).note_model3d_progress(self._project_id, percent)
        except Exception:  # noqa: BLE001 - reporting must not fail the task
            logger.exception("Could not record 3D progress for project %s", self._project_id)


def _finish_model3d(project_id: uuid.UUID, *, error: str | None) -> None:
    """Clear the project's in-flight 3D flag. Never raises.

    The flag has to come down whatever happened - a model, a refusal, an
    exception - otherwise the 3D page keeps reporting a build that is not
    running and never offers to start another one. A failure to clear it is
    logged and swallowed for the same reason. The progress percentage is cleared
    with it: it describes the build the flag was standing for.
    """
    try:
        with SessionLocal() as session:
            ProjectService(session).finish_model3d(project_id, error=error)
    except Exception:  # noqa: BLE001 - reporting must not fail the task
        logger.exception("Could not close out the 3D build state for project %s", project_id)


# ------------------------------------------------------------------ 3D model
def _build_model(
    project_id: uuid.UUID,
    image_paths: list[Path],
    workdir: Path,
    progress: _Model3DProgress | None = None,
) -> tuple[str | None, str | None]:
    """Run photogrammetry and register the mesh. Never raises.

    Returns ``(model_key, error)``: the key when a mesh was stored, otherwise the
    reason NodeODM gave for not producing one.

    ``should_stop`` is polled by the service while it waits on NodeODM, so a stop
    pressed during a reconstruction cancels the ODM task instead of holding the
    worker (and the machine) for the rest of an hour-long run.

    ``progress`` is the build state this call is allowed to write into, taken as
    an argument rather than constructed here because the caller owns the phases
    around this one: the analysis pipeline passes nothing, since the model it
    builds at the end of a run reports through the frames it is processing, not
    through the 3D page's bar.
    """

    def on_odm_progress(odm_percent: int) -> None:
        # ODM's 0-100 squeezed into 6-95: the frames came out of storage before
        # this (1-5%), and the textured model still has to be fetched from
        # NodeODM and put into object storage afterwards (95-100%). Passing the
        # number through unchanged would print 100% minutes before there is
        # anything to render, which is the one thing a progress bar must not do.
        if progress is not None:
            progress(6 + int(89 * odm_percent / 100))

    service = PhotogrammetryService()
    key = service.reconstruct(
        project_id,
        image_paths,
        workdir,
        should_stop=lambda: _stop_requested(project_id),
        on_progress=on_odm_progress,
    )
    if key is None:
        # Where the bar stopped is left alone: the closeout clears the percentage,
        # and the page shows the worker's reason instead of a stuck bar.
        logger.warning("Project %s: no 3D model was produced", project_id)
        return None, service.last_error

    with SessionLocal() as session:
        Model3DService(session).register(project_id, key)
    if progress is not None:
        # 100 means "in storage", which is exactly when this page's next poll
        # finds a model rather than a percentage.
        progress(100)
    logger.info("Project %s: registered 3D model %s", project_id, key)
    return key, None


# ------------------------------------------------------------------- helpers
def _stop_requested(project_id: uuid.UUID) -> bool:
    """Has the user asked this project's analysis to stop?

    Read fresh on every check - caching it would defeat the purpose, since the
    change is made by another process while this one is mid-batch. A single
    primary-key lookup on a short-lived session, so a stop cannot leave a
    transaction open behind it either.
    """
    with SessionLocal() as session:
        stopped_at = session.execute(
            select(Project.processing_stopped_at).where(Project.id == project_id)
        ).scalar_one_or_none()
    return stopped_at is not None


def _release_claimed(image_ids: Sequence[uuid.UUID]) -> int:
    """Hand frames this run never reached back to the queue. Never raises."""
    if not image_ids:
        return 0
    try:
        with SessionLocal() as session:
            return ImageService(session).release_claimed(list(image_ids))
    except Exception:  # noqa: BLE001 - a failed release must not hide the stop
        logger.exception("Could not release %d claimed frame(s)", len(image_ids))
        return 0


def _download(s3_path: str, workdir: Path) -> Path:
    """Fetch one frame from object storage.

    Deliberately session-free: this is I/O against S3, and opening a database
    connection just to reach the storage helper would hold it for the whole
    transfer.
    """
    return get_image_storage().download_to(s3_path, workdir / Path(s3_path).name)


def _persist_detections(image_id: uuid.UUID, detections: list[DetectedDefect]) -> int | None:
    """Store one frame's detections in its own transaction.

    Returns the number of defects written, or ``None`` if persistence failed -
    the frame is then marked ``failed`` in a separate transaction so the failure
    is visible in the UI instead of leaving it stuck in ``processing``.
    """
    with SessionLocal() as session:
        image = session.get(InspectionImage, image_id)
        if image is None:
            logger.warning("Image %s disappeared before its results could be stored", image_id)
            return None
        try:
            return len(ImageService(session).store_defects(image, detections))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not store detections for image %s", image_id)
            session.rollback()
            _mark_failed(image_id, f"persistence failed: {exc}")
            return None


def _mark_completed(image_id: uuid.UUID) -> bool:
    """Mark one frame analysed without storing anything. Never raises.

    Returns ``False`` when the frame could not be marked (it is gone, or the
    write failed), which the caller reports as a failed frame - the same
    treatment every other per-frame failure gets, so a frame can never be left
    in ``processing`` by this path.
    """
    with SessionLocal() as session:
        image = session.get(InspectionImage, image_id)
        if image is None:
            logger.warning("Image %s disappeared before it could be marked analysed", image_id)
            return False
        try:
            ImageService(session).mark_completed(image)
            return True
        except Exception:  # noqa: BLE001 - one frame must not sink the batch
            logger.exception("Could not mark image %s as analysed", image_id)
            session.rollback()
            _mark_failed(image_id, "could not be marked as analysed")
            return False


def _mark_failed(image_id: uuid.UUID, reason: str) -> None:
    with SessionLocal() as session:
        image = session.get(InspectionImage, image_id)
        if image is None:
            return
        ImageService(session).mark_failed(image, reason)


def _status_breakdown(project_id: uuid.UUID) -> dict[str, int]:
    with SessionLocal() as session:
        return ImageService(session).status_breakdown(project_id)


def _result(
    project_id: str,
    counts: dict[str, int],
    needs_3d_model: bool,
    model_key: str | None,
    synthetic_mode: bool,
    *,
    detect_defects: bool = True,
    status: str = "completed",
) -> dict[str, object]:
    """Shape the task result.

    ``status`` is ``stopped`` when the run was interrupted by the user, which is
    the one outcome a caller must not read as "the batch is done": the frames are
    still there and ``released_frames`` of them are queued for a resume.

    ``inference_mode`` is ``skipped`` for a project that never runs the detector -
    the third answer next to ``model`` and ``synthetic``, and the one a reader
    needs in order to explain a run that stored no defect at all.
    """
    parsed_project_id = uuid.UUID(project_id)
    with SessionLocal() as session:
        images = ImageService(session)
        breakdown = images.status_breakdown(parsed_project_id)
        defect_classes = images.defect_breakdown(parsed_project_id)

    return {
        "project_id": project_id,
        "status": status,
        "processed": counts["completed"],
        "failed": counts["failed"],
        "defects": counts["defects"],
        # Frames the run claimed but never reached: they are `pending` again, so a
        # resume picks them up.
        "released_frames": counts.get("released", 0),
        "defect_classes": defect_classes,
        "status_breakdown": breakdown,
        "needs_3d_model": needs_3d_model,
        "model_3d_key": model_key,
        # Surfaced so a caller can tell a real run from the missing-weights
        # fallback - or from a project that never asked for detection - without
        # digging through logs.
        "inference_mode": (
            "skipped" if not detect_defects else ("synthetic" if synthetic_mode else "model")
        ),
    }
