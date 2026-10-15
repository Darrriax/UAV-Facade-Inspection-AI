<script setup lang="ts">
/**
 * Wizard step 3: the pipeline's progress.
 *
 * Polls one endpoint every few seconds: the status counters
 * (`GET /projects/{id}/images/stats` - a single aggregate query, not a page of
 * rows). There is deliberately no per-frame strip here any more: a project can
 * hold thousands of frames, so a page of coloured chips can only ever be a
 * partial picture next to counters that are exact.
 *
 * Polling stops when the pipeline is done, when the project is stopped and while
 * the tab is hidden; coming back to the tab refreshes immediately. A failed
 * request does *not* stop it: the screen retries on the next tick and shows the
 * error inline, reporting it **once per outage** rather than once per attempt -
 * so a transient blip costs one message instead of a wall of them, and a screen
 * nobody can nudge back to life does not have to be nudged.
 *
 * This is also the only screen that can stop the pipeline, and the only one that
 * can start it again. Both are ordinary API calls: stopping raises a flag the
 * worker reads between frames, and resuming is `POST /start-processing`, which
 * clears that flag and queues the frames the stopped run handed back. Nothing is
 * lost either way - the frames already analysed keep their results.
 *
 * While the project is stopped, polling is switched off on purpose: the state
 * now changes only when the user acts, so a timer that refreshes every 4 s would
 * be a request every 4 s that can never have news.
 */

import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, useRoute } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import { describeApiError } from '@/api/client'
import { getProjectImageStats, startProjectProcessing, stopProjectProcessing } from '@/api/projects'
import { ROUTE_NAMES } from '@/router/routes/names'
import { useToasts } from '@/composables/useToasts'
import { useWizardStore } from '@/stores/wizard'
import { IMAGE_STATUS_ORDER, type ImageStatus, type ProjectImageStats } from '@/types/api'
import { formatTime } from '@/utils/format'

/** 4 s: fast enough to feel live, slow enough to leave the API alone. */
const POLL_MS = 4_000
/** Roughly 32 s of no change before the "is the worker running?" hint appears. */
const UNCHANGED_POLLS_BEFORE_HINT = 8

/**
 * The four frame states, as catalogue keys rather than copy.
 *
 * The keys are the workspace's *on purpose*: a frame that is `processing` reads
 * the same here, on the carousel strip and in the frame status chip, because it
 * is one state of one row - a second set of labels is how the same state starts
 * being called "Processing" on one screen and "Analysing" on another.
 */
const STATUS_META: Record<ImageStatus, { labelKey: string; tile: string; dot: string }> = {
  pending: {
    labelKey: 'inspector.workspace.frame.status.pending',
    tile: 'border-slate-200 bg-white',
    dot: 'bg-status-pending',
  },
  processing: {
    labelKey: 'inspector.workspace.frame.status.processing',
    tile: 'border-blue-200 bg-blue-50/60',
    dot: 'bg-status-processing',
  },
  completed: {
    labelKey: 'inspector.workspace.frame.status.completed',
    tile: 'border-emerald-200 bg-emerald-50/60',
    dot: 'bg-status-completed',
  },
  failed: {
    labelKey: 'inspector.workspace.frame.status.failed',
    tile: 'border-red-200 bg-red-50/60',
    dot: 'bg-status-failed',
  },
}

const route = useRoute()
const wizard = useWizardStore()
const toast = useToasts()
const { t } = useI18n()

const stats = ref<ProjectImageStats | null>(null)
const lastUpdated = ref<Date | null>(null)
const pollError = ref<string | null>(null)
/** Which pipeline button has a request in flight, so both can be disabled. */
const busyAction = ref<'stop' | 'resume' | null>(null)

/**
 * The interval handle, as a ref.
 *
 * It has to be reactive: `isPolling` is derived from it and the status line is
 * rendered from `isPolling`. A plain variable would be invisible to the
 * reactivity system, and a `computed` with no reactive dependency is evaluated
 * once and cached forever - so the label would freeze on whatever it said first.
 */
const timer = ref<number | null>(null)
let controller: AbortController | null = null
let inFlight = false
let unchangedPolls = 0
let previousSignature = ''
/** True while the last poll attempt failed, so an outage is reported once. */
let reportedFailure = false

const projectId = computed<string | null>(() => {
  const raw = route.params.projectId
  const fromRoute = Array.isArray(raw) ? raw[0] : raw
  return typeof fromRoute === 'string' && fromRoute.length > 0 ? fromRoute : wizard.projectId
})

const loadFailed = computed(() => wizard.status === 'error')
const total = computed(() => stats.value?.total ?? 0)
const processed = computed(() => (stats.value?.completed ?? 0) + (stats.value?.failed ?? 0))
const percent = computed(() => (total.value === 0 ? 0 : Math.round((processed.value / total.value) * 100)))
/** Terminal: nothing left to wait for. Also true for an empty project. */
const isSettled = computed(
  () => stats.value !== null && stats.value.pending === 0 && stats.value.processing === 0,
)
const hasFailures = computed(() => (stats.value?.failed ?? 0) > 0)
const isPolling = computed(() => timer.value !== null)

/**
 * The user has stopped this project.
 *
 * Read from the project the API returned, not from local component state: the
 * flag is what the worker obeys, so the screen has to agree with it rather than
 * with what a click did. `loadProject` keeps it fresh across a reload, and the
 * stop/resume handlers adopt the server's answer deliberately.
 */
const isPaused = computed(() => wizard.project?.processing_stopped_at != null)
/** Frames a resume would actually pick up. */
const remaining = computed(() => (stats.value?.pending ?? 0) + (stats.value?.processing ?? 0))
/** Offered only while there is something to interrupt. */
const canStop = computed(() => total.value > 0 && !isSettled.value && !isPaused.value)
/** A stopped project with no frames left has nothing to resume. */
const canResume = computed(() => isPaused.value && remaining.value > 0)
/**
 * Frames this session stored but never queued for analysis.
 *
 * Reachable honestly: the trigger is its own call now, so a broker outage, a
 * closed tab or a failed `POST /start-processing` all leave stored frames that
 * nothing is going to pick up. Saying so is better than an empty progress bar.
 */
const unqueuedFrames = computed(() => wizard.storedFrames > 0 && !wizard.isQueued)
const stalledHint = computed(
  () =>
    !isSettled.value &&
    !isPaused.value &&
    total.value > 0 &&
    unchangedPolls >= UNCHANGED_POLLS_BEFORE_HINT,
)

/**
 * The sentence under the title, chosen by what this project actually produces.
 *
 * Three combinations are reachable: detection only, detection plus the 3D model,
 * and the 3D model alone (`detect_defects` off). The last one is why this is a
 * keyed lookup and not a ternary - a screen promising YOLO per frame for a
 * project that will never load the detector reads as a broken pipeline.
 */
const helpKey = computed(() => {
  if (!wizard.detectDefects) return 'inspector.wizard.step3.helpNoDetection'
  return wizard.needs3dModel
    ? 'inspector.wizard.step3.help3d'
    : 'inspector.wizard.step3.help'
})

const counters = computed(() =>
  IMAGE_STATUS_ORDER.map((status) => ({
    status,
    label: t(STATUS_META[status].labelKey),
    count: stats.value?.[status] ?? 0,
    tile: STATUS_META[status].tile,
    dot: STATUS_META[status].dot,
  })),
)

// ------------------------------------------------------------------- polling
async function refresh(): Promise<void> {
  const id = projectId.value
  if (id === null || inFlight) return

  inFlight = true
  controller = new AbortController()

  try {
    const counts = await getProjectImageStats(id, controller.signal)

    const signature = JSON.stringify(counts)
    unchangedPolls = signature === previousSignature ? unchangedPolls + 1 : 0
    previousSignature = signature

    stats.value = counts
    lastUpdated.value = new Date()
    pollError.value = null

    reportedFailure = false
    if (isSettled.value) stopPolling()
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') return
    pollError.value = describeApiError(cause)
    // Keep the interval running: the screen has no manual refresh button any
    // more, and one failed request must not leave it stale forever. What is
    // reported is the *outage*, not the attempt - the inline error stays on
    // screen for as long as it lasts.
    if (!reportedFailure) {
      reportedFailure = true
      toast.error(t('inspector.wizard.step3.toast.pollFailed'), pollError.value)
    }
  } finally {
    inFlight = false
    controller = null
  }
}

/**
 * Fetch immediately and resume the interval.
 *
 * Called by the two things that know the state has just changed: the tab
 * becoming visible again, and a resume the server has accepted. There is no
 * button for it - the timer covers the ordinary case.
 */
async function refreshNow(): Promise<void> {
  await refresh()
  if (!isSettled.value) startPolling()
}

function startPolling(): void {
  // A stopped project has nothing to report until it is resumed, so the timer
  // stays off; `refreshNow` is what wakes the screen up again.
  if (timer.value !== null || isPaused.value) return
  timer.value = window.setInterval(() => void refresh(), POLL_MS)
}

function stopPolling(): void {
  if (timer.value === null) return
  window.clearInterval(timer.value)
  timer.value = null
}

/** Do not poll a tab nobody is looking at; refresh the moment it comes back. */
function onVisibilityChange(): void {
  if (document.visibilityState === 'hidden') {
    stopPolling()
  } else if (!isSettled.value) {
    void refreshNow()
  }
}

// ------------------------------------------------------------ pipeline control
/**
 * Stop the analysis.
 *
 * Fire-and-return: the API raises a flag and the worker notices it between
 * frames, so the frame in flight always finishes and the counters can keep
 * moving for a few seconds after this resolves. The screen says that out loud
 * rather than claiming the batch stopped at the instant of the click.
 */
async function stopProcessing(): Promise<void> {
  const id = projectId.value
  if (id === null || busyAction.value !== null) return

  busyAction.value = 'stop'
  try {
    // The response is the updated project, so the paused state comes from the
    // server rather than from an optimistic local flip.
    wizard.remember(await stopProjectProcessing(id))
    stopPolling()
    await refresh()
    toast.info(
      t('inspector.wizard.step3.toast.stopRequested'),
      t('inspector.wizard.step3.toast.stopBody'),
    )
  } catch (cause) {
    toast.error(t('inspector.wizard.step3.toast.stopFailed'), describeApiError(cause))
  } finally {
    busyAction.value = null
  }
}

/**
 * Resume: clear the stop flag and queue whatever is still pending.
 *
 * Deliberately the same call the wizard makes after an upload - one endpoint
 * queues work, so there is exactly one place that has to clear the flag first.
 */
async function resumeProcessing(): Promise<void> {
  const id = projectId.value
  if (id === null || busyAction.value !== null) return

  busyAction.value = 'resume'
  try {
    const started = await startProjectProcessing(id)
    if (started.task_id !== null) wizard.markQueued(started.task_id)
    // Re-read the project: `processing_stopped_at` is what the paused state and
    // this button are drawn from, and only the server can confirm it is clear.
    await wizard.loadProject(id, { force: true })
    await refreshNow()
    toast.success(
      t('inspector.wizard.step3.toast.resumed'),
      t('inspector.wizard.step3.toast.resumedBody', started.pending_images),
    )
  } catch (cause) {
    toast.error(t('inspector.wizard.step3.toast.resumeFailed'), describeApiError(cause))
  } finally {
    busyAction.value = null
  }
}

onMounted(async () => {
  const id = projectId.value
  if (id !== null) await wizard.loadProject(id)

  await refresh()
  if (!isSettled.value) startPolling()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
  controller?.abort()
})
</script>

<template>
  <div class="space-y-5">
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
      <!-- Stored but never queued: worth saying before the user waits on a bar. -->
      <div
        v-if="unqueuedFrames"
        class="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900"
      >
        <div class="flex items-start gap-2.5">
          <AppIcon name="alert" class="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
          <div>
            <p class="font-semibold">
              {{ t('inspector.wizard.step3.unqueued.title', wizard.storedFrames) }}
            </p>
            <p class="mt-1">{{ t('inspector.wizard.step3.unqueued.hint') }}</p>
            <p class="mt-1">{{ t('inspector.wizard.step3.unqueued.hintAction') }}</p>
          </div>
        </div>
      </div>

      <!--
        Stopped by the user. Explains three things a bare badge cannot: who did
        it, what is still waiting, and that nothing was thrown away - all of
        which are the questions this state raises.
      -->
      <div
        v-if="isPaused"
        class="rounded-xl border border-violet-200 bg-violet-50 p-4 text-sm text-violet-900"
      >
        <div class="flex items-start gap-2.5">
          <AppIcon name="pause" class="mt-0.5 h-5 w-5 shrink-0 text-violet-600" />
          <div>
            <p class="font-semibold">{{ t('inspector.wizard.step3.paused.title') }}</p>
            <p class="mt-1">
              {{
                remaining > 0
                  ? t('inspector.wizard.step3.paused.remaining', remaining)
                  : t('inspector.wizard.step3.paused.everyFrame')
              }}
              {{ t('inspector.wizard.step3.paused.kept') }}
            </p>
          </div>
        </div>
      </div>

      <section class="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div class="min-w-0">
            <h2 class="text-lg font-semibold text-slate-900">
              {{ t('inspector.wizard.step3.title') }}
            </h2>
            <p class="mt-1 text-sm text-slate-500">{{ t(helpKey) }}</p>
          </div>
          <span
            v-if="!wizard.detectDefects"
            class="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-700 ring-1 ring-amber-200 ring-inset"
          >
            <AppIcon name="eyeOff" class="h-3.5 w-3.5" />
            {{ t('inspector.wizard.step3.modeNoDetection') }}
          </span>
          <span
            v-else-if="wizard.needs3dModel"
            class="inline-flex items-center gap-1.5 rounded-full bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700 ring-1 ring-blue-200 ring-inset"
          >
            <AppIcon name="cube" class="h-3.5 w-3.5" />
            {{ t('inspector.wizard.step3.mode3d') }}
          </span>
          <span
            v-else
            class="inline-flex items-center gap-1.5 rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600 ring-1 ring-slate-200 ring-inset"
          >
            <AppIcon name="grid" class="h-3.5 w-3.5" />
            {{ t('inspector.wizard.step3.mode2d') }}
          </span>
        </div>

        <!-- Empty project: nothing is coming, so say so instead of polling forever. -->
        <div
          v-if="stats !== null && total === 0"
          class="mt-6 flex flex-col items-center gap-3 rounded-xl border border-dashed border-slate-300 bg-slate-50 px-6 py-10 text-center"
        >
          <span class="inline-flex h-11 w-11 items-center justify-center rounded-full bg-white ring-1 ring-slate-200">
            <AppIcon name="image" class="h-5 w-5 text-slate-400" />
          </span>
          <div>
            <p class="font-semibold text-slate-900">
              {{ t('inspector.wizard.step3.empty.title') }}
            </p>
            <p class="mt-1 text-sm text-slate-500">
              {{ t('inspector.wizard.step3.empty.body') }}
            </p>
          </div>
          <RouterLink
            :to="{ name: ROUTE_NAMES.projectPhotos, params: { projectId: projectId ?? '' } }"
            class="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-blue-700"
          >
            <AppIcon name="upload" class="h-4 w-4" />
            {{ t('inspector.wizard.step3.empty.action') }}
          </RouterLink>
        </div>

        <template v-else-if="stats !== null">
          <!-- Progress bar -->
          <div class="mt-6">
            <div class="flex items-end justify-between gap-3">
              <p class="text-sm text-slate-600">
                <span class="text-2xl font-semibold text-slate-900 tabular-nums">{{ percent }}%</span>
                <span class="ml-2">
                  {{ t('inspector.wizard.step3.progress.frames', { processed, total }) }}
                </span>
              </p>
              <p class="text-xs text-slate-500">
                <span v-if="isSettled" class="inline-flex items-center gap-1.5 font-medium text-emerald-700">
                  <AppIcon name="check" class="h-3.5 w-3.5" />
                  {{ t('inspector.wizard.step3.state.finished') }}
                </span>
                <span v-else-if="isPaused" class="inline-flex items-center gap-1.5 font-medium text-violet-700">
                  <AppIcon name="pause" class="h-3.5 w-3.5" />
                  {{ t('inspector.wizard.step3.state.stopped') }}
                </span>
                <!--
                  Reported from the real timer, not from "not settled yet":
                  polling also stops while the tab is hidden, and a screen
                  claiming to refresh every 4 s while doing nothing is worse
                  than saying it is paused. A failed request is deliberately not
                  one of these cases - it keeps retrying (see `refresh`).
                -->
                <span v-else-if="isPolling" class="inline-flex items-center gap-1.5">
                  <AppIcon name="refresh" class="h-3.5 w-3.5 animate-spin text-slate-400" />
                  {{ t('inspector.wizard.step3.state.refreshing', { seconds: POLL_MS / 1000 }) }}
                </span>
                <span v-else class="inline-flex items-center gap-1.5 font-medium text-amber-700">
                  <AppIcon name="clock" class="h-3.5 w-3.5" />
                  {{ t('inspector.wizard.step3.state.paused') }}
                </span>
              </p>
            </div>

            <div
              class="mt-3 h-3 w-full overflow-hidden rounded-full bg-slate-100 ring-1 ring-slate-200 ring-inset"
              role="progressbar"
              :aria-valuenow="percent"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-label="t('inspector.wizard.step3.progress.frames', { processed, total })"
            >
              <div class="flex h-full w-full">
                <div
                  v-if="stats.failed > 0"
                  class="h-full bg-status-failed transition-all duration-500"
                  :style="{ width: `${(stats.failed / total) * 100}%` }"
                />
                <div
                  class="h-full bg-status-completed transition-all duration-500"
                  :style="{ width: `${(stats.completed / total) * 100}%` }"
                />
              </div>
            </div>

            <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
              <span v-if="lastUpdated">
                {{ t('inspector.wizard.step3.progress.updatedAt', { time: formatTime(lastUpdated) }) }}
              </span>
              <span v-if="wizard.isQueued" class="inline-flex items-center gap-1.5">
                <AppIcon name="check" class="h-3.5 w-3.5 text-emerald-600" />
                {{ t('inspector.wizard.step3.progress.queuedAs', wizard.storedFrames) }}
                <code class="rounded bg-slate-100 px-1.5 py-0.5 font-mono">
                  {{ wizard.upload.taskId?.slice(0, 8) }}…
                </code>
              </span>
              <span v-if="pollError" class="font-medium text-red-600">{{ pollError }}</span>
              <!--
                No "Refresh now" button: this screen polls on its own, retries a
                failed request on the next tick, and refreshes the moment the tab
                becomes visible again - a button that only duplicated the timer
                was one more thing on screen for the user to wonder about.
              -->
            </div>
          </div>

          <!-- Counters -->
          <dl class="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div
              v-for="counter in counters"
              :key="counter.status"
              class="rounded-xl border p-4"
              :class="counter.tile"
            >
              <dt class="flex items-center gap-2 text-xs font-medium tracking-wide text-slate-600 uppercase">
                <span class="h-2 w-2 rounded-full" :class="counter.dot" />
                {{ counter.label }}
              </dt>
              <dd class="mt-2 text-2xl font-semibold text-slate-900 tabular-nums">{{ counter.count }}</dd>
            </div>
          </dl>

          <!-- Failure note -->
          <p v-if="hasFailures" class="mt-4 text-sm text-red-700">
            {{ t('inspector.wizard.step3.failures.lead', stats.failed) }}
            {{ t('inspector.wizard.step3.failures.hint') }}
          </p>

          <!-- Worker hint -->
          <p v-if="stalledHint" class="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">
            {{
              t('inspector.wizard.step3.stalled.text', {
                seconds: (UNCHANGED_POLLS_BEFORE_HINT * POLL_MS) / 1000,
              })
            }}
            <code class="rounded bg-white px-1.5 py-0.5 font-mono text-xs ring-1 ring-slate-200">docker compose ps</code>
            <span aria-hidden="true">·</span>
            <code class="rounded bg-white px-1.5 py-0.5 font-mono text-xs ring-1 ring-slate-200">docker compose logs worker</code>
          </p>

        </template>

        <!-- First poll still in flight. -->
        <p v-else class="mt-6 flex items-center gap-2 text-sm text-slate-500" role="status">
          <span class="h-4 w-4 animate-spin rounded-full border-2 border-slate-200 border-t-slate-500" aria-hidden="true" />
          {{ t('inspector.wizard.step3.reading') }}
        </p>

        <!--
          Actions.

          No "upload more frames" link here on purpose: adding photos is the
          second step of the stepper, and the user reaches it by clicking it -
          one route back to the upload screen instead of two.
        -->
        <div class="mt-7 flex flex-wrap items-center justify-end gap-3 border-t border-slate-100 pt-5">
          <div class="flex flex-wrap items-center gap-2">
            <!--
              Stop is offered only while there is something to interrupt, and is
              a *secondary* action: the destructive-looking button next to the
              primary "go and review the results" one would be the wrong default.
            -->
            <button
              v-if="canStop"
              type="button"
              :disabled="busyAction !== null"
              class="inline-flex items-center justify-center gap-2 rounded-lg border border-amber-300 bg-amber-50 px-4 py-2.5 text-sm font-semibold text-amber-800 transition hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-60"
              @click="stopProcessing"
            >
              <span
                v-if="busyAction === 'stop'"
                class="h-4 w-4 animate-spin rounded-full border-2 border-amber-300 border-t-amber-700"
                aria-hidden="true"
              />
              <AppIcon v-else name="pause" class="h-4 w-4" />
              {{
                busyAction === 'stop'
                  ? t('inspector.wizard.step3.actions.stopping')
                  : t('inspector.wizard.step3.actions.stop')
              }}
            </button>

            <button
              v-if="canResume"
              type="button"
              :disabled="busyAction !== null"
              class="inline-flex items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
              @click="resumeProcessing"
            >
              <span
                v-if="busyAction === 'resume'"
                class="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
                aria-hidden="true"
              />
              <AppIcon v-else name="play" class="h-4 w-4" />
              {{
                busyAction === 'resume'
                  ? t('inspector.wizard.step3.actions.resuming')
                  : t('inspector.wizard.step3.actions.resume', { n: remaining })
              }}
            </button>

            <RouterLink
              v-if="isSettled && total > 0"
              :to="{ name: ROUTE_NAMES.projectWorkspace, params: { projectId: projectId ?? '' } }"
              class="inline-flex items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
            >
              <AppIcon name="cube" class="h-4 w-4" />
              {{ t('inspector.wizard.step3.actions.workspace') }}
              <AppIcon name="arrowRight" class="h-4 w-4" />
            </RouterLink>

            <button
              v-else-if="!canStop && !canResume"
              type="button"
              disabled
              class="inline-flex cursor-not-allowed items-center justify-center gap-2 rounded-lg bg-slate-200 px-4 py-2.5 text-sm font-semibold text-slate-500"
              :title="t('inspector.wizard.step3.actions.waitingHint')"
            >
              <AppIcon name="clock" class="h-4 w-4" />
              {{ t('inspector.wizard.step3.actions.waiting') }}
            </button>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>
