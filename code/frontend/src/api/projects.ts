/**
 * Project endpoints, including the UAV upload used by wizard step 2.
 *
 * Note the object-storage boundary: the API returns *object keys*
 * (`main_photo_s3_path`, `InspectionImage.s3_path`), not URLs. Presigned URLs
 * are requested from the API when a screen needs the bytes, because they expire
 * - so no listing here returns something directly renderable in an `<img>`.
 */

import { apiRequest, apiRequestAllPages } from './client'
import type {
  BulkImageUploadResponse,
  Defect,
  InspectionImage,
  Page,
  PaginationQuery,
  PresignedUrl,
  ProcessingStartResponse,
  Project,
  ProjectCreatePayload,
  ProjectImageStats,
  ProjectModel3D,
  ProjectUpdatePayload,
  ReviewFrame,
} from '@/types/api'

// -------------------------------------------------------------------- CRUD
/**
 * `GET /projects` - an Inspector's own projects, every project for an Admin,
 * newest first.
 */
export function listProjects(
  query: PaginationQuery = {},
  signal?: AbortSignal,
): Promise<Page<Project>> {
  return apiRequest<Page<Project>>('/projects', { query: { ...query }, signal })
}

/** Every visible project, following the pagination to the last page. */
export function listAllProjects(signal?: AbortSignal): Promise<Project[]> {
  return apiRequestAllPages<Project>('/projects', {}, { signal })
}

/** `GET /projects/{id}` - Admins may open any project (read-only). */
export function getProject(projectId: string, signal?: AbortSignal): Promise<Project> {
  return apiRequest<Project>(`/projects/${projectId}`, { signal })
}

/** `POST /projects` - wizard step 1. Inspectors only; the owner is the caller. */
export function createProject(payload: ProjectCreatePayload): Promise<Project> {
  return apiRequest<Project>('/projects', { method: 'POST', body: payload })
}

/**
 * `PATCH /projects/{id}` - owner-scoped.
 *
 * `main_photo_s3_path` must be a key under `projects/<id>/`; the service rejects
 * anything else, which is what keeps a project from pointing at another
 * project's photo.
 */
export function updateProject(projectId: string, payload: ProjectUpdatePayload): Promise<Project> {
  return apiRequest<Project>(`/projects/${projectId}`, { method: 'PATCH', body: payload })
}

export function deleteProject(projectId: string): Promise<void> {
  return apiRequest<void>(`/projects/${projectId}`, { method: 'DELETE' })
}

// ------------------------------------------------------------- cover photo
/**
 * `POST /projects/{id}/main-photo` - the card image, uploaded by wizard step 1.
 *
 * A second request after `createProject`, not a field of it: the object key is
 * pinned to the project's own prefix (`projects/<id>/cover<ext>`), and that
 * prefix only exists once the project row does. Re-uploading replaces the
 * previous cover. The part name is `file` (singular) because the endpoint binds
 * a single `UploadFile`.
 *
 * The response is the updated project, so the caller gets the new
 * `main_photo_s3_path` from the server rather than assuming a key.
 */
export function uploadProjectMainPhoto(
  projectId: string,
  file: File,
  signal?: AbortSignal,
): Promise<Project> {
  const form = new FormData()
  form.append('file', file, file.name)
  return apiRequest<Project>(`/projects/${projectId}/main-photo`, {
    method: 'POST',
    form,
    signal,
  })
}

/**
 * `GET /projects/{id}/main-photo` - a short-lived URL for the cover photo.
 *
 * Requested per render rather than stored: the API keeps only the object key,
 * and the URL expires. A 404 means the project has no cover yet (or is not
 * visible to the caller), which is the cue to render a placeholder instead.
 */
export function getProjectMainPhotoUrl(
  projectId: string,
  signal?: AbortSignal,
): Promise<PresignedUrl> {
  return apiRequest<PresignedUrl>(`/projects/${projectId}/main-photo`, { signal })
}

// ------------------------------------------------------------------ images
/** `GET /projects/{id}/images` - drives the progress screen. */
export function listProjectImages(
  projectId: string,
  query: PaginationQuery = {},
  signal?: AbortSignal,
): Promise<Page<InspectionImage>> {
  return apiRequest<Page<InspectionImage>>(`/projects/${projectId}/images`, {
    query: { ...query },
    signal,
  })
}

/** Limits enforced by the API; mirrored here so the wizard can warn up front. */
export const MAX_FILES_PER_UPLOAD = 100
export const MAX_FILE_BYTES = 25 * 1024 * 1024
export const ACCEPTED_IMAGE_TYPES = 'image/jpeg,image/png,image/tiff,image/webp'

/**
 * `GET /projects/{id}/frames` - every frame with a signed URL, for the reviewer.
 *
 * A different endpoint from `listProjectImages` on purpose: that listing is
 * polled by the progress screen and carries object keys only, while signing a
 * URL per frame on every poll tick would be wasted work. This one is read once
 * per review session, and it includes frames with no detections - the carousel
 * pages through the whole sortie, not only the photos the model fired on.
 */
export function listAllProjectFrames(
  projectId: string,
  signal?: AbortSignal,
): Promise<ReviewFrame[]> {
  return apiRequestAllPages<ReviewFrame>(`/projects/${projectId}/frames`, {}, { signal })
}

/**
 * `GET /projects/{id}/images/stats` - the progress screen's counters.
 *
 * One query per tick instead of paging the whole collection: a project can hold
 * thousands of frames, and the wizard refreshes this every few seconds.
 */
export function getProjectImageStats(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectImageStats> {
  return apiRequest<ProjectImageStats>(`/projects/${projectId}/images/stats`, { signal })
}

/**
 * `POST /projects/{id}/images` - one chunk of wizard step 2.
 *
 * The multipart field name is `files` on every part (repeated), which is what
 * `list[UploadFile]` binds to in FastAPI. The request is one transaction: a
 * failure stores none of that chunk, so a retry re-sends exactly the chunk that
 * failed.
 *
 * **This call queues nothing.** Storage and analysis are separate so that a
 * 400-frame sortie can arrive as many chunks; the caller counts `uploaded` and
 * calls `startProjectProcessing()` once, after the last chunk. Chunking itself
 * lives in `utils/upload.ts`.
 */
export function uploadProjectImages(
  projectId: string,
  files: readonly File[],
  signal?: AbortSignal,
): Promise<BulkImageUploadResponse> {
  const form = new FormData()
  for (const file of files) form.append('files', file, file.name)
  return apiRequest<BulkImageUploadResponse>(`/projects/${projectId}/images`, {
    method: 'POST',
    form,
    signal,
  })
}

// ------------------------------------------------------------------ defects
/**
 * `GET /projects/{id}/defects` - the review list, one page at a time.
 *
 * Each defect carries its mask polygon *and* a freshly signed URL for the frame
 * it was found on, so a card needs no second request to draw itself. The URLs
 * expire, so a screen that stays open for a long time should re-read the list.
 */
export function listProjectDefects(
  projectId: string,
  query: PaginationQuery = {},
  signal?: AbortSignal,
): Promise<Page<Defect>> {
  return apiRequest<Page<Defect>>(`/projects/${projectId}/defects`, {
    query: { ...query },
    signal,
  })
}

/** Every defect in a project, following the pagination to the last page. */
export function listAllProjectDefects(
  projectId: string,
  signal?: AbortSignal,
): Promise<Defect[]> {
  return apiRequestAllPages<Defect>(`/projects/${projectId}/defects`, {}, { signal })
}

/**
 * `PATCH /projects/{id}/defects/{defectId}` - the human-in-the-loop step.
 *
 * `true` records that an inspector confirmed the detection, `false` returns it
 * to the review queue. Returns the updated defect, so the caller can replace
 * its copy instead of guessing at the new state.
 */
export function setDefectVerified(
  projectId: string,
  defectId: string,
  isVerified: boolean,
): Promise<Defect> {
  return apiRequest<Defect>(`/projects/${projectId}/defects/${defectId}`, {
    method: 'PATCH',
    body: { is_verified: isVerified },
  })
}

/**
 * `DELETE /projects/{id}/defects/{defectId}` - drop a detection or a drawn mask.
 *
 * The delete mode of the annotation tools: a false positive, or a shape the
 * inspector drew by mistake, leaves the record entirely rather than being kept
 * as a hidden row that would still count towards the project's statistics.
 */
export function deleteDefect(projectId: string, defectId: string): Promise<void> {
  return apiRequest<void>(`/projects/${projectId}/defects/${defectId}`, { method: 'DELETE' })
}

// ------------------------------------------------------------- 3D model
/**
 * `GET /projects/{id}/model3d` - the newest reconstruction plus a signed URL.
 *
 * A 404 is the normal "nothing to show" answer (no model produced yet, or the
 * project did not ask for one) - render the 2D viewer, do not treat it as an
 * error. The URL expires: pass it straight to the loader, do not store it.
 */
export function getProjectModel3D(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectModel3D> {
  return apiRequest<ProjectModel3D>(`/projects/${projectId}/model3d`, { signal })
}

/**
 * `POST /projects/{id}/model3d` - queue a reconstruction of the stored frames.
 *
 * The only way to get a model for a project whose frames are already analysed:
 * the pipeline builds the mesh at the end of an analysis run, so a project whose
 * reconstruction was interrupted (a worker restart mid-run) has nothing pending
 * and `start-processing` returns without reaching NodeODM.
 *
 * Returns the project with `model3d_requested_at` set, so the caller adopts the
 * server's state instead of guessing at it. Calling it while a build is already
 * in flight is harmless: it queues nothing and returns the same project.
 */
export function buildProjectModel3D(
  projectId: string,
  signal?: AbortSignal,
): Promise<Project> {
  return apiRequest<Project>(`/projects/${projectId}/model3d`, {
    method: 'POST',
    signal,
  })
}

/**
 * `POST /projects/{id}/model3d/mesh` - store the mesh this page just rebuilt.
 *
 * The 3D page runs photogrammetry itself, so the finished `.glb` exists in the
 * tab's memory and nowhere else until it is handed over here. Storing it is
 * what makes the model outlive the page: without it a reload finds no model and
 * offers to reconstruct the building again.
 *
 * Multipart with a single `file` part, named for the asset ODM wrote - the
 * server derives the object key from the project id and ignores this name, so
 * it is documentation rather than a claim. The response is the registered
 * model with a fresh signed URL.
 */
export function uploadProjectModel3D(
  projectId: string,
  glb: Blob,
  signal?: AbortSignal,
): Promise<ProjectModel3D> {
  const form = new FormData()
  form.append('file', glb, 'odm_textured_model_geo.glb')
  return apiRequest<ProjectModel3D>(`/projects/${projectId}/model3d/mesh`, {
    method: 'POST',
    form,
    signal,
  })
}

/**
 * `POST /projects/{id}/start-processing` - queue every pending frame.
 *
 * Called once, after the last chunk. Idempotent from the caller's point of
 * view: a second call claims whatever is still pending and the task returns
 * early when there is nothing. A 503 means the broker was unreachable and
 * nothing was queued - retry this call; the frames are already stored.
 *
 * This is also how a stopped project is **resumed**: the endpoint clears the
 * stop flag before queueing, so the task it dispatches claims the frames the
 * stopped run handed back. There is deliberately no separate resume endpoint -
 * one path queues work, so there is one place to get that order right.
 */
export function startProjectProcessing(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProcessingStartResponse> {
  return apiRequest<ProcessingStartResponse>(`/projects/${projectId}/start-processing`, {
    method: 'POST',
    signal,
  })
}

/**
 * `POST /projects/{id}/stop-processing` - ask the worker to stop between frames.
 *
 * Returns the updated project, so the badge is redrawn from the server's answer
 * rather than from a guess. Everything already analysed is kept, and the frames
 * the run never reached go back to `pending` - which `startProjectProcessing`
 * then picks up, because resuming is starting again.
 */
export function stopProjectProcessing(projectId: string): Promise<Project> {
  return apiRequest<Project>(`/projects/${projectId}/stop-processing`, { method: 'POST' })
}
