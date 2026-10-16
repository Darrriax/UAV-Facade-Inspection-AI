<script setup lang="ts">
/**
 * The 3D model page: this project's building, reconstructed from its own frames.
 *
 * ## Who runs the reconstruction
 *
 * The worker also builds a mesh, at the end of an analysis run, and this page
 * used to be a reader of *that* build - a bar fed by `project.model3d_progress`
 * and a button posting to `/projects/{id}/model3d`. A Celery task carries a
 * deadline, though, and one NodeODM call over hundreds of full-resolution frames
 * outlives it: the run then ends with no mesh and a `model3d_error` explaining
 * it. So the reconstruction runs from the browser instead. The frames come out
 * of storage through the API's signed URLs, go straight to NodeODM
 * (`utils/nodeodm.ts`), and this page follows the run for as long as it takes -
 * no worker, no deadline, ODM's own percentage on screen.
 *
 * The worker still owns the analysis, and the model still belongs to the project:
 * once a mesh exists, the server serves it and this page renders it. A finished
 * run is therefore handed back to the API (`POST /projects/{id}/model3d/mesh`),
 * which stores it in the models bucket and registers it - so the model survives
 * a reload, and every later visit reads it from storage instead of
 * reconstructing the building again. A run whose upload fails still renders:
 * the reconstruction is the expensive part and it is complete either way, so
 * losing the bytes on the way to storage is warned about, not thrown away.
 *
 * ## Two screens
 *
 * * No mesh yet - what the project has to reconstruct from, and then the stages
 *   of the run itself: frames out of storage, upload, ODM's percentage,
 *   download, storing the result.
 * * A mesh - the viewer and nothing else. The stage below the project header
 *   belongs to the model, so the viewer carries no instructions, no camera
 *   button and no wireframe toggle. The one thing drawn over the scene is the
 *   notice that the mesh could not be stored, because that changes what the
 *   next reload will look like.
 *
 * ## Every frame, in batches
 *
 * A run sends the whole sortie: photogrammetry reads overlap between
 * neighbouring frames, and dropping frames is precisely what makes a mesh fail
 * to solve. What the page does not do is hold the whole sortie at once. It
 * reads a batch out of storage, sends that batch, and releases its copies
 * before reading the next one, so peak memory follows the batch rather than the
 * project. ODM's API is shaped for this - one `init`, many `upload`s, one
 * `commit` - and nothing starts before the commit, so the reconstruction never
 * sees a partial set either.
 */

import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, useRoute } from 'vue-router'

import { ApiError, describeApiError } from '@/api/client'
import {
  getProject,
  getProjectModel3D,
  listAllProjectFrames,
  uploadProjectModel3D,
} from '@/api/projects'
import AppIcon from '@/components/AppIcon.vue'
import AppSpinner from '@/components/AppSpinner.vue'
import ProjectViewHeader from '@/components/ProjectViewHeader.vue'
import ModelViewer from '@/components/workspace/ModelViewer.vue'
import { ROUTE_NAMES } from '@/router/routes/names'
import type { Project, ProjectModel3D, ReviewFrame } from '@/types/api'
import { NODEODM_STATUS, NodeOdmClient, NodeOdmError } from '@/utils/nodeodm'
import type { NodeOdmOptions } from '@/utils/nodeodm'

/**
 * Where NodeODM listens, as an absolute URL.
 *
 * A browser is the client here, so this cannot ride the app's own `/api` proxy.
 * ODM answers CORS with the permissive headers its image does, so the page calls
 * `localhost:3000` directly. `||` rather than `??`: an env var that is present
 * but empty is a blank URL, not an override.
 */
const NODEODM_URL = import.meta.env.VITE_NODEODM_URL || 'http://localhost:3000'

/** Poll interval while ODM works: live enough to move, rare enough to be polite. */
const POLL_MS = 4_000

/** Below three frames there is nothing to triangulate. */
const MIN_FRAMES = 3

/**
 * Frames per storage read and per upload request.
 *
 * One number for both, because a batch is one trip: read out of storage, sent
 * to ODM, released. Twenty full-resolution frames is a couple of hundred
 * megabytes - bounded enough that a thousand-frame project does not matter,
 * large enough that the requests stay rare.
 */
const BATCH_SIZE = 20

/** ODM needs the option name spelled `lowest` for its cheapest feature detector. */
type Quality = 'lowest' | 'low' | 'medium'

/**
 * One screen at a time, in the order a run happens.
 *
 * `preparing` is the frame listing, `uploading` the read-and-send loop
 * that follows it, `retrieving` the download of the archive ODM leaves behind,
 * and `saving` the upload of the extracted mesh to the API - so the status line
 * never falls back to a vague "working".
 */
type Phase =
  | 'idle'
  | 'preparing'
  | 'uploading'
  | 'processing'
  | 'retrieving'
  | 'saving'
  | 'done'
  | 'failed'
  | 'stopped'

const route = useRoute()
const { t } = useI18n()

const projectId = computed(() => String(route.params.projectId ?? ''))

const project = ref<Project | null>(null)
const serverModel = ref<ProjectModel3D | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

/** The mesh this page built, as an object URL, or null until one exists. */
const glbUrl = ref<string | null>(null)

/**
 * Why the finished mesh could not be stored, or null when it was.
 *
 * A warning rather than a failure: the reconstruction is over and the model is
 * on screen either way - what is lost is its survival across a reload.
 */
const saveError = ref<string | null>(null)

const phase = ref<Phase>('idle')
const errorMessage = ref<string | null>(null)
/** How many frames this run sends in total, and how many are already in ODM. */
const framesTotal = ref(0)
const uploaded = ref(0)
const odmProgress = ref(0)
const odmStatus = ref<string | null>(null)
const elapsedSeconds = ref(0)

/** ODM resolves features at this quality; every frame is always sent. */
const quality = ref<Quality>('low')

let client: NodeOdmClient | null = null
let taskUuid: string | null = null
let pollTimer: number | null = null
let clockTimer: number | null = null
let startedAt: number | null = null
/** Aborts the storage reads of the run in flight, which have no other handle. */
let reads: AbortController | null = null
let stopRequested = false

const frameCount = computed(() => project.value?.stats.total_images ?? 0)

/** What the viewer renders: this page's own mesh first, then the server's. */
const modelUrl = computed(() => glbUrl.value ?? serverModel.value?.url ?? null)

/** The URL the last refresh was for, so a failed load is refreshed once. */
const refreshedFor = ref<string | null>(null)

const busy = computed(
  () =>
    phase.value === 'preparing' ||
    phase.value === 'uploading' ||
    phase.value === 'processing' ||
    phase.value === 'retrieving' ||
    phase.value === 'saving',
)

/**
 * Whether there is still something to cancel.
 *
 * `saving` is deliberately not cancellable: the reconstruction is finished by
 * then, and what is in flight is the upload that keeps the result - stopping it
 * could only lose work, never save any.
 */
const stoppable = computed(() => busy.value && phase.value !== 'saving')

const canBuild = computed(
  () =>
    project.value?.needs_3d_model === true &&
    frameCount.value >= MIN_FRAMES &&
    !busy.value,
)

const elapsedLabel = computed(() => {
  const minutes = Math.floor(elapsedSeconds.value / 60)
  return `${minutes}:${String(elapsedSeconds.value % 60).padStart(2, '0')}`
})

const statusLine = computed(() => {
  switch (phase.value) {
    case 'preparing':
      // The frame list is one request, so this is the whole of it: there is no
      // count to report yet, and "0 of 395" would claim work that has not begun.
      return t('inspector.model3d.run.preparing')
    case 'uploading':
      return t('inspector.model3d.run.uploading', {
        done: uploaded.value,
        total: framesTotal.value,
      })
    case 'processing':
      return odmStatus.value ?? t('inspector.model3d.run.processing')
    case 'retrieving':
      return t('inspector.model3d.run.retrieving')
    case 'saving':
      return t('inspector.model3d.run.saving')
    case 'done':
      return t('inspector.model3d.run.done')
    case 'stopped':
      return t('inspector.model3d.run.stopped')
    case 'failed':
      return t('inspector.model3d.run.failed')
    default:
      return ''
  }
})

/** Share of the current stage, for the bar: the read-and-send loop, then ODM. */
const barPercent = computed(() => {
  switch (phase.value) {
    case 'uploading':
      return framesTotal.value === 0 ? 0 : (uploaded.value / framesTotal.value) * 100
    case 'processing':
      return odmProgress.value
    // ODM is done and its model has been downloaded; all that is left is
    // handing the bytes over, which is not a share of anything.
    case 'saving':
      return 100
    default:
      return 0
  }
})

// ------------------------------------------------------------------- loading
onMounted(async () => {
  const id = projectId.value
  if (!id) {
    error.value = t('inspector.model3d.missingId')
    loading.value = false
    return
  }

  try {
    // In parallel: the model lookup answers 404 for a project with no mesh, and
    // that is an ordinary result rather than a failure of the project read.
    const [loadedProject, loadedModel] = await Promise.all([
      getProject(id),
      getProjectModel3D(id).catch((cause: unknown) => {
        if (cause instanceof ApiError && cause.isNotFound) return null
        throw cause
      }),
    ])
    project.value = loadedProject
    serverModel.value = loadedModel
  } catch (cause) {
    error.value = describeApiError(cause)
  } finally {
    loading.value = false
  }
})

onUnmounted(() => {
  stopTimers()
  // A run's storage reads have no other handle, and a page that is gone must
  // not leave them in flight.
  reads?.abort()
  if (glbUrl.value) URL.revokeObjectURL(glbUrl.value)
})

// --------------------------------------------------------------------- run
function startClock(): void {
  startedAt = Date.now()
  elapsedSeconds.value = 0
  clockTimer = window.setInterval(() => {
    if (startedAt !== null) elapsedSeconds.value = Math.floor((Date.now() - startedAt) / 1000)
  }, 1000)
}

function stopTimers(): void {
  if (pollTimer !== null) window.clearTimeout(pollTimer)
  if (clockTimer !== null) window.clearInterval(clockTimer)
  pollTimer = null
  clockTimer = null
}

/** A run that ends badly. A stop is not a failure, even if it aborted a read. */
function fail(cause: unknown): void {
  stopTimers()
  if (stopRequested) {
    phase.value = 'stopped'
    return
  }
  phase.value = 'failed'
  errorMessage.value =
    cause instanceof ApiError
      ? describeApiError(cause)
      : cause instanceof Error
        ? cause.message
        : String(cause)
}

function resetRun(): void {
  stopTimers()
  reads?.abort()
  reads = null
  client = null
  taskUuid = null
  errorMessage.value = null
  saveError.value = null
  framesTotal.value = 0
  uploaded.value = 0
  odmProgress.value = 0
  odmStatus.value = null
  elapsedSeconds.value = 0
}

async function build(): Promise<void> {
  if (!canBuild.value) return
  resetRun()
  stopRequested = false
  phase.value = 'preparing'
  startClock()

  try {
    // Signed URLs are requested here rather than on mount: they expire, and a
    // page that only reads them when a run starts never holds a stale one. A
    // frame with no stored object is skipped rather than failing the run; the
    // rest of the sortie can still solve.
    const frames = (await listAllProjectFrames(projectId.value)).filter(
      (frame) => frame.image_url !== null,
    )
    framesTotal.value = frames.length
    if (frames.length < MIN_FRAMES) {
      throw new NodeOdmError(
        `The project holds only ${frames.length} readable frame(s); ODM needs at least ${MIN_FRAMES}.`,
      )
    }

    // Named locally as well: the module-level reference is what `stop()` cancels
    // through, and a `let` can be reassigned between the awaits below.
    const odm = new NodeOdmClient(NODEODM_URL)
    client = odm

    // Options are ODM's own names. `gltf` is what makes ODM write the textured
    // GLB this page renders; the orthophoto and the report are not needed, and
    // skipping them is most of the time a run saves.
    const options: NodeOdmOptions = {
      'feature-quality': quality.value,
      'pc-quality': 'lowest',
      'skip-orthophoto': true,
      'skip-report': true,
      gltf: true,
    }

    taskUuid = await odm.createTask(`project-${projectId.value.slice(0, 8)}`, options)
    if (stopRequested) return

    phase.value = 'uploading'
    reads = new AbortController()
    for (let start = 0; start < frames.length; start += BATCH_SIZE) {
      if (stopRequested) return
      // Read, send, release: `batch` goes out of scope at the end of each turn,
      // so the frames of the previous batch are free to be collected while the
      // next one is read.
      const batch: File[] = []
      for (const frame of frames.slice(start, start + BATCH_SIZE)) {
        batch.push(await readFrame(frame, reads.signal))
      }
      if (stopRequested) return
      await odm.uploadImages(taskUuid, batch)
      uploaded.value = Math.min(start + batch.length, frames.length)
    }

    if (stopRequested) return
    // The commit is what starts the reconstruction: a run stopped before it
    // leaves ODM with an open task and nothing queued.
    await odm.commitTask(taskUuid)
    if (stopRequested) return
    phase.value = 'processing'
    poll()
  } catch (cause) {
    fail(cause)
  }
}

/**
 * One frame's bytes, as a `File` that ODM can be handed.
 *
 * The signed URL is cross-origin, so the browser reads it the way an `<img>`
 * would not have to: with CORS, which the API sets on the bucket at startup.
 * Frames are read one at a time within a batch on purpose - issuing a whole
 * batch at once would put its responses in memory simultaneously, which is the
 * one thing batching is here to avoid.
 */
async function readFrame(frame: ReviewFrame, signal: AbortSignal): Promise<File> {
  const url = frame.image_url
  if (!url) throw new NodeOdmError(`Frame ${frame.file_path} has no stored object`)

  const response = await fetch(url, { signal })
  if (!response.ok) {
    throw new NodeOdmError(`Storage answered HTTP ${response.status} for ${frame.file_path}`)
  }

  const blob = await response.blob()
  const name = frame.file_path.split('/').pop() || `frame-${frame.id}.jpg`
  return new File([blob], name, { type: blob.type || 'image/jpeg' })
}

async function poll(): Promise<void> {
  if (!client || !taskUuid) return
  try {
    const info = await client.getTaskInfo(taskUuid)
    odmProgress.value = Math.round(info.progress ?? 0)
    switch (info.status.code) {
      case NODEODM_STATUS.queued:
        odmStatus.value = t('inspector.model3d.run.queued')
        break
      case NODEODM_STATUS.running:
        odmStatus.value = `${t('inspector.model3d.run.processing')} ${odmProgress.value}%`
        break
      case NODEODM_STATUS.completed:
        await retrieve()
        return
      case NODEODM_STATUS.failed:
        fail(new NodeOdmError(info.status.errorMessage ?? t('inspector.model3d.run.failed')))
        return
      case NODEODM_STATUS.canceled:
        stopTimers()
        phase.value = 'stopped'
        return
    }
  } catch (cause) {
    fail(cause)
    return
  }
  pollTimer = window.setTimeout(() => void poll(), POLL_MS)
}

/**
 * The finished reconstruction: stored, then rendered from an object URL.
 *
 * ODM hands back one archive from which the client picks the GLB, so this is the
 * step that can still fail after a successful run - which is why it is its own
 * phase rather than a silent tail of the last one.
 *
 * Storage comes first because it is the one thing that cannot be redone from
 * this page's state: the mesh only exists here, and the tab is one reload away
 * from losing it. The upload is also what lets the *next* visit render the
 * model without a reconstruction, so a failure is worth saying out loud - but
 * not worth failing the run over, since the model is on screen either way.
 */
async function retrieve(): Promise<void> {
  if (!client || !taskUuid) return
  stopTimers()
  phase.value = 'retrieving'
  let blob: Blob
  try {
    blob = await client.fetchGlb(taskUuid)
  } catch (cause) {
    fail(cause)
    return
  }

  phase.value = 'saving'
  try {
    // Adopted rather than kept alongside: the stored record is what a reload
    // will find, and its URL is signed by the same service that serves it then.
    serverModel.value = await uploadProjectModel3D(projectId.value, blob)
  } catch (cause) {
    saveError.value = describeApiError(cause)
  }

  glbUrl.value = URL.createObjectURL(blob)
  phase.value = 'done'
}

/**
 * The viewer could not load the mesh. A stored mesh's signed URL expires after an
 * hour, and a page left open past that finds its URL dead - so ask the API for a
 * fresh one, once per URL. Retrying the same URL would only repeat the failure.
 */
async function onViewerFailed(): Promise<void> {
  const current = modelUrl.value
  if (!current || glbUrl.value || refreshedFor.value === current) return
  refreshedFor.value = current
  try {
    serverModel.value = await getProjectModel3D(projectId.value)
  } catch {
    // Keep the failure on screen: there is no better URL to fall back to.
  }
}

async function stop(): Promise<void> {
  if (!busy.value) return
  stopRequested = true
  reads?.abort()
  // Removing an ODM task would not stop it; only a cancel ends the work. A run
  // stopped while frames are still going up has not been committed, so there is
  // nothing in ODM's queue to cancel yet.
  if (client && taskUuid && phase.value !== 'uploading') {
    try {
      await client.cancelTask(taskUuid)
    } catch (cause) {
      fail(cause)
      return
    }
  }
  stopTimers()
  phase.value = 'stopped'
}

</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col bg-slate-100">
    <!-- The project's own header, shared with the 2D workspace: the same project
         must not be framed by two different kinds of chrome. Its totals come
         from the server rollup, because this screen loads no defect rows. -->
    <ProjectViewHeader :project="project" view="model" />

    <div
      v-if="error && project === null"
      class="m-4 flex flex-col gap-3 rounded-xl border border-red-200 bg-red-50 px-5 py-4 sm:flex-row sm:items-center sm:justify-between"
      role="alert"
    >
      <p class="text-sm text-red-800">
        <span class="font-semibold">{{ t('inspector.model3d.loadError') }}</span> {{ error }}
      </p>
      <RouterLink
        :to="{ name: ROUTE_NAMES.dashboard }"
        class="shrink-0 rounded-lg border border-red-300 bg-white px-3.5 py-2 text-sm font-medium text-red-700 transition hover:bg-red-100"
      >
        {{ t('inspector.model3d.back') }}
      </RouterLink>
    </div>

    <div v-else-if="loading" class="flex flex-1 items-center justify-center text-slate-500">
      <AppSpinner :label="t('inspector.model3d.loading')" />
    </div>

    <!-- The mesh, and nothing else: keyed by URL, so a later reconstruction
         mounts a fresh viewer instead of re-pointing a live loader. -->
    <div v-else-if="modelUrl" class="relative min-h-0 flex-1">
      <ModelViewer :key="modelUrl" :url="modelUrl" @failed="onViewerFailed" />

      <!-- The one exception to "nothing over the scene": a mesh that was not
           stored disappears with the next reload, and the user is the only one
           who can do something about that. -->
      <p
        v-if="saveError"
        class="absolute inset-x-4 top-4 mx-auto w-fit max-w-2xl rounded-xl border border-amber-300 bg-amber-50/95 px-4 py-2 text-center text-xs text-amber-900 shadow-lg"
        role="status"
      >
        <span class="font-semibold">{{ t('inspector.model3d.run.saveFailed') }}</span>
        {{ saveError }}
      </p>
    </div>

    <!-- No mesh to render: what the project has, and what a run would do with it.
         `v-else` and not a chain of its own: a page that has a mesh must not
         also draw the panel that offers to build one. -->
    <div v-else class="flex min-h-0 flex-1 items-center justify-center overflow-y-auto bg-slate-100 p-6">
      <div
        class="flex w-full max-w-lg flex-col items-center gap-4 rounded-2xl border border-slate-200 bg-white px-8 py-10 text-center shadow-sm"
      >
        <!-- The project never asked for a model: photogrammetry has no place here. -->
        <template v-if="project && !project.needs_3d_model">
          <span
            class="inline-flex h-14 w-14 items-center justify-center rounded-2xl border border-slate-200 bg-slate-50"
          >
            <AppIcon name="cube" class="h-7 w-7 text-slate-400" />
          </span>
          <h2 class="text-base font-semibold text-slate-900">
            {{ t('inspector.model3d.notRequested.title') }}
          </h2>
          <p class="max-w-md text-sm text-slate-600">
            {{ t('inspector.model3d.notRequested.body') }}
          </p>
          <RouterLink
            :to="{ name: ROUTE_NAMES.projectWorkspace, params: { projectId } }"
            class="mt-1 inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3.5 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100"
          >
            <AppIcon name="image" class="h-4 w-4" />
            {{ t('inspector.model3d.review2d') }}
          </RouterLink>
        </template>

        <!-- Nothing to reconstruct from yet. -->
        <template v-else-if="project && frameCount === 0">
          <span
            class="inline-flex h-14 w-14 items-center justify-center rounded-2xl border border-slate-200 bg-slate-50"
          >
            <AppIcon name="upload" class="h-7 w-7 text-slate-400" />
          </span>
          <h2 class="text-base font-semibold text-slate-900">
            {{ t('inspector.model3d.panel.title') }}
          </h2>
          <p class="max-w-md text-sm text-slate-600">
            {{ t('inspector.model3d.panel.noFrames') }}
          </p>
          <RouterLink
            :to="{ name: ROUTE_NAMES.projectPhotos, params: { projectId } }"
            class="mt-1 inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3.5 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100"
          >
            <AppIcon name="upload" class="h-4 w-4" />
            {{ t('inspector.wizard.step3.empty.action') }}
          </RouterLink>
        </template>

        <!-- The run: what will be sent, and then how far it has got. -->
        <template v-else-if="project">
          <span
            class="inline-flex h-14 w-14 items-center justify-center rounded-2xl border"
            :class="busy ? 'animate-pulse border-blue-200 bg-blue-50' : 'border-slate-200 bg-slate-50'"
          >
            <AppIcon name="cube" class="h-7 w-7" :class="busy ? 'text-blue-500' : 'text-slate-400'" />
          </span>
          <h2 class="text-base font-semibold text-slate-900">
            {{ t('inspector.model3d.panel.title') }}
          </h2>

          <label class="mt-1 block w-full text-left text-sm">
            <span class="font-medium text-slate-700">{{ t('inspector.model3d.panel.quality') }}</span>
            <select
              v-model="quality"
              :disabled="busy"
              class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100"
            >
              <option value="lowest">lowest</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
            </select>
          </label>

          <p class="text-sm text-slate-600">
            {{ t('inspector.model3d.panel.source', { total: frameCount }) }}
          </p>

          <div class="flex flex-wrap items-center justify-center gap-3">
            <button
              type="button"
              :disabled="!canBuild"
              class="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
              @click="build"
            >
              <span
                v-if="busy"
                class="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
                aria-hidden="true"
              />
              <AppIcon v-else name="cube" class="h-4 w-4" />
              {{ busy ? t('inspector.model3d.buildingAction') : t('inspector.model3d.build') }}
            </button>
            <button
              v-if="stoppable"
              type="button"
              class="rounded-lg border border-red-300 px-4 py-2.5 text-sm font-medium text-red-800 transition hover:bg-red-50"
              @click="stop"
            >
              {{ t('inspector.model3d.stop') }}
            </button>
          </div>

          <div v-if="phase !== 'idle'" class="w-full space-y-2">
            <div class="flex items-center justify-between gap-3 text-sm">
              <span
                class="text-left font-medium"
                :class="phase === 'failed' ? 'text-red-800' : 'text-slate-800'"
              >
                {{ statusLine }}
              </span>
              <span class="font-mono text-slate-500 tabular-nums">{{ elapsedLabel }}</span>
            </div>

            <div
              v-if="
                phase === 'preparing' ||
                phase === 'uploading' ||
                phase === 'processing' ||
                phase === 'saving'
              "
              class="h-2.5 w-full overflow-hidden rounded-full bg-slate-200 ring-1 ring-slate-300/70 ring-inset"
              role="progressbar"
              :aria-label="t('inspector.model3d.run.stage')"
              :aria-valuenow="phase === 'preparing' ? undefined : Math.round(barPercent)"
              :aria-busy="phase === 'preparing'"
              aria-valuemin="0"
              aria-valuemax="100"
            >
              <!-- The frame listing has no share to show: a sweep says
                   "working" without claiming a position on the track. -->
              <div
                v-if="phase === 'preparing'"
                class="h-full w-1/3 animate-[bar-sweep_1.6s_ease-in-out_infinite] rounded-full bg-blue-500/80"
              />
              <div
                v-else
                class="h-full rounded-full bg-gradient-to-r from-blue-600 to-blue-400 transition-[width] duration-700 ease-out"
                :style="{ width: `${barPercent}%` }"
              />
            </div>

            <p
              v-if="phase === 'failed' && errorMessage"
              class="rounded-xl border border-red-200 bg-red-50 p-3 text-left text-xs text-red-800"
            >
              {{ errorMessage }}
            </p>
          </div>

        </template>
      </div>
    </div>
  </div>
</template>
