<script setup lang="ts">
/**
 * The 2D review canvas: one UAV frame at a time, at full size, pageable.
 *
 * Three things define it.
 *
 * **Every frame is in the carousel.** The strip and the arrows walk the whole
 * sortie in upload order - a photo the model found nothing on included - because
 * the question an inspector is answering is "what is on this building", and
 * "this panel is clean" is part of that answer. Frames the worker has not
 * finished with carry a status chip instead of pretending to be clean.
 *
 * **It draws objects, not detections.** The frame arrives as `MaskObject[]`, the
 * already-merged view of the damage (see `mergeDetections`), so overlapping
 * fragments of one crack are a single shape with a single outline and the count
 * in the caption is a count of things on the building. The panel beside the
 * canvas lists the same objects, which is what keeps the two halves of the
 * screen from disagreeing about what is there.
 *
 * **The annotated view is a display mode, not a second screen.** `annotated`
 * dims the photo and opens the mask shapes as bright windows (see
 * `DefectMaskOverlay`); switching to the plain photo hides the masks entirely,
 * which is what a reviewer wants when judging the facade itself. That state and
 * the armed tool live in the parent, because the tool buttons sit in the panel
 * beside this canvas - the canvas must not keep private state the panel cannot
 * see or clear.
 *
 * **Paging is cheap.** Objects arrive already grouped, so switching frame is a
 * lookup rather than a raster pass, and the neighbours' bytes are pulled into
 * the browser's cache ahead of the click (see `warmNeighbours`) - the two things
 * that made the next photo take a visible moment to appear.
 */

import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import AppIcon from '@/components/AppIcon.vue'
import DefectMaskOverlay from '@/components/workspace/DefectMaskOverlay.vue'
import { IMAGE_STATUSES } from '@/types/api'
import type { ImageStatus } from '@/types/api'
import {
  isObjectFocused,
  type FrameGroup,
  type MaskObject,
  type MaskShape,
} from '@/utils/defects'

const props = withDefaults(
  defineProps<{
    frames: FrameGroup[]
    /** Position of the frame on screen; clamped by the parent. */
    index: number
    /** The frame's damage, already merged into objects. */
    objects?: MaskObject[]
    /** False shows the plain photo with no masks. */
    annotated?: boolean
    /** Detection focused in the panel; its object is drawn as the solid mask. */
    activeDefectId?: string | null
  }>(),
  { objects: () => [], annotated: true, activeDefectId: null },
)

const emit = defineEmits<{
  select: [imageId: string]
  /** The plain / annotated switch. */
  setView: [annotated: boolean]
  focus: [object: MaskObject]
}>()

const { t } = useI18n()

/**
 * Catalogue key for a frame that is still moving through the pipeline.
 *
 * Keys rather than sentences, so the chip and the thumbnail tooltip - which
 * show the same status in two places - cannot end up disagreeing about the
 * wording of one state.
 */
const STATUS_KEYS: Record<ImageStatus, string> = {
  [IMAGE_STATUSES.PENDING]: 'inspector.workspace.frame.status.pending',
  [IMAGE_STATUSES.PROCESSING]: 'inspector.workspace.frame.status.processing',
  [IMAGE_STATUSES.COMPLETED]: 'inspector.workspace.frame.status.completed',
  [IMAGE_STATUSES.FAILED]: 'inspector.workspace.frame.status.failed',
}

const active = computed<FrameGroup | null>(() => props.frames[props.index] ?? null)

/** One shape per object; the pencil and the delete click work on objects. */
const shapes = computed<MaskShape[]>(() =>
  props.objects.map((object) => ({
    id: object.id,
    rings: object.rings,
    defectClass: object.defectClass,
    active: isObjectFocused(object, props.activeDefectId),
  })),
)

const activeStatus = computed(() => active.value?.status ?? null)

/**
 * What pressing the eye does: the label names the view the button *switches
 * to*, which is the one thing a two-state toggle cannot show on its face.
 */
const viewLabel = computed(() =>
  props.annotated ? t('inspector.workspace.view.plain') : t('inspector.workspace.view.annotated'),
)

const thumbs = ref<(HTMLElement | null)[]>([])

function setThumb(element: Element | null, position: number): void {
  thumbs.value[position] = element as HTMLElement | null
}

/**
 * URLs already pulled into the browser's cache, so a revisit costs nothing.
 *
 * URLs rather than frame ids: the signed URL is what the `<img>` asks for, and
 * that is what the cache is keyed by.
 */
const warmed = new Set<string>()

/**
 * Fetch the frames around the one on screen before they are asked for.
 *
 * A UAV frame is a multi-megabyte JPEG: paging the carousel is the point of this
 * screen, and paying for the download and the decode *after* the click is what
 * makes the next photo take a moment to appear. Pulling the neighbours into the
 * cache while the current one is being read turns that pause into nothing. The
 * `Image` objects are dropped immediately - the browser keeps the decoded bytes,
 * which is all the next `<img>` needs - and the set above keeps this from
 * refetching the same URL on every step.
 */
function warmNeighbours(): void {
  for (const offset of [1, -1, 2, -2]) {
    const neighbour = props.frames[props.index + offset]
    const url = neighbour?.imageUrl
    if (!url || warmed.has(url)) continue
    warmed.add(url)
    const image = new Image()
    image.decoding = 'async'
    image.src = url
  }
}

// Keep the frame on screen visible in the strip: with a few hundred frames the
// active one is otherwise scrolled out of the container the user just clicked.
// Warming the neighbours happens here too, because a step is what makes them the
// frames the user is about to look at.
watch(
  () => props.index,
  async () => {
    warmNeighbours()
    await nextTick()
    thumbs.value[props.index]?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  },
)

// The first frame is warmed on arrival: the frames list lands after the route,
// and until then there is no neighbour to fetch.
watch(
  () => props.frames,
  () => warmNeighbours(),
  { immediate: true },
)

function step(offset: number): void {
  const next = props.frames[props.index + offset]
  if (next) emit('select', next.imageId)
}

/** Narrows a drawn shape back to the object it came from, for the parent. */
function objectOf(shape: MaskShape): MaskObject | null {
  if (!shape.id) return null
  return props.objects.find((object) => object.id === shape.id) ?? null
}

function onFocus(shape: MaskShape): void {
  const object = objectOf(shape)
  if (object) emit('focus', object)
}
</script>

<template>
  <div class="flex h-full min-h-0 flex-col bg-slate-950">
    <template v-if="active">
      <!--
        Frame caption. The file name says *which* photo, the chip says what the
        worker knows about it, and the pager and the view switch sit together on
        the right: they are the two controls that move the user through the
        sortie, so they belong side by side rather than at opposite ends of a
        row. The object count lives in the panel, where the objects are listed -
        repeating it here said nothing the list did not already say.
      -->
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-slate-800 px-4 py-2.5">
        <p class="truncate font-mono text-xs text-slate-300" :title="active.filePath">
          {{ active.filePath }}
        </p>
        <span
          v-if="activeStatus && activeStatus !== IMAGE_STATUSES.COMPLETED"
          class="rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset"
          :class="
            activeStatus === IMAGE_STATUSES.FAILED
              ? 'bg-rose-500/15 text-rose-300 ring-rose-500/40'
              : 'bg-amber-500/15 text-amber-200 ring-amber-500/40'
          "
        >
          {{ t(STATUS_KEYS[activeStatus]) }}
        </span>

        <span class="ml-auto shrink-0 text-[11px] tabular-nums text-slate-400">
          {{ t('inspector.workspace.frame.counter', { index: index + 1, total: frames.length }) }}
        </span>

        <!--
          One round button, not a two-state segmented control: the eye shows what
          the canvas is showing right now, and pressing it flips that.
        -->
        <button
          type="button"
          class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500"
          :class="
            annotated
              ? 'border-blue-400 bg-blue-500/20 text-blue-100 hover:bg-blue-500/30'
              : 'border-slate-700 bg-slate-900 text-slate-300 hover:bg-slate-800'
          "
          :aria-pressed="annotated"
          :aria-label="viewLabel"
          :title="viewLabel"
          @click="emit('setView', !annotated)"
        >
          <AppIcon :name="annotated ? 'eye' : 'eyeOff'" class="h-4 w-4" />
        </button>
      </div>

      <!--
        Frame under review.

        The row is `minmax(0, 1fr)`, not an implicit auto row: the canvas inside
        is sized in pixels by its own measurement of this cell, so an auto row
        would let that measurement inflate the row and then be measured again -
        a loop that pushes the photo out of the panel as soon as the layout gets
        short enough. A definite row breaks it, because content cannot grow a
        `1fr` track.
      -->
      <div
        class="grid min-h-0 flex-1 grid-cols-[auto_1fr_auto] grid-rows-[minmax(0,1fr)] items-center gap-2 p-3"
      >
        <button
          type="button"
          class="inline-flex h-9 w-9 items-center justify-center rounded-full border border-slate-700 bg-slate-900/80 text-slate-200 transition hover:bg-slate-800 disabled:opacity-30"
          :disabled="index === 0"
          :aria-label="t('inspector.workspace.frame.previous')"
          @click="step(-1)"
        >
          <AppIcon name="arrowLeft" class="h-4 w-4" />
        </button>

        <div class="flex h-full min-h-0 w-full items-center justify-center overflow-hidden">
          <DefectMaskOverlay
            :src="active.imageUrl"
            :shapes="annotated ? shapes : []"
            :dim="annotated"
            fit="contain"
            :alt="t('inspector.workspace.canvas.alt', { file: active.filePath, n: objects.length }, objects.length)"
            :empty-label="t('inspector.workspace.canvas.noObjectKey')"
            @focus="onFocus"
          />
        </div>

        <button
          type="button"
          class="inline-flex h-9 w-9 items-center justify-center rounded-full border border-slate-700 bg-slate-900/80 text-slate-200 transition hover:bg-slate-800 disabled:opacity-30"
          :disabled="index >= frames.length - 1"
          :aria-label="t('inspector.workspace.frame.next')"
          @click="step(1)"
        >
          <AppIcon name="arrowRight" class="h-4 w-4" />
        </button>
      </div>

      <!--
        The whole sortie, not only its defects: a clean frame is a result the
        reviewer has to be able to open, and the counter is how they find it.
      -->
      <div class="flex gap-2 overflow-x-auto border-t border-slate-800 px-4 py-3">
        <button
          v-for="(frame, position) in frames"
          :key="frame.imageId"
          :ref="(element) => setThumb(element as Element | null, position)"
          type="button"
          class="relative h-14 w-20 shrink-0 overflow-hidden rounded-md border transition"
          :class="
            position === index
              ? 'border-blue-400 ring-1 ring-blue-400'
              : 'border-slate-700 opacity-70 hover:opacity-100'
          "
          :title="
            frame.status === IMAGE_STATUSES.COMPLETED
              ? frame.filePath
              : `${frame.filePath} · ${t(STATUS_KEYS[frame.status])}`
          "
          :aria-label="t('inspector.workspace.frame.show', { index: position + 1, total: frames.length })"
          :aria-current="position === index"
          @click="emit('select', frame.imageId)"
        >
          <img
            v-if="frame.imageUrl"
            :src="frame.imageUrl"
            alt=""
            loading="lazy"
            class="h-full w-full object-cover"
          />
          <span
            v-else
            class="flex h-full w-full items-center justify-center bg-slate-900 text-[10px] text-slate-500"
          >
            {{ t('inspector.workspace.frame.noImage') }}
          </span>
          <!--
            No defect count in the corner: the thumbnails are how the reviewer
            recognises a frame, and a number over a 80x56 preview is unreadable
            at a glance while making every photo look annotated. What a frame
            holds is on the canvas, and how many frames hold anything is in the
            panel beside it.
          -->
          <span
            v-if="frame.status !== IMAGE_STATUSES.COMPLETED"
            class="absolute top-0.5 left-0.5 h-1.5 w-1.5 rounded-full"
            :class="frame.status === IMAGE_STATUSES.FAILED ? 'bg-rose-400' : 'bg-amber-400'"
          />
        </button>
      </div>
    </template>

    <div v-else class="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
      <span class="inline-flex h-12 w-12 items-center justify-center rounded-full bg-slate-900">
        <AppIcon name="image" class="h-6 w-6 text-slate-500" />
      </span>
      <p class="text-sm font-medium text-slate-300">{{ t('inspector.workspace.empty.title') }}</p>
      <p class="max-w-sm text-xs text-slate-500">
        {{ t('inspector.workspace.empty.body') }}
      </p>
    </div>
  </div>
</template>
