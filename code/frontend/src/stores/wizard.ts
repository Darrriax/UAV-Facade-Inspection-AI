/**
 * State of the project-creation wizard.
 *
 * The three steps are three *routes*, so this store is what connects them: the
 * id of the project step 1 created, its details - a refreshed
 * `/projects/<id>/photos` has to know what it is uploading to - and the outcome
 * of the last upload, which step 3 reports on before its first poll returns.
 *
 * Deliberately not persisted: a half-finished wizard is not a session, and a
 * stale project id in `localStorage` would outlive the project it points at.
 * It is also scoped to the account that created the project, so signing out and
 * back in as somebody else in the same tab cannot resume another inspector's
 * flow.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError, describeApiError } from '@/api/client'
import { getProject } from '@/api/projects'
import { useAuthStore } from '@/stores/auth'
import type { Project } from '@/types/api'

/**
 * What this session put on the server for the project.
 *
 * `stored` accumulates across upload chunks, so a retry after a failed batch
 * adds to it rather than resetting it - the number is what the server holds
 * because of this session, which is what step 3 reports and what the "frames
 * are stored, processing was not started" banner counts.
 *
 * `taskId` is set once `POST /start-processing` answers. Until then the frames
 * are pending and nothing is coming for them, which is a state the UI has to be
 * able to explain.
 */
export interface UploadSession {
  stored: number
  taskId: string | null
}

/**
 * The details form of step 1, as the user left it.
 *
 * Step 1 is a route like the others, so walking back to it mounts the component
 * again - and a component that owns its fields would hand the user a blank form
 * for a project they have already named. The draft is the memory that fixes
 * that: every keystroke, so an edit nobody submitted yet survives the trip to
 * step 2 and back.
 */
export interface ProjectDraft {
  name: string
  location: string
  description: string
  needs3dModel: boolean
}

export type WizardLoadStatus = 'idle' | 'loading' | 'ready' | 'error'

export const useWizardStore = defineStore('wizard', () => {
  const auth = useAuthStore()

  const project = ref<Project | null>(null)
  /** The account the remembered project belongs to (see the module docstring). */
  const ownerId = ref<string | null>(null)
  const status = ref<WizardLoadStatus>('idle')
  const error = ref<string | null>(null)
  const upload = ref<UploadSession>({ stored: 0, taskId: null })
  /** True once the user chose to continue past a cover photo that failed to upload. */
  const coverPhotoSkipped = ref(false)
  /** What step 1's fields held the last time the user was on them. */
  const draft = ref<ProjectDraft | null>(null)

  // --------------------------------------------------------------- getters
  const projectId = computed<string | null>(() => project.value?.id ?? null)
  const projectName = computed<string | null>(() => project.value?.name ?? null)
  const needs3dModel = computed(() => project.value?.needs_3d_model ?? false)
  /**
   * Whether the flow's project analyses frames for defects.
   *
   * Defaults to true for a project that has not been loaded yet, matching the
   * API's own default: a screen that renders before the row arrives must not
   * claim detection is off.
   */
  const detectDefects = computed(() => project.value?.detect_defects ?? true)
  const storedFrames = computed(() => upload.value.stored)
  /** True once analysis has been queued, so the UI can stop offering to start it. */
  const isQueued = computed(() => upload.value.taskId !== null)
  const isLoading = computed(() => status.value === 'loading')
  /** Set to the project itself only when the signed-in account owns the flow. */
  const resumableProject = computed<Project | null>(() => {
    if (project.value === null || ownerId.value === null) return null
    return auth.user?.id === ownerId.value ? project.value : null
  })

  // --------------------------------------------------------------- actions
  /** Adopt a project as the flow's subject and remember who owns it. */
  function remember(next: Project): Project {
    project.value = next
    ownerId.value = next.user_id
    status.value = 'ready'
    error.value = null
    return next
  }

  /**
   * Load a project into the store, from the API.
   *
   * Steps 2 and 3 are addressable URLs, so a refresh or a deep link arrives with
   * an id but no memory of the wizard. The already-loaded project is reused
   * unless `force` is set, which is what keeps step changes from re-fetching.
   *
   * Returns `null` on failure - the caller renders its own empty state - and
   * keeps the message here so every step words the failure the same way.
   */
  async function loadProject(id: string, options: { force?: boolean } = {}): Promise<Project | null> {
    if (!options.force && project.value?.id === id) {
      status.value = 'ready'
      return project.value
    }

    status.value = 'loading'
    error.value = null
    try {
      return remember(await getProject(id))
    } catch (cause) {
      error.value = describeApiError(cause)
      status.value = 'error'
      // A 404 from `/projects/{id}` is the API's way of saying "not yours, or
      // not there" - the same answer, so the two cases are not distinguished
      // in the UI either.
      if (!(cause instanceof ApiError)) throw cause
      return null
    }
  }

  /** Add the frames one upload chunk confirmed, keeping any earlier total. */
  function addStored(count: number): void {
    upload.value = { ...upload.value, stored: upload.value.stored + count }
  }

  /**
   * Adopt the pending frames the server already holds.
   *
   * A sortie can take tens of minutes, so a refresh in the middle of one is
   * ordinary: the component's memory of the upload is gone while the rows are
   * still in storage. Only used when this session has counted nothing of its
   * own - an accumulated total is the smaller truth and is kept.
   */
  function noteStoredPending(pending: number): void {
    if (pending <= 0 || upload.value.stored > 0) return
    upload.value = { ...upload.value, stored: pending }
  }

  /** Record that analysis was queued (the Celery task id, for the log lines). */
  function markQueued(taskId: string): void {
    upload.value = { ...upload.value, taskId }
  }

  function markCoverPhotoSkipped(skipped = true): void {
    coverPhotoSkipped.value = skipped
  }

  /**
   * Remember step 1's fields.
   *
   * A copy, not the form object itself: the component (and its reactivity
   * scope) goes away when the step does, and the store must not hold a dead
   * proxy of it.
   */
  function saveDraft(next: ProjectDraft): void {
    draft.value = { ...next }
  }

  /** Reset the flow. Called by "Start over" and after the workspace is reached. */
  function clear(): void {
    project.value = null
    ownerId.value = null
    status.value = 'idle'
    error.value = null
    upload.value = { stored: 0, taskId: null }
    coverPhotoSkipped.value = false
    // "Start a new one" means a new project, so the old details go with it.
    draft.value = null
  }

  return {
    // state
    project,
    status,
    error,
    upload,
    coverPhotoSkipped,
    draft,
    // getters
    projectId,
    projectName,
    needs3dModel,
    detectDefects,
    storedFrames,
    isQueued,
    isLoading,
    resumableProject,
    // actions
    remember,
    loadProject,
    addStored,
    noteStoredPending,
    markQueued,
    markCoverPhotoSkipped,
    saveDraft,
    clear,
  }
})
