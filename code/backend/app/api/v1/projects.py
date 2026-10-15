"""``/projects`` endpoints.

Every handler receives the acting principal and passes it down; the service and
repository layers turn it into an ownership predicate, so no handler can
accidentally expose another inspector's data.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Annotated, BinaryIO

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.api.deps import (
    CurrentUser,
    DefectServiceDep,
    ImageServiceDep,
    Model3DServiceDep,
    PaginationDep,
    ProjectServiceDep,
)
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.storage import ALLOWED_IMAGE_SUFFIXES, CONTENT_TYPE_SUFFIX
from app.db.models import ImageStatus
from app.schemas.common import Page, PresignedUrlRead
from app.schemas.defect import DefectCreate, DefectRead, DefectUpdate
from app.schemas.image import (
    BulkImageUploadResponse,
    ImageStatsRead,
    InspectionImageRead,
    ProcessingStartResponse,
    ReviewFrameRead,
)
from app.schemas.model3d import Model3DRead
from app.schemas.project import ProjectCreate, ProjectRead, ProjectUpdate
from app.services.image import IncomingImage
from app.tasks.celery_app import BUILD_MODEL3D_TASK, PROCESS_IMAGES_TASK, celery_app

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])

#: Canonical extension -> content type. The content type declared to S3 always
#: comes from this table, never from the client's ``Content-Type`` header: a
#: spoofed upload must not end up stored (and later served) as ``text/html``.
SUFFIX_CONTENT_TYPE: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}

#: Alias extensions folded onto their canonical form.
CANONICAL_SUFFIX: dict[str, str] = {
    ".jpg": ".jpg",
    ".jpeg": ".jpg",
    ".png": ".png",
    ".tif": ".tiff",
    ".tiff": ".tiff",
    ".webp": ".webp",
}


def _stream_size(fileobj: BinaryIO) -> int:
    """Size of a seekable upload stream, leaving the read position at 0."""
    fileobj.seek(0, os.SEEK_END)
    size = fileobj.tell()
    fileobj.seek(0)
    return size


#: Content types a browser sends for the mesh it just rebuilt.
#:
#: `model/gltf-binary` is what the API itself serves a `.glb` as, and what a
#: caller that knows the format will declare. `application/octet-stream` - or no
#: type at all - is what a `Blob` part carries when the page lets the browser
#: decide, which is the common case for an upload driven by `fetch`.
MESH_CONTENT_TYPES: frozenset[str] = frozenset(
    {"model/gltf-binary", "application/octet-stream", ""}
)


def _validated_mesh(file: UploadFile) -> None:
    """Check one uploaded mesh part, or raise the HTTP error that explains it.

    Deliberately not the image validator: a mesh has a single accepted format
    and its own size ceiling, and the two paths must not be able to drift into
    accepting each other's files. The file name is checked but never used - the
    object key comes from the project id (see ``Model3DService.store_mesh``), so
    a crafted name has nothing to attach itself to.
    """
    declared_type = (file.content_type or "").split(";")[0].strip().lower()
    if declared_type not in MESH_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type '{declared_type or 'unknown'}'. A 3D "
                "model must be a binary glTF (`.glb`)."
            ),
        )

    # Any directory component is stripped before it can reach an error message.
    filename = PurePosixPath(file.filename or "").name
    if PurePosixPath(filename).suffix.lower() != ".glb":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"'{filename or 'unnamed'}' is not a `.glb`. A 3D model must "
                "be a binary glTF."
            ),
        )

    size = _stream_size(file.file)
    if size == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The uploaded 3D model is empty.",
        )
    if size > settings.UPLOAD_MAX_MODEL_BYTES:
        limit_mib = settings.UPLOAD_MAX_MODEL_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"The 3D model is {size} bytes, over the {limit_mib:g} MiB "
                "limit."
            ),
        )


def _validated_uploads(files: Sequence[UploadFile]) -> list[IncomingImage]:
    """Turn multipart parts into validated :class:`IncomingImage` descriptors.

    These limits live in the transport layer rather than in the service: they
    are properties of the HTTP payload, and the service must stay callable from
    a Celery task, where there is no request at all.
    """
    if not files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one image file is required.",
        )

    if len(files) > settings.UPLOAD_MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"At most {settings.UPLOAD_MAX_FILES_PER_REQUEST} files per request, "
                f"received {len(files)}."
            ),
        )

    uploads: list[IncomingImage] = []
    for part in files:
        declared_type = (part.content_type or "").split(";")[0].strip().lower()
        # Strip any directory component: a crafted filename must never reach the
        # object key or the database.
        filename = PurePosixPath(part.filename or "").name

        suffix = CONTENT_TYPE_SUFFIX.get(declared_type)
        if suffix is None:
            # Clients sometimes send a generic type for a perfectly good image,
            # so fall back to a whitelisted *extension* in that case only.
            candidate = PurePosixPath(filename).suffix.lower()
            generic_type = declared_type in {"", "application/octet-stream"}
            if generic_type and candidate in ALLOWED_IMAGE_SUFFIXES:
                suffix = CANONICAL_SUFFIX[candidate]
            else:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail=(
                        f"Unsupported file type '{declared_type or 'unknown'}' for "
                        f"'{filename or 'unnamed'}'. Allowed: JPEG, PNG, TIFF, WebP."
                    ),
                )

        size = _stream_size(part.file)
        if size == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"'{filename or 'unnamed'}' is empty.",
            )
        if size > settings.UPLOAD_MAX_FILE_BYTES:
            limit_mib = settings.UPLOAD_MAX_FILE_BYTES / (1024 * 1024)
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=(
                    f"'{filename or 'unnamed'}' is {size} bytes, over the "
                    f"{limit_mib:g} MiB per-file limit."
                ),
            )

        uploads.append(
            IncomingImage(
                filename=filename or f"upload{suffix}",
                suffix=suffix,
                content_type=SUFFIX_CONTENT_TYPE[suffix],
                size=size,
                fileobj=part.file,
            )
        )
    return uploads


def _dispatch(task_name: str, project_id: uuid.UUID, *, unavailable_detail: str) -> str:
    """Hand a project's work to the broker, returning the task id.

    Raises 503 when the broker cannot be reached. That is the difference between
    this and the storage path: storing frames is durable and idempotent, so a
    broker outage must not fail it, while these endpoints' *entire* contract is
    "the work has been queued". Reporting success with no task would leave the
    client waiting forever on something nothing is going to pick up.
    """
    try:
        # Dispatched by name, not by importing the task: importing it would drag
        # torch and ultralytics into the API process, which does not install them.
        async_result = celery_app.send_task(task_name, args=[str(project_id)])
        return str(async_result.id)
    except Exception as exc:  # noqa: BLE001 - any broker/connection error is handled the same way
        logger.exception("Could not queue %s for project %s", task_name, project_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_detail,
        ) from exc


def _dispatch_processing(project_id: uuid.UUID) -> str:
    """Queue the analysis of a project's pending frames.

    The caller keeps the frames: they are ``pending`` and a later call to this
    endpoint queues them, so a 503 costs one retry, never a re-upload.
    """
    return _dispatch(
        PROCESS_IMAGES_TASK,
        project_id,
        unavailable_detail=(
            "The analysis queue is not reachable, so processing was not started. "
            "The uploaded frames are stored and still pending - retry once the "
            "broker is back."
        ),
    )


# --------------------------------------------------------------- project CRUD
@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
    description="Only users with the 'inspector' role may create projects.",
    responses={403: {"description": "Caller is not an Inspector"}},
)
def create_project(
    payload: ProjectCreate,
    current_user: CurrentUser,
    service: ProjectServiceDep,
) -> ProjectRead:
    return service.present(service.create(payload, current_user=current_user))


@router.get(
    "",
    response_model=Page[ProjectRead],
    summary="List visible projects",
    description="An Inspector's own projects; every project when the caller is an Admin.",
)
def list_projects(
    current_user: CurrentUser,
    service: ProjectServiceDep,
    pagination: PaginationDep,
) -> Page[ProjectRead]:
    projects, total = service.list(
        current_user=current_user,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    # The service attaches the frame/defect rollup to the whole page with two
    # grouped queries, so the dashboard grid needs no per-card requests.
    return Page[ProjectRead](
        items=service.present_many(projects),
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{project_id}",
    response_model=ProjectRead,
    summary="Get a project",
    description=(
        "Inspectors may read their own projects; Admins may read any project "
        "(read-only - mutations stay Inspector-scoped)."
    ),
    responses={404: {"description": "Project not found or not visible to the caller"}},
)
def get_project(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    service: ProjectServiceDep,
) -> ProjectRead:
    return service.present(service.get_visible(project_id, current_user=current_user))


@router.patch(
    "/{project_id}",
    response_model=ProjectRead,
    summary="Update a project",
    responses={
        404: {"description": "Project not found or not owned by the caller"},
        422: {"description": "Invalid main_photo_s3_path"},
    },
)
def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    current_user: CurrentUser,
    service: ProjectServiceDep,
) -> ProjectRead:
    return service.present(service.update(project_id, payload, current_user=current_user))


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project",
    responses={404: {"description": "Project not found or not owned by the caller"}},
)
def delete_project(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    service: ProjectServiceDep,
) -> None:
    service.delete(project_id, current_user=current_user)


# --------------------------------------------------------------------- images
@router.post(
    "/{project_id}/main-photo",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a project's cover photo",
    description=(
        "The image shown on the project card. A separate call rather than a "
        "part of `POST /projects`, because the object key is pinned to the "
        "project's own prefix (`projects/<id>/cover<ext>`) and that prefix only "
        "exists once the project row does. Re-uploading replaces the previous "
        "cover.\n\n"
        "This endpoint returns the object *key*; render the bytes with "
        "`GET /projects/{id}/main-photo`, which signs a short-lived URL."
    ),
    responses={
        404: {"description": "Project not found or not owned by the caller"},
        413: {"description": "The image is too large"},
        415: {"description": "Unsupported image type"},
    },
)
def upload_project_main_photo(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    file: Annotated[
        UploadFile,
        File(description="Cover photo (JPEG, PNG, TIFF or WebP)."),
    ],
) -> ProjectRead:
    # A single file goes through the same validation as a UAV batch, so the size
    # limit and the accepted types cannot drift between the two upload paths.
    upload = _validated_uploads([file])[0]
    return project_service.present(
        project_service.set_main_photo(project_id, upload, current_user=current_user)
    )


@router.get(
    "/{project_id}/main-photo",
    response_model=PresignedUrlRead,
    summary="Signed URL for a project's cover photo",
    description=(
        "Returns a short-lived URL for the bytes behind "
        "`main_photo_s3_path`. The image never travels through the API: the "
        "browser fetches it straight from the S3 endpoint. Callers should "
        "re-request the URL once `expires_in` has passed rather than caching it."
    ),
    responses={
        404: {
            "description": (
                "Project not found, not visible to the caller, or has no cover photo"
            )
        },
    },
)
def get_project_main_photo(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
) -> PresignedUrlRead:
    return project_service.main_photo_url(project_id, current_user=current_user)


@router.post(
    "/{project_id}/images",
    response_model=BulkImageUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a chunk of UAV images to a project",
    description=(
        "Stores drone frames in object storage and registers them as `pending`. "
        "The caller must own the project.\n\n"
        "**Nothing is queued here.** Analysis starts when the client calls "
        "`POST /projects/{id}/start-processing`, which is deliberately a "
        "separate step: a full sortie arrives as many chunked requests, and "
        "dispatching per chunk would queue work for frames the following chunks "
        "have not delivered yet. Up to "
        f"{settings.UPLOAD_MAX_FILES_PER_REQUEST} files per request; a request "
        "is one transaction, so a failure stores nothing from that chunk."
    ),
    responses={
        404: {"description": "Project not found or not owned by the caller"},
        413: {"description": "Too many files, or a file is too large"},
        415: {"description": "Unsupported image type"},
    },
)
def upload_project_images(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    image_service: ImageServiceDep,
    files: Annotated[
        list[UploadFile],
        File(description="UAV frames (JPEG, PNG, TIFF or WebP), repeated per file."),
    ],
) -> BulkImageUploadResponse:
    # Ownership is enforced here; another inspector's project is invisible (404).
    project = project_service.get(project_id, current_user=current_user)
    uploads = _validated_uploads(files)

    images = image_service.store_uploads(project, uploads)
    return BulkImageUploadResponse(uploaded=len(images), images=images)


@router.post(
    "/{project_id}/start-processing",
    response_model=ProcessingStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a project's pending frames for analysis",
    description=(
        "Starts YOLO detection (and the 3D model, when the project asks for "
        "one) over every `pending` frame. The caller must own the project.\n\n"
        "Called once after the last upload chunk. Calling it again is harmless: "
        "the task claims whatever is still pending and returns early when "
        "nothing is. A 503 means the broker was unreachable and **nothing was "
        "queued** - the frames are still stored and pending, so retry this call "
        "rather than re-uploading."
    ),
    responses={
        404: {"description": "Project not found or not owned by the caller"},
        503: {"description": "The Celery broker is unreachable; nothing was queued"},
    },
)
def start_project_processing(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    image_service: ImageServiceDep,
) -> ProcessingStartResponse:
    # `resume_processing` fetches owner-scoped, not `get_visible`: queueing work
    # is a mutation of the project's pipeline, and an Admin (who may read any
    # project) does not own campaigns.
    #
    # It also clears a stop flag *before* the task is queued, which is what makes
    # this endpoint double as "resume": a worker that saw the flag would exit
    # without claiming a single frame (see `app.tasks.image_processing`).
    project_service.resume_processing(project_id, current_user=current_user)

    # Informational: what the worker will pick up when it gets there. A race with
    # a worker that claims the frames first only changes this number, never the
    # task's correctness.
    pending = image_service.status_breakdown(project_id).get(ImageStatus.PENDING.value, 0)
    task_id = _dispatch_processing(project_id)

    return ProcessingStartResponse(task_id=task_id, pending_images=pending)


@router.post(
    "/{project_id}/stop-processing",
    response_model=ProjectRead,
    summary="Stop a project's analysis",
    description=(
        "Asks the worker to stop between frames, and keeps a queued task from "
        "claiming anything. Frames already analysed keep their results, the "
        "frames that were claimed but never reached go back to `pending`, and "
        "the 3D step is skipped - or an in-flight reconstruction is cancelled.\n\n"
        "Nothing is deleted, so `POST /projects/{id}/start-processing` resumes "
        "from exactly where this left off. Idempotent."
    ),
    responses={404: {"description": "Project not found or not owned by the caller"}},
)
def stop_project_processing(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
) -> ProjectRead:
    return project_service.present(
        project_service.stop_processing(project_id, current_user=current_user)
    )


@router.get(
    "/{project_id}/images/stats",
    response_model=ImageStatsRead,
    summary="Status breakdown of a project's images",
    description=(
        "Counts per pipeline status, in one query. The wizard's progress screen "
        "polls this every few seconds, so it must not have to page through every "
        "frame row to add them up."
    ),
    responses={404: {"description": "Project not found or not visible to the caller"}},
)
def get_project_image_stats(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    image_service: ImageServiceDep,
) -> ImageStatsRead:
    project_service.get_visible(project_id, current_user=current_user)
    # Only statuses that occur are returned, so every field is filled in here -
    # the UI renders a stable set of counters regardless of how far the batch got.
    breakdown = image_service.status_breakdown(project_id)
    return ImageStatsRead(
        total=sum(breakdown.values()),
        pending=breakdown.get(ImageStatus.PENDING.value, 0),
        processing=breakdown.get(ImageStatus.PROCESSING.value, 0),
        completed=breakdown.get(ImageStatus.COMPLETED.value, 0),
        failed=breakdown.get(ImageStatus.FAILED.value, 0),
    )


@router.get(
    "/{project_id}/frames",
    response_model=Page[ReviewFrameRead],
    summary="List a project's frames with review URLs",
    description=(
        "Every uploaded frame, in upload order, each with a short-lived signed "
        "URL - including the frames the model found nothing on. The review "
        "workspace pages through the whole sortie, and a photo without a "
        "detection is a result too, so it cannot be left out of the carousel.\n\n"
        "Separate from `GET /projects/{id}/images`, which stays URL-free on "
        "purpose: the progress screen polls that listing every few seconds, and "
        "signing a URL per row on every tick would be wasted work. Read this "
        "one once per review session."
    ),
    responses={404: {"description": "Project not found or not visible to the caller"}},
)
def list_project_frames(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    image_service: ImageServiceDep,
    pagination: PaginationDep,
) -> Page[ReviewFrameRead]:
    project_service.get_visible(project_id, current_user=current_user)
    images, total = image_service.list_by_project(
        project_id,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return Page[ReviewFrameRead](
        items=image_service.present_frames(images),
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{project_id}/images",
    response_model=Page[InspectionImageRead],
    summary="List a project's images",
    responses={404: {"description": "Project not found or not visible to the caller"}},
)
def list_project_images(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    image_service: ImageServiceDep,
    pagination: PaginationDep,
) -> Page[InspectionImageRead]:
    """Lets the UI follow a batch through pending -> processing -> completed."""
    project_service.get_visible(project_id, current_user=current_user)
    images, total = image_service.list_by_project(
        project_id,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return Page[InspectionImageRead](
        items=images,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


# -------------------------------------------------------------------- defects
@router.post(
    "/{project_id}/images/{image_id}/defects",
    response_model=DefectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Draw a defect mask on a frame",
    description=(
        "Stores a mask an inspector drew over one frame, for a defect the model "
        "missed entirely. The payload is the class and the polygon in "
        "normalized [0, 1] image coordinates; `confidence_score` and "
        "`is_verified` are set server-side (a hand-drawn mask is the review).\n\n"
        "Owner-scoped: an Admin may read a project but does not annotate "
        "somebody else's inspection."
    ),
    responses={
        404: {"description": "Project or frame not found, or not owned by the caller"},
        422: {"description": "A ring with fewer than 3 points, or a point outside [0, 1]"},
    },
)
def create_frame_defect(
    project_id: uuid.UUID,
    image_id: uuid.UUID,
    payload: DefectCreate,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    defect_service: DefectServiceDep,
) -> DefectRead:
    # `get`, not `get_visible`: drawing a mask is a mutation of the review record.
    project_service.get(project_id, current_user=current_user)
    defect = defect_service.create(project_id, image_id, payload)
    return defect_service.present(defect)


@router.get(
    "/{project_id}/defects",
    response_model=Page[DefectRead],
    summary="List a project's defects",
    description=(
        "Every detected instance in the project, each with its class, its mask "
        "polygon in normalized [0, 1] image coordinates, the verification flag "
        "and a short-lived signed URL for the frame it was found on.\n\n"
        "The workspace draws the polygon over the frame, so the two have to "
        "arrive together: one request per card would make the review screen "
        "N+1 requests. Signed URLs are shared between defects that sit on the "
        "same frame."
    ),
    responses={404: {"description": "Project not found or not visible to the caller"}},
)
def list_project_defects(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    defect_service: DefectServiceDep,
    pagination: PaginationDep,
) -> Page[DefectRead]:
    project_service.get_visible(project_id, current_user=current_user)
    defects, total = defect_service.list_by_project(
        project_id,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return Page[DefectRead](
        items=defect_service.present_many(defects),
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.patch(
    "/{project_id}/defects/{defect_id}",
    response_model=DefectRead,
    summary="Confirm, clear or re-shape a defect",
    description=(
        "The human-in-the-loop step, in two parts. `is_verified=true` records "
        "that an inspector confirmed the detection and `false` returns it to "
        "the review queue; `mask_polygon` replaces the stored geometry, which "
        "is how the eraser persists a mask it rubbed part of. A mask emptied "
        "completely is deleted rather than sent here, since an empty polygon "
        "is not a shape the app can draw. Omitting both fields is a 422.\n\n"
        "Owner-scoped like every other mutation - an Admin may read a project "
        "but does not sign off somebody else's inspection."
    ),
    responses={
        404: {"description": "Project or defect not found, or not owned by the caller"},
    },
)
def update_defect(
    project_id: uuid.UUID,
    defect_id: uuid.UUID,
    payload: DefectUpdate,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    defect_service: DefectServiceDep,
) -> DefectRead:
    # `get`, not `get_visible`: editing is a mutation of the review record.
    project_service.get(project_id, current_user=current_user)
    defect = defect_service.update(project_id, defect_id, payload)
    return defect_service.present(defect)


@router.delete(
    "/{project_id}/defects/{defect_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a defect",
    description=(
        "Removes one detection - what the annotation tools' delete mode calls. "
        "Used for a mask that does not belong (a false positive, or a hand-drawn "
        "shape that was a mistake). A 3D localization of the same defect is "
        "removed with it by the foreign key's cascade.\n\n"
        "Owner-scoped like the other mutations."
    ),
    responses={
        404: {"description": "Project or defect not found, or not owned by the caller"},
    },
)
def delete_defect(
    project_id: uuid.UUID,
    defect_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    defect_service: DefectServiceDep,
) -> None:
    project_service.get(project_id, current_user=current_user)
    defect_service.delete(project_id, defect_id)


# -------------------------------------------------------------------- 3D model
@router.get(
    "/{project_id}/model3d",
    response_model=Model3DRead,
    summary="Get a project's active 3D model",
    description=(
        "Metadata for the newest reconstruction plus a short-lived signed URL "
        "for the `.glb` in object storage, so the browser can load the mesh "
        "without any storage credential.\n\n"
        "404 means *no model is available yet* - either the project never asked "
        "for one (`needs_3d_model` is false) or the reconstruction has not "
        "finished. The detail message says which; the workspace falls back to "
        "the 2D viewer either way.\n\n"
        "While a build is running the useful number is not here but on the "
        "project: `GET /projects/{id}` carries `model3d_progress`, the worker's "
        "last report of how far NodeODM has got, which is what the page draws "
        "its progress bar from."
    ),
    responses={
        404: {
            "description": (
                "Project not visible to the caller, no model produced yet, or "
                "3D reconstruction was not requested"
            )
        },
    },
)
def get_project_model3d(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    model3d_service: Model3DServiceDep,
) -> Model3DRead:
    project = project_service.get_visible(project_id, current_user=current_user)
    model = model3d_service.latest_for_project(project_id)
    if model is None:
        # Four distinct reasons to have no model, and the detail says which: a
        # client that only reads this endpoint has no other way to tell "wait for
        # it" from "start one" from "this project never wanted one".
        if not project.needs_3d_model:
            raise NotFoundError("This project does not request a 3D reconstruction.")
        if project.model3d_requested_at is not None:
            raise NotFoundError("The 3D reconstruction is still running for this project.")
        if project.model3d_error:
            raise NotFoundError(project.model3d_error)
        raise NotFoundError(
            "No 3D model has been built for this project yet. Queue one with "
            "`POST /projects/{id}/model3d`."
        )
    return model3d_service.present(model)


@router.post(
    "/{project_id}/model3d",
    response_model=ProjectRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Build (or rebuild) a project's 3D model",
    description=(
        "Queues a NodeODM reconstruction of the project's stored frames. The "
        "caller must own the project, and the project must request a 3D model "
        "(`needs_3d_model`).\n\n"
        "This is the way back from a reconstruction that never finished: the "
        "main pipeline builds the model at the end of an analysis run, so once "
        "a project's frames are all `completed` there is nothing left to start "
        "- a worker restarted mid-reconstruction used to leave such a project "
        "permanently without its model. Frames are read, never modified: no "
        "status changes, and the defect results are untouched.\n\n"
        "A run takes tens of minutes, so the response is the project with "
        "`model3d_requested_at` set and `model3d_progress` null - poll "
        "`GET /projects/{id}/model3d` until it answers 200, and read "
        "`model3d_progress` off the project meanwhile to show how far it has "
        "got. Calling it again while a build is in flight is harmless and "
        "queues nothing."
    ),
    responses={
        404: {"description": "Project not found or not owned by the caller"},
        422: {
            "description": (
                "The project does not request a 3D model, or has fewer than "
                "NODEODM_MIN_IMAGES frames"
            )
        },
        503: {"description": "The Celery broker is unreachable; nothing was queued"},
    },
)
def build_project_model3d(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
) -> ProjectRead:
    # `get`, not `get_visible`: queueing a reconstruction is a mutation of the
    # project's pipeline, and an Admin does not own inspection campaigns - the
    # same rule every other mutation here follows.
    project, queued = project_service.request_model3d(
        project_id, current_user=current_user
    )
    if not queued:
        # Already in flight. Adoption rather than conflict: the caller's intent
        # ("this project should have a model") is already being served, and a 409
        # would only make the client special-case a state it does not care about.
        return project_service.present(project)

    try:
        task_id = _dispatch(
            BUILD_MODEL3D_TASK,
            project_id,
            unavailable_detail=(
                "The analysis queue is not reachable, so the reconstruction was "
                "not queued. The frames are stored and unchanged - retry once "
                "the broker is back."
            ),
        )
    except HTTPException:
        # The flag was set before the dispatch so the task could not finish
        # before it existed; an unreachable broker therefore has to take it down
        # again, or the 3D page would report a build that is not running.
        project_service.finish_model3d(
            project_id,
            error="The reconstruction could not be queued: the analysis queue is unreachable.",
        )
        raise

    logger.info("Queued 3D reconstruction %s for project %s", task_id, project_id)
    return project_service.present(project)


@router.post(
    "/{project_id}/model3d/mesh",
    response_model=Model3DRead,
    status_code=status.HTTP_201_CREATED,
    summary="Store a 3D model built in the browser",
    description=(
        "Stores the `.glb` a browser-side reconstruction just produced and "
        "registers it as the project's active model.\n\n"
        "The 3D page drives NodeODM itself (a worker-side build has a deadline "
        "no full sortie fits into), so the finished mesh exists in that tab and "
        "nowhere else. This is what puts it in object storage: without it a "
        "reload loses the reconstruction, and the page offers to build it "
        "again.\n\n"
        "The object key is derived from the project id, so a client cannot name "
        "an arbitrary object; a second upload replaces the project's mesh. The "
        "response is the registered model with a fresh signed URL, so the "
        "caller can switch from its local copy to the stored one."
    ),
    responses={
        404: {"description": "Project not found or not owned by the caller"},
        413: {"description": "The mesh is larger than the accepted limit"},
        415: {"description": "The part is not a .glb"},
        422: {
            "description": (
                "The project does not request a 3D model, or the file is empty"
            )
        },
    },
)
def upload_project_model3d(
    project_id: uuid.UUID,
    current_user: CurrentUser,
    project_service: ProjectServiceDep,
    model3d_service: Model3DServiceDep,
    file: Annotated[
        UploadFile,
        File(description="Textured reconstruction (binary glTF, `.glb`)."),
    ],
) -> Model3DRead:
    # `get`, not `get_visible`: storing a mesh is a mutation of the project's 3D
    # record, so it follows the same owner-scoped rule as every other mutation
    # here.
    project = project_service.get(project_id, current_user=current_user)
    if not project.needs_3d_model:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "This project does not request a 3D model, so it has no "
                "reconstruction to store."
            ),
        )

    _validated_mesh(file)
    model = model3d_service.store_mesh(project_id, file.file)

    # A stored mesh is the successful end of the project's build state: this
    # takes down the flag, the percentage and the error left by an earlier
    # attempt, so the project stops describing a failed reconstruction it now
    # has the result of.
    project_service.finish_model3d(project_id, error=None)
    logger.info("Stored 3D model %s for project %s", model.id, project_id)
    return model3d_service.present(model)
