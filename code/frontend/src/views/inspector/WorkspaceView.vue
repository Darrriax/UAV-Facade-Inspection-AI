<script setup lang="ts">
/**
 * The project's defect review workspace: the photo in the middle, the findings
 * the model reported in the panel beside it.
 *
 * Four decisions shape this screen.
 *
 * **The frame is the unit of review, and every frame is here.** The carousel
 * walks the whole sortie, so a photo the model found nothing on is reachable and
 * reads as "clean" rather than as a missing row. Frames the worker has not
 * finished with say so instead of looking empty.
 *
 * **The panel and the canvas talk about objects, not detections.** A model run
 * at a low confidence threshold reports one crack several times; `mergeDetections`
 * folds the detections that share an `object_index` into a single object - an
 * answer the server already gave when it analysed the frame, not a calculation
 * repeated here - and both halves of the screen render that result: one outline
 * on the photo, one row in the list. The confirm and delete actions still apply
 * to the detections underneath, because the review record belongs to them.
 *
 * **Every counter on this screen counts objects.** The header's totals and the
 * per-class badges summarise the same units the panel lists and the project card
 * shows, so "34 defects" means thirty-four pieces of damage and not thirty-four
 * rows of model output.
 *
 * **The masks are the model's.** Every polygon drawn on the photo arrived with
 * the detection it belongs to; this screen reads them and writes nothing to
 * them. The review decisions the panel owns - confirm a detection, clear that
 * confirmation, delete a false positive - are the only writes, and the server's
 * answer replaces the row rather than the client guessing at it, because the
 * detection list is what the project's statistics are computed from.
 *
 * **View state lives in the URL where it is shareable.** `?frame=<image id>` and
 * `?defect=<defect id>` are query parameters, so the back button works and an
 * inspector can link a colleague to one specific crack. The class, the armed
 * tool and the annotated/plain switch are local UI state and deliberately not in
 * the URL.
 *
 * **A 3D project gets a switch, not a second panel.** The reconstruction viewer
 * is a page of its own, reached from the header switch, and the switch only
 * appears when the project asked for a model. Either way this screen keeps the
 * whole viewport for the photo.
 */

import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import type { LocationQueryRaw } from 'vue-router'

import { describeApiError } from '@/api/client'
import {
  deleteDefect,
  getProject,
  listAllProjectDefects,
  listAllProjectFrames,
  setDefectVerified,
} from '@/api/projects'
import AppIcon from '@/components/AppIcon.vue'
import AppSpinner from '@/components/AppSpinner.vue'
import ProjectViewHeader from '@/components/ProjectViewHeader.vue'
import FrameReviewer from '@/components/workspace/FrameReviewer.vue'
import { useToasts } from '@/composables/useToasts'
import { ROUTE_NAMES } from '@/router/routes/names'
import { useAuthStore } from '@/stores/auth'
import { DEFECT_CLASSES } from '@/types/api'
import type { Defect, DefectClass, Project, ReviewFrame } from '@/types/api'
import {
  defectClassStyle,
  formatConfidence,
  groupByFrame,
  mergeDetections,
  objectConfidence,
  objectVerified,
  type CanvasTool,
  type FrameGroup,
  type MaskObject,
} from '@/utils/defects'

/** The two classes the model can produce, in the order the panel lists them. */
const CLASS_ORDER: readonly DefectClass[] = [DEFECT_CLASSES.CRACK, DEFECT_CLASSES.SPALLING]

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const toasts = useToasts()
const { t } = useI18n()

const projectId = computed(() => String(route.params.projectId ?? ''))

const project = ref<Project | null>(null)
const defects = ref<Defect[]>([])
const reviewFrames = ref<ReviewFrame[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
/** Defect ids whose request (a verify toggle or a delete) is in flight. */
const busy = ref<string[]>([])

/**
 * Armed tool, for the pressed state of the two buttons.
 *
 * It selects nothing and edits nothing: the brush and the eraser are the
 * palette's two places, and the mask editing they stand for is not part of the
 * reviewer yet. `null` is the unarmed default.
 */
const tool = ref<CanvasTool>(null)
/** The class the panel has highlighted. */
const activeClass = ref<DefectClass>(DEFECT_CLASSES.CRACK)
/** False shows the plain photo instead of the annotated one. */
const annotated = ref(true)

// ------------------------------------------------------------------ derived
const frames = computed<FrameGroup[]>(() => groupByFrame(reviewFrames.value, defects.value))

/**
 * Every object in the project, frame by frame.
 *
 * Derived once and read by the header totals and the class badges: the two used
 * to count detection rows, which is one number for the model's output and quite
 * another for the damage on the building.
 */
const allObjects = computed<MaskObject[]>(() =>
  frames.value.flatMap((frame) => mergeDetections(frame.defects)),
)

const counts = computed(() => ({
  frames: frames.value.length,
  total: allObjects.value.length,
  verified: allObjects.value.filter(objectVerified).length,
}))

const classCounts = computed<Record<DefectClass, number>>(() => {
  const result: Record<DefectClass, number> = {
    [DEFECT_CLASSES.CRACK]: 0,
    [DEFECT_CLASSES.SPALLING]: 0,
  }
  for (const object of allObjects.value) result[object.defectClass] += 1
  return result
})

const selectedDefectId = computed<string | null>(() => {
  const value = route.query.defect
  return typeof value === 'string' ? value : null
})

const selectedDefect = computed<Defect | null>(
  () => defects.value.find((defect) => defect.id === selectedDefectId.value) ?? null,
)

/**
 * The frame on screen: the selected defect's, else the linked one, else the
 * first. A `?frame=` that points at a frame this project no longer has (or never
 * had) is ignored rather than rendered as an empty canvas.
 */
const activeImageId = computed<string | null>(() => {
  if (selectedDefect.value) return selectedDefect.value.image_id
  const linked = route.query.frame
  if (typeof linked === 'string' && frames.value.some((frame) => frame.imageId === linked)) {
    return linked
  }
  return frames.value[0]?.imageId ?? null
})

const activeIndex = computed(() => {
  const found = frames.value.findIndex((frame) => frame.imageId === activeImageId.value)
  return found === -1 ? 0 : found
})

const activeFrame = computed<FrameGroup | null>(() => frames.value[activeIndex.value] ?? null)

/** The visible frame's damage, merged into the objects an inspector reasons about. */
const maskObjects = computed<MaskObject[]>(() =>
  mergeDetections(activeFrame.value?.defects ?? []),
)

const canEdit = computed(() => auth.isInspector)

/**
 * The tool palette, in the order the panel shows it.
 *
 * One table rather than four hand-written buttons: the buttons differ only in
 * which tool they arm, and four near-identical blocks of markup is where the
 * labels and the pressed state start to drift apart.
 */
const TOOLS: readonly {
  id: Exclude<CanvasTool, null>
  labelKey: string
  icon: 'brush' | 'eraser'
}[] = [
  { id: 'brush', labelKey: 'inspector.workspace.tools.brush', icon: 'brush' },
  { id: 'erase', labelKey: 'inspector.workspace.tools.eraser', icon: 'eraser' },
]

// ------------------------------------------------------------------- loading
onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  const id = projectId.value
  if (!id) {
    error.value = t('inspector.workspace.missingId')
    loading.value = false
    return
  }

  try {
    const [loadedProject, loadedDefects, loadedFrames] = await Promise.all([
      getProject(id),
      listAllProjectDefects(id),
      listAllProjectFrames(id),
    ])
    project.value = loadedProject
    defects.value = loadedDefects
    reviewFrames.value = loadedFrames
  } catch (cause) {
    error.value = describeApiError(cause)
  } finally {
    loading.value = false
  }
}

/**
 * Re-read the detections without touching the rest of the screen.
 *
 * Used after a partial failure - a batch of confirmations where one request
 * failed leaves the client guessing about the others, and the server's answer is
 * the only trustworthy one. A failed refresh is not reported: the caller's
 * toast already explained the problem, and a second toast would only bury it.
 */
async function refreshDefects(): Promise<void> {
  try {
    defects.value = await listAllProjectDefects(projectId.value)
  } catch {
    // Intentionally best-effort; see above.
  }
}

// ------------------------------------------------------------------ actions
function updateQuery(patch: Record<string, string | undefined>): void {
  const query: LocationQueryRaw = { ...route.query }
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) delete query[key]
    else query[key] = value
  }
  void router.replace({ query })
}

function selectFrame(imageId: string): void {
  // Changing frame clears the defect focus: that mask is not on screen anymore.
  updateQuery({ frame: imageId, defect: undefined })
}

/**
 * Loads an object's frame *and* highlights it - used by both panels.
 *
 * The class list follows the selection: the highlighted class is the class of
 * the finding being looked at, which keeps the two halves of the panel agreeing
 * about which kind of damage is in view.
 */
function focusObject(object: MaskObject): void {
  const first = object.members[0]
  if (!first) return
  activeClass.value = object.defectClass
  updateQuery({ frame: first.image_id, defect: object.id })
}

function setClass(next: DefectClass): void {
  activeClass.value = next
}

function setTool(next: Exclude<CanvasTool, null>): void {
  if (!canEdit.value) return
  // A second click on the armed tool disarms it.
  tool.value = tool.value === next ? null : next
}

function setView(next: boolean): void {
  annotated.value = next
}

function objectBusy(object: MaskObject): boolean {
  return object.members.some((member) => busy.value.includes(member.id))
}

/**
 * An object's row carries the detection it acts on, class and confidence and all.
 *
 * Assembled from a message per action rather than concatenated in place, so a
 * language that orders the class and the confidence differently can say so.
 */
function objectActionLabel(object: MaskObject, action: 'confirm' | 'clear' | 'delete'): string {
  const label = t(defectClassStyle(object.defectClass).labelKey).toLowerCase()
  const confidence = formatConfidence(objectConfidence(object))
  return t(`inspector.workspace.objects.action.${action}`, { label, confidence })
}

/**
 * Remove an object: every detection it was merged from.
 *
 * One object on screen is one click, so it has to be one action - leaving any of
 * its fragments behind would redraw a mask the inspector just deleted.
 */
async function removeObject(object: MaskObject): Promise<void> {
  const ids = object.members.map((member) => member.id)
  if (ids.length === 0 || objectBusy(object)) return

  busy.value = [...busy.value, ...ids]
  try {
    await Promise.all(ids.map((id) => deleteDefect(projectId.value, id)))
    defects.value = defects.value.filter((item) => !ids.includes(item.id))
    // A deleted object must not stay focused in the URL.
    if (selectedDefectId.value !== null && ids.includes(selectedDefectId.value)) {
      updateQuery({ defect: undefined })
    }
    const source = object.members[0]?.image_file_path ?? t('inspector.workspace.thisFrame')
    toasts.success(
      t('inspector.workspace.toast.deleted'),
      ids.length > 1
        ? t(
            'inspector.workspace.toast.deletedManyBody',
            { n: ids.length, source },
            ids.length,
          )
        : t('inspector.workspace.toast.deletedBody', { source }),
    )
  } catch (cause) {
    toasts.error(t('inspector.workspace.toast.deleteFailed'), describeApiError(cause))
    await refreshDefects()
  } finally {
    busy.value = busy.value.filter((id) => !ids.includes(id))
  }
}

/** Confirm or clear a whole object: the detections it was merged from. */
async function toggleObjectVerified(object: MaskObject, isVerified: boolean): Promise<void> {
  const pending = object.members.filter((member) => member.is_verified !== isVerified)
  if (pending.length === 0 || objectBusy(object)) return

  const ids = pending.map((member) => member.id)
  busy.value = [...busy.value, ...ids]
  try {
    const updated = await Promise.all(
      ids.map((id) => setDefectVerified(projectId.value, id, isVerified)),
    )
    const byId = new Map(updated.map((item) => [item.id, item]))
    defects.value = defects.value.map((item) => byId.get(item.id) ?? item)
    toasts.success(
      isVerified
        ? t('inspector.workspace.toast.confirmed')
        : t('inspector.workspace.toast.cleared'),
      ids.length > 1
        ? t('inspector.workspace.toast.confirmedManyBody', ids.length)
        : t('inspector.workspace.toast.confirmedBody'),
    )
  } catch (cause) {
    toasts.error(t('inspector.workspace.toast.updateFailed'), describeApiError(cause))
    await refreshDefects()
  } finally {
    busy.value = busy.value.filter((id) => !ids.includes(id))
  }
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col bg-slate-100">
    <!-- Header: shared with the 3D viewer, so the two views of one project
         are framed by the same chrome. This screen counts its own totals from
         the rows it has loaded, because confirming a defect changes them. -->
    <ProjectViewHeader :project="project" view="defects" :counts="counts" />

    <!-- Screen-level states -->
    <div
      v-if="error"
      class="m-4 flex flex-col gap-3 rounded-xl border border-red-200 bg-red-50 px-5 py-4 sm:flex-row sm:items-center sm:justify-between"
      role="alert"
    >
      <p class="text-sm text-red-800">
        <span class="font-semibold">{{ t('inspector.workspace.loadError') }}</span> {{ error }}
      </p>
      <button
        type="button"
        class="shrink-0 rounded-lg border border-red-300 bg-white px-3.5 py-2 text-sm font-medium text-red-700 transition hover:bg-red-100"
        @click="load"
      >
        {{ t('common.tryAgain') }}
      </button>
    </div>

    <div v-else-if="loading" class="flex flex-1 items-center justify-center text-slate-500">
      <AppSpinner :label="t('inspector.workspace.loading')" />
    </div>

    <!--
      Split screen. The row itself never scrolls (`overflow-hidden` at every
      breakpoint): the panel and the canvas each own their scroll area, so the
      photo keeps its place while the object list is read to its end.
    -->
    <div v-else class="flex min-h-0 flex-1 flex-col overflow-hidden lg:flex-row">
      <!-- Annotation panel: classes, tools and the objects on the current frame. -->
      <aside
        class="flex max-h-[35vh] w-full shrink-0 flex-col overflow-hidden border-b border-slate-200 bg-white lg:max-h-none lg:w-80 lg:border-r lg:border-b-0"
        :aria-label="t('inspector.workspace.annotations')"
      >
        <div class="min-h-0 flex-1 space-y-5 overflow-y-auto p-3">
          <!-- Classes -->
          <section>
            <h2 class="text-xs font-semibold tracking-wide text-slate-500 uppercase">
              {{ t('inspector.workspace.classes') }}
            </h2>
            <ul class="mt-2 space-y-1">
              <li v-for="defectClass in CLASS_ORDER" :key="defectClass">
                <button
                  type="button"
                  class="flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-sm transition disabled:cursor-not-allowed disabled:opacity-60"
                  :class="
                    activeClass === defectClass
                      ? 'border-blue-400 bg-blue-50 text-slate-900'
                      : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                  "
                  :aria-pressed="activeClass === defectClass"
                  :disabled="!canEdit"
                  @click="setClass(defectClass)"
                >
                  <span
                    class="h-2.5 w-2.5 shrink-0 rounded-full"
                    :style="{ backgroundColor: defectClassStyle(defectClass).stroke }"
                  />
                  <span class="font-medium">
                    {{ t(defectClassStyle(defectClass).labelKey) }}
                  </span>
                  <span
                    class="ml-auto rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium tabular-nums text-slate-600"
                  >
                    {{ classCounts[defectClass] }}
                  </span>
                </button>
              </li>
            </ul>
          </section>

          <!--
            Tools. The palette, and nothing else: two labelled buttons rather
            than an icon-only strip, so "which one am I holding?" is answerable
            at a glance. What each one does is not implemented yet - the buttons
            arm and disarm.
          -->
          <section>
            <h2 class="text-xs font-semibold tracking-wide text-slate-500 uppercase">
              {{ t('inspector.workspace.tools.title') }}
            </h2>
            <div class="mt-2 grid grid-cols-2 gap-2">
              <button
                v-for="entry in TOOLS"
                :key="entry.id"
                type="button"
                class="inline-flex flex-col items-center gap-1 rounded-lg border px-2 py-2.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50"
                :class="
                  tool === entry.id
                    ? entry.id === 'erase'
                      ? 'border-rose-400 bg-rose-50 text-rose-800'
                      : 'border-blue-400 bg-blue-50 text-blue-800'
                    : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                "
                :aria-pressed="tool === entry.id"
                :disabled="!canEdit || !activeFrame"
                @click="setTool(entry.id)"
              >
                <AppIcon :name="entry.icon" class="h-4 w-4" />
                {{ t(entry.labelKey) }}
              </button>
            </div>
          </section>

          <!-- Objects on the frame currently on screen -->
          <section>
            <div class="flex items-center gap-2">
              <h2 class="text-xs font-semibold tracking-wide text-slate-500 uppercase">
                {{ t('inspector.workspace.objects.title') }}
              </h2>
              <span
                class="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium tabular-nums text-slate-600"
              >
                {{ maskObjects.length }}
              </span>
            </div>

            <!--
              The nudge to draw by hand belongs to the brush, not to the
              annotated/plain switch: flipping the overlay on is how somebody
              *looks* at a frame, and answering that with "paint over the
              damage" reads as an instruction they did not ask for. With the
              eraser armed the sentence would be wrong too, so it is the brush
              specifically.
            -->
            <p
              v-if="maskObjects.length === 0"
              class="mt-2 rounded-lg border border-dashed border-slate-300 px-3 py-4 text-center text-xs text-slate-500"
            >
              {{ t('inspector.workspace.objects.none') }}
              {{ canEdit && tool === 'brush' ? t('inspector.workspace.objects.noneHint') : '' }}
            </p>

            <ul v-else class="mt-2 space-y-1">
              <li v-for="object in maskObjects" :key="object.id" class="flex items-center gap-1.5">
                <!--
                  Two sibling buttons, never one nested in the other: an inspector
                  who tabs to "Confirm" must not walk to another object by
                  pressing it.
                -->
                <button
                  type="button"
                  class="flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2 py-1.5 text-left transition hover:bg-slate-50"
                  :class="
                    object.id === selectedDefectId ? 'bg-blue-50 ring-1 ring-blue-200 ring-inset' : ''
                  "
                  :aria-pressed="object.id === selectedDefectId"
                  @click="focusObject(object)"
                >
                  <span
                    class="inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset"
                    :class="defectClassStyle(object.defectClass).badge"
                  >
                    {{ t(defectClassStyle(object.defectClass).labelKey) }}
                  </span>
                  <span class="shrink-0 text-xs font-semibold tabular-nums text-slate-900">
                    {{ formatConfidence(objectConfidence(object)) }}
                  </span>
                  <span
                    v-if="objectVerified(object)"
                    class="ml-auto inline-flex shrink-0 items-center gap-1 text-[11px] font-medium text-emerald-700"
                  >
                    <AppIcon name="check" class="h-3 w-3" />
                    {{ t('inspector.workspace.objects.verified') }}
                  </span>
                  <span v-else class="ml-auto shrink-0 text-[11px] text-slate-400">
                    {{ t('inspector.workspace.objects.awaiting') }}
                  </span>
                </button>

                <button
                  v-if="canEdit"
                  type="button"
                  class="inline-flex shrink-0 items-center rounded-md border px-1.5 py-1.5 text-[11px] font-medium transition disabled:opacity-50"
                  :class="
                    objectVerified(object)
                      ? 'border-slate-300 bg-white text-slate-600 hover:bg-slate-100'
                      : 'border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100'
                  "
                  :disabled="objectBusy(object)"
                  :aria-label="
                    objectActionLabel(object, objectVerified(object) ? 'clear' : 'confirm')
                  "
                  @click="toggleObjectVerified(object, !objectVerified(object))"
                >
                  <AppIcon :name="objectVerified(object) ? 'close' : 'check'" class="h-3 w-3" />
                </button>

                <button
                  v-if="canEdit"
                  type="button"
                  class="inline-flex shrink-0 items-center rounded-md border border-slate-300 bg-white px-1.5 py-1.5 text-slate-500 transition hover:border-rose-300 hover:bg-rose-50 hover:text-rose-700 disabled:opacity-50"
                  :disabled="objectBusy(object)"
                  :aria-label="objectActionLabel(object, 'delete')"
                  @click="removeObject(object)"
                >
                  <AppIcon name="trash" class="h-3 w-3" />
                </button>
              </li>
            </ul>
          </section>
        </div>
      </aside>

      <!-- Reviewer: the photo, its masks and the paging controls. -->
      <section
        class="relative flex min-h-0 flex-1 flex-col overflow-hidden bg-slate-950"
        :aria-label="t('inspector.workspace.review')"
      >
        <FrameReviewer
          :frames="frames"
          :index="activeIndex"
          :objects="maskObjects"
          :annotated="annotated"
          :active-defect-id="selectedDefectId"
          @select="selectFrame"
          @set-view="setView"
          @focus="focusObject"
        />
      </section>
    </div>
  </div>
</template>
