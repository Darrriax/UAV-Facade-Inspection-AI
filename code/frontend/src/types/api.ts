/**
 * TypeScript mirror of the FastAPI schemas (`app/schemas/*.py`).
 *
 * Hand-written rather than generated: the surface is small, and the generated
 * client would have to be regenerated (and reviewed) on every backend change
 * that the frontend does not consume yet. Keep this file in sync with
 * `app/schemas/` - it is the only place the wire format is described.
 *
 * Enums are plain string-literal maps rather than TS `enum`s: the API exchanges
 * lowercase values (`"inspector"`, `"pending"`), and this way a value from the
 * API can be compared to a literal without a cast, while the map still gives
 * one canonical list to iterate over in the UI.
 */

// ------------------------------------------------------------------ primitives
/** Envelope for every paginated collection endpoint. */
export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

/** Offset/limit query parameters shared by the listing endpoints. */
export interface PaginationQuery {
  offset?: number
  limit?: number
}

export type ISODateString = string

/**
 * A short-lived URL for one stored object, `expires_in` seconds from signing.
 *
 * The API returns object *keys* everywhere else; bytes are fetched straight
 * from the S3 endpoint with a URL from an endpoint like
 * `GET /projects/{id}/main-photo`, so a screen must re-request the URL rather
 * than caching it.
 */
export interface PresignedUrl {
  url: string
  expires_in: number
}

// ----------------------------------------------------------------------- roles
export const USER_ROLES = {
  ADMIN: 'admin',
  INSPECTOR: 'inspector',
} as const

export type UserRole = (typeof USER_ROLES)[keyof typeof USER_ROLES]

// ------------------------------------------------------------------ user/auth
export interface User {
  id: string
  email: string
  role: UserRole
  /** False on a deactivated account: it can no longer authenticate. */
  is_active: boolean
  first_name: string | null
  last_name: string | null
  /** ISO 3166-1 alpha-2, upper-case. */
  country: string | null
  age: number | null
  phone_number: string | null
  /** Object key inside the images bucket; render it through a presigned URL. */
  profile_photo_s3_path: string | null
  created_at: ISODateString
  updated_at: ISODateString
}

export interface LoginPayload {
  email: string
  password: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
  /** Access token lifetime in seconds. */
  expires_in: number
}

/** Fields the owner of an account may edit on their own profile. */
export interface ProfileUpdatePayload {
  first_name?: string | null
  last_name?: string | null
  country?: string | null
  age?: number | null
  phone_number?: string | null
  profile_photo_s3_path?: string | null
  password?: string
}

/** `PATCH /users/me` is the self-service profile contract. */
export type MyProfileUpdatePayload = ProfileUpdatePayload

/** Admin-only account contract: adds the account-level attributes. */
export interface UserUpdatePayload extends ProfileUpdatePayload {
  email?: string
  role?: UserRole
  is_active?: boolean
}

/** Admin-only creation contract (`POST /users`). */
export interface UserCreatePayload {
  email: string
  password: string
  role?: UserRole
  first_name?: string | null
  last_name?: string | null
  country?: string | null
  age?: number | null
  phone_number?: string | null
}

export interface UserListQuery extends PaginationQuery {
  /** The Admin dashboard lists Inspectors only. */
  role?: UserRole
}

// ------------------------------------------------------------------- projects
/**
 * Project badge state, rolled up server-side from the frame statuses.
 *
 * Derived, not stored: no column holds it, because it changes from the Celery
 * worker rather than from an API request. Precedence is
 * draft -> paused -> processing -> uploading -> failed -> completed; `stats`
 * carries the detail behind the badge (a `completed` project can still have
 * failed frames). `paused` comes from `processing_stopped_at`, the one input the
 * rollup takes from a column rather than from the frames.
 */
export const PROJECT_STATUSES = {
  DRAFT: 'draft',
  UPLOADING: 'uploading',
  PROCESSING: 'processing',
  /** Stopped by the user with frames still waiting, so it can be resumed. */
  PAUSED: 'paused',
  COMPLETED: 'completed',
  FAILED: 'failed',
} as const

export type ProjectStatus = (typeof PROJECT_STATUSES)[keyof typeof PROJECT_STATUSES]

/** Frame and defect rollup shown on the project cards. */
export interface ProjectStats {
  total_images: number
  pending_images: number
  processing_images: number
  completed_images: number
  failed_images: number
  /** Objects across the project's frames, not detection rows. */
  total_defects: number
  /**
   * Objects the reviewer confirmed, counting only whole objects.
   *
   * Server-computed so a screen with no defect rows loaded - the reconstruction
   * viewer - can print the same totals the workspace's header does, instead of
   * fetching every detection in the project for one subtitle.
   */
  verified_defects: number
}

export interface Project {
  id: string
  user_id: string
  name: string
  location: string | null
  description: string | null
  main_photo_s3_path: string | null
  /** Whether a .glb reconstruction is expected for this project. */
  needs_3d_model: boolean
  /**
   * Whether frames are analysed for defects.
   *
   * `false` is a 3D-only project: processing marks its frames analysed without
   * ever running the detector, so no frame carries a mask. It is a property of
   * the project rather than of one run, because it changes what the whole
   * project produces - and "no defects" has to stay distinguishable from "the
   * detector never looked".
   */
  detect_defects: boolean
  created_at: ISODateString
  updated_at: ISODateString
  /** Counters for the card footer, aggregated in one query per page. */
  stats: ProjectStats
  status: ProjectStatus
  /**
   * When the user stopped this project's analysis, or null while it may run.
   *
   * Distinct from `status`: a stop also discards nothing, so a stopped project
   * still reports its counters, and `start-processing` clears this again.
   */
  processing_stopped_at: ISODateString | null
  /**
   * When a 3D reconstruction was queued, or null when none is in flight.
   *
   * A reconstruction runs in the worker for tens of minutes, so this is the only
   * way the 3D page can tell a build that is genuinely running from one that was
   * never started - and therefore whether pressing "build" would start work or
   * duplicate it. The worker clears it when the build finishes.
   */
  model3d_requested_at: ISODateString | null
  /**
   * Why the last reconstruction produced no model, or null when it succeeded.
   *
   * `GET /projects/{id}/model3d` answers 404 both while a build is running and
   * after one that failed; this is what tells the two apart.
   */
  model3d_error: string | null
  /**
   * How far the running reconstruction has got, 0-100, or null while none runs.
   *
   * The worker's own report of NodeODM's percentage, refreshed with the rest of
   * the project - so the 3D page can show a bar that moves instead of an
   * animation that only pretends to. Null with `model3d_requested_at` set means
   * the build has not reported its first percentage yet (queued, or still
   * fetching frames), which the page renders as an indeterminate bar rather
   * than as 0%.
   */
  model3d_progress: number | null
}

export interface ProjectCreatePayload {
  name: string
  location?: string | null
  description?: string | null
  needs_3d_model?: boolean
  detect_defects?: boolean
}

export interface ProjectUpdatePayload {
  name?: string
  location?: string | null
  description?: string | null
  main_photo_s3_path?: string | null
  needs_3d_model?: boolean
  detect_defects?: boolean
}

// ---------------------------------------------------------- images & pipeline
export const IMAGE_STATUSES = {
  PENDING: 'pending',
  PROCESSING: 'processing',
  COMPLETED: 'completed',
  FAILED: 'failed',
} as const

export type ImageStatus = (typeof IMAGE_STATUSES)[keyof typeof IMAGE_STATUSES]

/**
 * The four statuses in pipeline order.
 *
 * One canonical order, so the progress screen's counters, its legend and the
 * per-frame chips cannot disagree about which status comes first.
 */
export const IMAGE_STATUS_ORDER: readonly ImageStatus[] = [
  IMAGE_STATUSES.PENDING,
  IMAGE_STATUSES.PROCESSING,
  IMAGE_STATUSES.COMPLETED,
  IMAGE_STATUSES.FAILED,
]

export interface InspectionImage {
  id: string
  project_id: string
  /** Original filename, kept for traceability. */
  file_path: string
  s3_path: string | null
  status: ImageStatus
  created_at: ISODateString
  updated_at: ISODateString
}

/**
 * `POST /projects/{id}/images` stores one chunk of frames and queues nothing.
 *
 * A full sortie arrives as many chunked requests, so dispatching per chunk
 * would queue analysis for frames the following chunks have not delivered yet.
 * The client accumulates `uploaded` across chunks and calls
 * `POST /projects/{id}/start-processing` once, at the end.
 */
export interface BulkImageUploadResponse {
  uploaded: number
  images: InspectionImage[]
}

/**
 * `POST /projects/{id}/start-processing` - analysis has been queued.
 *
 * `pending_images` is what the worker will find (0 when an earlier run already
 * claimed the frames, which makes a repeated call a no-op rather than an
 * error). A 503 from this call means the broker was unreachable and **nothing**
 * was queued: the frames are still stored and pending, so the fix is to retry
 * this call, never to re-upload.
 */
export interface ProcessingStartResponse {
  task_id: string
  pending_images: number
}

/**
 * `GET /projects/{id}/frames` - every frame, with a signed URL for its bytes.
 *
 * The review carousel is the only consumer. It is separate from
 * `InspectionImage` because that listing is polled by the progress screen and
 * stays URL-free (a signature per row per tick would be wasted work), and
 * because the carousel must hold frames the model found *nothing* on: a photo
 * with no detection is a result too, and it has no `Defect` row to hang off.
 */
export interface ReviewFrame {
  id: string
  project_id: string
  file_path: string
  status: ImageStatus
  /** Signed URL of the frame; null when the row has no stored object key. */
  image_url: string | null
  /** Lifetime of `image_url` in seconds from the moment it was signed. */
  image_url_expires_in: number
}

/**
 * `GET /projects/{id}/images/stats` - one row per status, zeros included.
 *
 * What the progress screen polls: the alternative is paging through every
 * frame and adding them up client-side, on every tick.
 */
export interface ProjectImageStats {
  total: number
  pending: number
  processing: number
  completed: number
  failed: number
}

// -------------------------------------------------------------------- defects
export const DEFECT_CLASSES = {
  CRACK: 'crack',
  SPALLING: 'spalling',
} as const

export type DefectClass = (typeof DEFECT_CLASSES)[keyof typeof DEFECT_CLASSES]

/** One vertex of a defect mask, in normalized [0..1] image coordinates. */
export interface MaskPoint {
  x: number
  y: number
}

/**
 * One closed ring of a defect mask.
 *
 * A mask can cover several disconnected patches. Each of them is its own ring,
 * because drawing them as one ring would close the gap between two patches with
 * a straight edge that is not in the mask.
 */
export type MaskRing = MaskPoint[]

/**
 * One detected instance as the workspace reads it: the detection *and* the
 * frame it sits on.
 *
 * The frame arrives with the defect on purpose - a review card draws the mask
 * over the photo, so fetching the two separately would make the list N+1
 * requests. `image_url` is a presigned URL that expires (`image_url_expires_in`
 * seconds), which is why it is never cached: re-read the list to get fresh ones.
 */
export interface Defect {
  id: string
  image_id: string
  defect_class: DefectClass
  confidence_score: number
  /**
   * Mask outlines drawn over the photo: one entry per connected part of the
   * mask. Null when the model returned none for this detection.
   */
  mask_polygon: MaskRing[] | null
  is_verified: boolean
  /**
   * Which object on this frame the detection belongs to.
   *
   * Detections that share the value are one piece of damage: at a low
   * confidence threshold the model reports a single crack as several
   * overlapping fragments, and the server groups them when the frame is
   * analysed (`defects.object_index`). Grouping by this field rather than
   * re-deriving it in the browser is what keeps the workspace list, the project
   * cards and the progress counters showing the same number of defects.
   */
  object_index: number
  created_at: ISODateString
  updated_at: ISODateString
  /** Original filename of the frame, shown as the card caption. */
  image_file_path: string
  /** Signed URL of the frame; null when the row has no stored object key. */
  image_url: string | null
  /** Lifetime of `image_url` in seconds from the moment it was signed. */
  image_url_expires_in: number
}

/**
 * `POST /projects/{id}/images/{imageId}/defects` - a mask drawn by a human.
 *
 * Only the geometry and the class travel: the server fixes `confidence_score`
 * and `is_verified`, because a hand-drawn mask is ground truth rather than a
 * detection awaiting review.
 */
export interface DefectCreatePayload {
  defect_class: DefectClass
  mask_polygon: MaskRing[]
}

// ---------------------------------------------------------------- 3D mapping
/**
 * A reconstructed mesh, ready to render: the object key plus a signed URL.
 *
 * `GET /projects/{id}/model3d` answers 404 while no model exists yet, or when
 * the project never asked for one - the workspace falls back to its 2D viewer.
 */
export interface ProjectModel3D {
  id: string
  project_id: string
  /** Object key of the .glb in the models bucket (the stable identity). */
  s3_path: string
  created_at: ISODateString
  updated_at: ISODateString
  /** Signed URL for the bytes; expires after `url_expires_in` seconds. */
  url: string
  url_expires_in: number
}

/** A defect localized on a mesh; one defect may map to several points. */
export interface DefectMap3D {
  id: string
  defect_id: string
  model_3d_id: string
  point_x: number
  point_y: number
  point_z: number
  created_at: ISODateString
  updated_at: ISODateString
}
