<script setup lang="ts">
/**
 * Wizard step 2: the UAV frames, in chunks.
 *
 * A real sortie is hundreds of frames and several gigabytes, so this screen does
 * three things the single-request version could not:
 *
 * 1. **Folders.** A DJI card is a folder, and pairing the photos with their
 *    sidecars (`.SRT`, `.LRF`) means dragging the folder is the natural gesture.
 *    Both a folder picker and a recursive folder drop are supported.
 * 2. **Chunks.** The selection is split by `planUploadChunks` (bounded by files
 *    *and* bytes) and sent a few at a time by `uploadInChunks`, which is what
 *    keeps a 4 GB selection from becoming one impossible request.
 * 3. **Analysis as a separate step.** Chunks only *store* frames; once the last
 *    one lands, `POST /start-processing` queues the batch. A chunk that fails
 *    stops the run and nothing is queued, so a half-uploaded sortie is never
 *    half-analysed.
 *
 * The frames stored by a chunk that succeeded before a later failure are kept
 * (they are durable rows, and re-sending them would duplicate frames): the
 * selection is pruned to exactly what the server does not have yet, so pressing
 * upload again continues the sortie instead of restarting it.
 */

import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import { describeApiError } from '@/api/client'
import { getProjectImageStats, startProjectProcessing, uploadProjectImages } from '@/api/projects'
import { ROUTE_NAMES } from '@/router/routes/names'
import { useToasts } from '@/composables/useToasts'
import { useWizardStore } from '@/stores/wizard'
import { formatBytes, isAcceptedImage } from '@/utils/files'
import {
  ACCEPTED_TYPES,
  MAX_BYTES_PER_FILE,
  UPLOAD_CHUNK_MAX_FILES,
  UPLOAD_CONCURRENCY,
  filesFromDataTransfer,
  isSystemNoise,
  pickDirectory,
  planUploadChunks,
  uploadInChunks,
  type UploadChunk,
  type UploadProgress,
} from '@/utils/upload'

/**
 * A file that was not selected, with the catalogue key that explains why.
 *
 * The reason travels as a key rather than as a finished sentence: the list is
 * rendered on a screen that can change language, so `t()` has to run where the
 * active locale is known. `params` carries what the key interpolates, and is
 * part of a group's identity - "4.2 MB is over the limit" and "9.1 MB is over
 * the limit" are two facts, not one repeated forty times.
 */
interface Rejection {
  name: string
  reasonKey: string
  params: Record<string, string>
}

/** Files listed individually before the list collapses into a count. */
const LIST_LIMIT = 50
/** Example filenames shown per rejection reason. */
const EXAMPLES_PER_REASON = 3

const route = useRoute()
const router = useRouter()
const wizard = useWizardStore()
const toast = useToasts()
const { t } = useI18n()

const selected = ref<File[]>([])
const rejections = ref<Rejection[]>([])
const ignoredNoise = ref(0)
const dragging = ref(false)
const uploading = ref(false)
const starting = ref(false)
/** True while a dropped folder is being walked; hundreds of entries take a moment. */
const scanning = ref(false)
const progress = ref<UploadProgress | null>(null)
const chunkFailure = ref<{ chunk: UploadChunk; message: string } | null>(null)
const queueFailure = ref<string | null>(null)

const fileInput = ref<HTMLInputElement | null>(null)
const folderInput = ref<HTMLInputElement | null>(null)
/** The in-flight run, so Cancel can actually cancel it. */
let controller: AbortController | null = null

const projectId = computed<string | null>(() => {
  const raw = route.params.projectId
  const fromRoute = Array.isArray(raw) ? raw[0] : raw
  return typeof fromRoute === 'string' && fromRoute.length > 0 ? fromRoute : wizard.projectId
})

const loadFailed = computed(() => wizard.status === 'error')
const totalBytes = computed(() => selected.value.reduce((sum, file) => sum + file.size, 0))
/** What the selection will be sent as - recomputed as files are added or removed. */
const batches = computed(() => planUploadChunks(selected.value).length)
const visibleSelected = computed(() => selected.value.slice(0, LIST_LIMIT))
const hiddenSelected = computed(() => Math.max(0, selected.value.length - LIST_LIMIT))

/** Rejections grouped by reason: 395 sidecar files must not become 395 lines. */
const rejectionGroups = computed(() => {
  const groups = new Map<
    string,
    {
      key: string
      reasonKey: string
      params: Record<string, string>
      count: number
      examples: string[]
    }
  >()
  for (const rejection of rejections.value) {
    const key = `${rejection.reasonKey}|${JSON.stringify(rejection.params)}`
    const group =
      groups.get(key) ??
      ({ key, reasonKey: rejection.reasonKey, params: rejection.params, count: 0, examples: [] })
    group.count += 1
    if (group.examples.length < EXAMPLES_PER_REASON) group.examples.push(rejection.name)
    groups.set(key, group)
  }
  return [...groups.values()].sort((left, right) => right.count - left.count)
})

const progressPercent = computed(() => {
  const snapshot = progress.value
  if (snapshot === null || snapshot.totalFiles === 0) return 0
  return Math.round((snapshot.completedFiles / snapshot.totalFiles) * 100)
})

const uploadLabel = computed(() => {
  if (uploading.value) return t('inspector.wizard.step2.actions.uploading')
  if (starting.value) return t('inspector.wizard.step2.actions.starting')
  if (selected.value.length > 0) {
    return t('inspector.wizard.step2.actions.upload', selected.value.length)
  }
  if (wizard.storedFrames > 0 && !wizard.isQueued) return t('inspector.wizard.step2.actions.start')
  return t('inspector.wizard.step2.actions.uploadFrames')
})

const canUpload = computed(
  () =>
    !uploading.value &&
    !starting.value &&
    (selected.value.length > 0 || (wizard.storedFrames > 0 && !wizard.isQueued)),
)

onMounted(async () => {
  const id = projectId.value
  if (id === null) return
  await wizard.loadProject(id)
  await adoptPendingFrames(id)
})

/**
 * Pick up frames the server already holds, after a reload mid-sortie.
 *
 * Without this the screen would claim nothing is stored while hundreds of
 * `pending` rows sit in storage - the state that decides whether the user has
 * anything to resume or to start analysing. Best-effort: a failed lookup must
 * not block a screen that works perfectly well without the number.
 */
async function adoptPendingFrames(id: string): Promise<void> {
  try {
    const stats = await getProjectImageStats(id)
    wizard.noteStoredPending(stats.pending)
  } catch {
    // Deliberately ignored: this is a convenience count, not a precondition.
  }
}

onUnmounted(() => {
  // Leaving the step must not leave a chunked run going.
  controller?.abort()
})

// --------------------------------------------------------------- selection
/**
 * Filter a batch of incoming files into the selection.
 *
 * Rejections are collected rather than raised one at a time: a dropped folder
 * can contain hundreds of files that are not frames, and each one deserves an
 * explanation but not a toast.
 */
function addFiles(incoming: readonly File[]): void {
  const additions: File[] = []
  const problems: Rejection[] = []
  let noise = 0

  for (const file of incoming) {
    if (isSystemNoise(file)) {
      // `._DJI_0001.JPG` twins and `.DS_Store`: expected in a folder, never a frame.
      noise += 1
      continue
    }
    if (file.size === 0) {
      problems.push({
        name: file.name,
        reasonKey: 'inspector.wizard.step2.rejected.empty',
        params: {},
      })
      continue
    }
    if (!isAcceptedImage(file)) {
      problems.push({
        name: file.name,
        reasonKey: 'inspector.wizard.step2.rejected.notImage',
        params: {},
      })
      continue
    }
    if (file.size > MAX_BYTES_PER_FILE) {
      problems.push({
        name: file.name,
        reasonKey: 'inspector.wizard.step2.rejected.tooLarge',
        params: {
          size: formatBytes(file.size),
          limit: formatBytes(MAX_BYTES_PER_FILE),
        },
      })
      continue
    }
    // The same frame dropped twice is one upload; `lastModified` keeps two
    // genuinely different files that happen to share a name apart.
    const duplicate = [...selected.value, ...additions].some(
      (kept) =>
        kept.name === file.name && kept.size === file.size && kept.lastModified === file.lastModified,
    )
    if (duplicate) continue
    additions.push(file)
  }

  selected.value = [...selected.value, ...additions]
  // Rejections accumulate across drops: the previous list explains files the
  // user may still be looking at, and silently replacing it hides them.
  rejections.value = [...rejections.value, ...problems]
  ignoredNoise.value += noise
  if (noise > 0 && problems.length === 0) {
    toast.info(
      t('inspector.wizard.step2.toast.noise', noise),
      t('inspector.wizard.step2.toast.noiseBody'),
    )
  }
  // A new selection is a new attempt: clear the previous run's verdicts.
  if (additions.length > 0) {
    chunkFailure.value = null
    queueFailure.value = null
  }
}

function onSelect(event: Event): void {
  const input = event.target as HTMLInputElement
  addFiles(Array.from(input.files ?? []))
  // Let the same file be picked again after a failed upload.
  input.value = ''
}

async function onDrop(event: DragEvent): Promise<void> {
  dragging.value = false
  scanning.value = true
  try {
    // Reads dropped folders recursively; `dataTransfer.files` alone is empty for
    // a directory drop, which is the gesture this screen is built around.
    addFiles(await filesFromDataTransfer(event.dataTransfer))
  } finally {
    scanning.value = false
  }
}

/**
 * `dragleave` also fires when the pointer crosses into a child element, which
 * would flicker the highlight. Ignore those transitions.
 */
function onDragLeave(event: DragEvent): void {
  const next = event.relatedTarget
  if (next instanceof Node && event.currentTarget instanceof HTMLElement) {
    if (event.currentTarget.contains(next)) return
  }
  dragging.value = false
}

function openFiles(): void {
  if (uploading.value) return
  fileInput.value?.click()
}

/**
 * Pick a folder and take what is in it.
 *
 * The directory picker is tried first because it is the only API that opens a
 * *folder* dialog; the hidden input stays as the fallback for browsers without
 * it. A dialog the reader dismisses is an answer - the input must not then pop
 * a second one over it.
 */
async function openFolder(): Promise<void> {
  if (uploading.value) return

  if (folderInput.value === null) return
  scanning.value = true
  try {
    const result = await pickDirectory()
    if (result.status === 'picked') {
      addFiles(result.files)
      return
    }
    if (result.status === 'cancelled') return
    folderInput.value.click()
  } finally {
    scanning.value = false
  }
}

function removeFile(index: number): void {
  selected.value = selected.value.filter((_, position) => position !== index)
}

function clearSelection(): void {
  selected.value = []
  rejections.value = []
  ignoredNoise.value = 0
  chunkFailure.value = null
}

// ------------------------------------------------------------------ upload
async function upload(): Promise<void> {
  const id = projectId.value
  if (id === null || !canUpload.value) return

  // Nothing left to send: the only remaining step is queueing what is stored.
  if (selected.value.length === 0) {
    await triggerProcessing(id)
    return
  }

  const chunks = planUploadChunks(selected.value)
  const chunkCount = chunks.length
  uploading.value = true
  chunkFailure.value = null
  queueFailure.value = null
  progress.value = {
    totalFiles: selected.value.length,
    totalChunks: chunkCount,
    completedFiles: 0,
    completedChunks: 0,
    inFlight: 0,
  }
  controller = new AbortController()

  try {
    const result = await uploadInChunks(
      chunks,
      async (chunk, signal) => {
        const response = await uploadProjectImages(id, chunk.files, signal)
        return response.uploaded
      },
      {
        concurrency: UPLOAD_CONCURRENCY,
        signal: controller.signal,
        onProgress: (snapshot) => {
          progress.value = snapshot
        },
      },
    )

    // Whatever the server confirmed is now on it: prune exactly those files so a
    // retry continues the sortie rather than duplicating frames.
    const stored = new Set(result.storedFiles)
    selected.value = selected.value.filter((file) => !stored.has(file))
    wizard.addStored(result.storedFiles.length)

    if (result.aborted) {
      toast.info(
        t('inspector.wizard.step2.toast.cancelled'),
        t('inspector.wizard.step2.toast.cancelledBody', result.storedFiles.length),
      )
      return
    }

    if (result.failure !== null) {
      const { chunk, error } = result.failure
      chunkFailure.value = { chunk, message: describeApiError(error) }
      // Deliberately no processing trigger: the sortie is incomplete.
      toast.error(
        t('inspector.wizard.step2.toast.batchFailed', {
          number: chunk.number,
          total: chunkCount,
        }),
        t(
          'inspector.wizard.step2.toast.batchFailedBody',
          { message: describeApiError(error), stored: result.storedFiles.length },
          result.storedFiles.length,
        ),
      )
      return
    }

    toast.success(
      t('inspector.wizard.step2.toast.uploaded', result.storedFiles.length),
      chunkCount === 1
        ? t('inspector.wizard.step2.toast.uploadedBodyOne')
        : t('inspector.wizard.step2.toast.uploadedBody', { batches: chunkCount }),
    )
    await triggerProcessing(id)
  } finally {
    uploading.value = false
    controller = null
  }
}

/**
 * Queue the stored frames for analysis and, only on success, move to step 3.
 *
 * The trigger is its own call, so its failure has its own state: the frames are
 * already stored, and a 503 (broker down) is fixed by trying again - not by
 * re-uploading gigabytes.
 */
async function triggerProcessing(id: string): Promise<void> {
  if (starting.value) return

  starting.value = true
  try {
    const response = await startProjectProcessing(id)
    wizard.markQueued(response.task_id)
    queueFailure.value = null
    toast.success(
      t('inspector.wizard.step2.toast.queued', response.pending_images),
      t('inspector.wizard.step2.toast.queuedBody', { task: response.task_id.slice(0, 8) }),
    )
    await goToProgress(id)
  } catch (cause) {
    queueFailure.value = describeApiError(cause)
    toast.error(t('inspector.wizard.step2.toast.queueFailed'), describeApiError(cause))
  } finally {
    starting.value = false
  }
}

function cancelUpload(): void {
  controller?.abort()
}

async function goToProgress(id: string | null): Promise<void> {
  if (id === null) return
  await router.push({ name: ROUTE_NAMES.projectProgress, params: { projectId: id } })
}
</script>

<template>
  <div class="space-y-5">
    <!-- Unknown / foreign project: the id came from the URL, not from step 1. -->
    <div
      v-if="loadFailed"
      role="alert"
      class="rounded-2xl border border-red-200 bg-red-50 p-6 text-sm text-red-800"
    >
      <div class="flex items-start gap-2.5">
        <AppIcon name="alert" class="mt-0.5 h-5 w-5 shrink-0" />
        <div>
          <p class="font-semibold">{{ t('inspector.wizard.projectUnavailable.title') }}</p>
          <p class="mt-1">{{ wizard.error }}</p>
          <RouterLink
            :to="{ name: ROUTE_NAMES.dashboard }"
            class="mt-3 inline-flex items-center gap-2 rounded-lg border border-red-300 px-3 py-2 font-medium transition hover:bg-red-100"
          >
            <AppIcon name="grid" class="h-4 w-4" />
            {{ t('inspector.wizard.projectUnavailable.back') }}
          </RouterLink>
        </div>
      </div>
    </div>

    <template v-else>
      <!-- Stored, but nothing was queued: the recovery is a retry of one call. -->
      <div
        v-if="queueFailure"
        role="alert"
        class="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900"
      >
        <div class="flex items-start gap-2.5">
          <AppIcon name="alert" class="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
          <div class="min-w-0">
            <p class="font-semibold">
              {{ t('inspector.wizard.step2.queueFailure.title', wizard.storedFrames) }}
            </p>
            <p class="mt-1">{{ queueFailure }}</p>
            <p class="mt-1 text-amber-800">
              {{ t('inspector.wizard.step2.queueFailure.hint') }}
            </p>
          </div>
        </div>
      </div>

      <!-- A chunk failed: the run stopped, nothing was queued, the rest is still selected. -->
      <div
        v-if="chunkFailure"
        role="alert"
        class="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800"
      >
        <div class="flex items-start gap-2.5">
          <AppIcon name="alert" class="mt-0.5 h-5 w-5 shrink-0" />
          <div class="min-w-0">
            <p class="font-semibold">
              {{
                t('inspector.wizard.step2.chunkFailure.title', {
                  number: chunkFailure.chunk.number,
                  total: progress?.totalChunks,
                })
              }}
            </p>
            <p class="mt-1">{{ chunkFailure.message }}</p>
            <p class="mt-1">
              {{ t('inspector.wizard.step2.chunkFailure.stopped') }}
              {{ t('inspector.wizard.step2.chunkFailure.stored', wizard.storedFrames) }}
              {{ t('inspector.wizard.step2.chunkFailure.selected', selected.length) }}
            </p>
          </div>
        </div>
      </div>

      <section class="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div class="min-w-0">
            <h2 class="text-lg font-semibold text-slate-900">
              {{ t('inspector.wizard.step2.title') }}
            </h2>
            <p class="mt-1 text-sm text-slate-500">
              {{
                t('inspector.wizard.step2.help', {
                  size: formatBytes(MAX_BYTES_PER_FILE),
                  files: UPLOAD_CHUNK_MAX_FILES,
                  concurrency: UPLOAD_CONCURRENCY,
                })
              }}
            </p>
          </div>
          <span
            v-if="wizard.needs3dModel"
            class="inline-flex items-center gap-1.5 rounded-full bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700 ring-1 ring-blue-200 ring-inset"
          >
            <AppIcon name="cube" class="h-3.5 w-3.5" />
            {{ t('inspector.wizard.step2.queued3d') }}
          </span>
        </div>

        <!-- Drop zone -->
        <div
          class="mt-5 flex cursor-pointer flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed px-6 py-10 text-center transition"
          :class="
            dragging
              ? 'border-blue-500 bg-blue-50'
              : 'border-slate-300 bg-slate-50 hover:border-blue-400 hover:bg-blue-50/40'
          "
          :aria-busy="uploading"
          @click="openFiles"
          @dragover.prevent="dragging = true"
          @dragenter.prevent="dragging = true"
          @dragleave="onDragLeave"
          @drop.prevent="onDrop"
        >
          <span
            class="inline-flex h-12 w-12 items-center justify-center rounded-full transition"
            :class="dragging ? 'bg-blue-600 text-white' : 'bg-white text-slate-400 ring-1 ring-slate-200'"
          >
            <AppIcon name="upload" class="h-6 w-6" />
          </span>
          <p class="text-base font-semibold text-slate-900">
            {{
              dragging
                ? t('inspector.wizard.step2.drop.active')
                : t('inspector.wizard.step2.drop.idle')
            }}
          </p>
          <p class="text-sm text-slate-500">{{ t('inspector.wizard.step2.drop.or') }}</p>
          <div class="flex flex-wrap items-center justify-center gap-2">
            <button
              type="button"
              class="inline-flex items-center gap-2 rounded-lg bg-white px-3.5 py-2 text-sm font-semibold text-slate-700 ring-1 ring-slate-300 transition hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
              :disabled="uploading"
              @click.stop="openFiles"
            >
              <AppIcon name="image" class="h-4 w-4" />
              {{ t('inspector.wizard.step2.drop.files') }}
            </button>
            <button
              type="button"
              class="inline-flex items-center gap-2 rounded-lg bg-white px-3.5 py-2 text-sm font-semibold text-slate-700 ring-1 ring-slate-300 transition hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
              :disabled="uploading"
              @click.stop="openFolder"
            >
              <AppIcon name="folder" class="h-4 w-4" />
              {{ t('inspector.wizard.step2.drop.folder') }}
            </button>
          </div>
          <!--
            `@click.stop` on both hidden inputs: a programmatic `.click()`
            bubbles like any other click, so without it opening *this* input from
            a button would run the drop zone's own `openFiles` and pop a second,
            file-shaped dialog on top of the first.
          -->
          <input
            ref="fileInput"
            type="file"
            class="hidden"
            multiple
            :accept="ACCEPTED_TYPES"
            :disabled="uploading"
            @click.stop
            @change="onSelect"
          />
          <!--
            The fallback folder picker. No `accept`: it filters *files*, and a
            directory dialog with a file filter is exactly the kind of thing that
            makes a browser offer a file chooser instead. Everything chosen is
            validated by `addFiles` anyway.
          -->
          <input
            ref="folderInput"
            type="file"
            class="hidden"
            multiple
            webkitdirectory
            directory
            :disabled="uploading"
            @click.stop
            @change="onSelect"
          />
        </div>

        <p v-if="scanning" class="mt-3 flex items-center gap-2 text-sm text-slate-500" role="status">
          <span class="h-4 w-4 animate-spin rounded-full border-2 border-slate-200 border-t-slate-500" aria-hidden="true" />
          {{ t('inspector.wizard.step2.drop.scanning') }}
        </p>

        <!-- Rejected files, grouped: one line per reason, not one per file. -->
        <div
          v-if="rejectionGroups.length > 0"
          class="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900"
        >
          <div class="flex items-start gap-2.5">
            <AppIcon name="alert" class="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
            <div class="min-w-0">
              <p class="font-semibold">
                {{ t('inspector.wizard.step2.rejected.title', rejections.length) }}
              </p>
              <ul class="mt-1.5 space-y-1">
                <li v-for="group in rejectionGroups" :key="group.key" class="break-words">
                  <span class="font-medium">{{ group.count }}</span> —
                  {{ t(group.reasonKey, group.params) }}
                  <span class="text-xs text-amber-800">
                    ({{ group.examples.join(', ') }}<span v-if="group.count > group.examples.length">, …</span>)
                  </span>
                </li>
              </ul>
              <p v-if="ignoredNoise > 0" class="mt-1.5 text-xs text-amber-800">
                {{ t('inspector.wizard.step2.rejected.noise', ignoredNoise) }}
              </p>
            </div>
            <button
              type="button"
              class="ml-auto shrink-0 rounded-lg p-1 opacity-60 transition hover:opacity-100"
              :aria-label="t('inspector.wizard.step2.rejected.dismiss')"
              @click="rejections = []"
            >
              <AppIcon name="close" class="h-4 w-4" />
            </button>
          </div>
        </div>

        <!-- Selection -->
        <div v-if="selected.length > 0" class="mt-5">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <p class="text-sm font-medium text-slate-900">
              {{ t('inspector.wizard.step2.selection.files', selected.length) }} ·
              {{ formatBytes(totalBytes) }} ·
              {{ t('inspector.wizard.step2.selection.batches', batches) }}
            </p>
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm font-medium text-slate-600 transition hover:bg-slate-100 hover:text-red-700 disabled:opacity-50"
              :disabled="uploading"
              @click="clearSelection"
            >
              <AppIcon name="trash" class="h-4 w-4" />
              {{ t('inspector.wizard.step2.selection.clear') }}
            </button>
          </div>

          <ul class="mt-3 max-h-64 divide-y divide-slate-100 overflow-y-auto rounded-xl border border-slate-200">
            <li
              v-for="(file, index) in visibleSelected"
              :key="`${file.name}-${file.size}-${file.lastModified}`"
              class="flex items-center gap-3 px-3 py-2.5"
            >
              <AppIcon name="image" class="h-4 w-4 shrink-0 text-slate-400" />
              <!-- A folder selection keeps its subpath; a folder *drop* does not
                   (the entry API gives bare names). Server-side only the basename
                   is stored either way. -->
              <span class="min-w-0 flex-1 truncate text-sm text-slate-800" :title="file.name">
                {{ file.webkitRelativePath || file.name }}
              </span>
              <span class="shrink-0 text-xs text-slate-500 tabular-nums">{{ formatBytes(file.size) }}</span>
              <button
                type="button"
                class="shrink-0 rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-red-700 disabled:opacity-50"
                :disabled="uploading"
                :aria-label="t('inspector.wizard.step2.selection.remove', { name: file.name })"
                @click="removeFile(index)"
              >
                <AppIcon name="close" class="h-3.5 w-3.5" />
              </button>
            </li>
          </ul>
          <p v-if="hiddenSelected > 0" class="mt-2 text-xs text-slate-500">
            {{
              t(
                'inspector.wizard.step2.selection.hidden',
                { limit: LIST_LIMIT },
                hiddenSelected,
              )
            }}
          </p>
        </div>

        <!-- Upload progress: batches, not a spinner. -->
        <div v-if="uploading && progress" class="mt-5 rounded-xl border border-slate-200 p-4">
          <div class="flex flex-wrap items-end justify-between gap-3">
            <p class="text-sm font-medium text-slate-900">
              {{
                t('inspector.wizard.step2.progress.files', {
                  done: progress.completedFiles,
                  total: progress.totalFiles,
                })
              }}
            </p>
            <p class="text-xs text-slate-500">
              {{
                t('inspector.wizard.step2.progress.batches', {
                  done: progress.completedChunks,
                  total: progress.totalChunks,
                })
              }}<span v-if="progress.inFlight > 0">
                · {{ t('inspector.wizard.step2.progress.inFlight', progress.inFlight) }}</span
              >
            </p>
          </div>
          <div
            class="mt-3 h-2.5 w-full overflow-hidden rounded-full bg-slate-100 ring-1 ring-slate-200 ring-inset"
            role="progressbar"
            :aria-valuenow="progressPercent"
            aria-valuemin="0"
            aria-valuemax="100"
            :aria-label="
              t('inspector.wizard.step2.progress.aria', {
                done: progress.completedFiles,
                total: progress.totalFiles,
              })
            "
          >
            <div
              class="h-full bg-blue-600 transition-all duration-300"
              :style="{ width: `${progressPercent}%` }"
            />
          </div>
          <button
            type="button"
            class="mt-3 inline-flex items-center gap-2 rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100"
            @click="cancelUpload"
          >
            <AppIcon name="close" class="h-4 w-4" />
            {{ t('inspector.wizard.step2.progress.cancel') }}
          </button>
        </div>

        <!-- What the server already holds from this session. -->
        <p
          v-if="!uploading && wizard.storedFrames > 0 && !queueFailure && !chunkFailure"
          class="mt-5 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900"
        >
          <span class="font-medium">
            {{ t('inspector.wizard.step2.stored.title', wizard.storedFrames) }}
          </span>
          {{
            wizard.isQueued
              ? t('inspector.wizard.step2.stored.queued')
              : t('inspector.wizard.step2.stored.notQueued')
          }}
        </p>

        <!-- Actions -->
        <div class="mt-7 flex flex-wrap items-center justify-end gap-3 border-t border-slate-100 pt-5">
          <button
            type="button"
            :disabled="!canUpload"
            class="inline-flex items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:bg-blue-400"
            @click="upload"
          >
            <span
              v-if="starting"
              class="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
              aria-hidden="true"
            />
            <AppIcon v-else :name="selected.length === 0 && wizard.storedFrames > 0 ? 'refresh' : 'upload'" class="h-4 w-4" />
            {{ uploadLabel }}
          </button>
        </div>
      </section>
    </template>
  </div>
</template>
