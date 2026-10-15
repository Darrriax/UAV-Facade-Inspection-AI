<script setup lang="ts">
/**
 * A UAV frame with defect masks drawn over it.
 *
 * **Why the mask stays aligned.** The model emits polygons in normalized [0, 1]
 * coordinates, so the SVG uses `viewBox="0 0 1 1"` with
 * `preserveAspectRatio="none"` and the raw points as vertices - the SVG
 * transform does the scaling. That is only correct while the box it fills has
 * the *frame's* aspect ratio, so this component guarantees that instead of
 * trusting its caller:
 *
 * * `fit="width"`: the box is full width with `aspect-ratio` taken from the
 *   frame's own `naturalWidth/naturalHeight`.
 * * `fit="contain"` (the reviewer): a ResizeObserver measures the available
 *   space and sizes the box in pixels, which is the one reliable way to fit a
 *   fixed-ratio box inside a box. CSS alone cannot do it without either
 *   cropping or stretching, and stretching is precisely the bug that would
 *   slide every mask off its defect. The observed element is the *host*, whose
 *   size comes from the caller's layout, so the measurement cannot feed back
 *   into itself.
 *
 * **The annotated view is a mask, not four dark rectangles.** `dim` paints one
 * scrim over the whole photo with an SVG `<mask>` that punches the defect shapes
 * out of it, so the masks read as windows onto the untouched frame while
 * everything around them is dimmed - which is what makes a thin crack visible
 * instead of merely outlined.
 *
 * **Clicking a mask selects it.** While the masks are on screen a click on a
 * shape reports the object it belongs to (`focus`), which is how the canvas and
 * the panel keep pointing at the same finding; the canvas itself changes no
 * data.
 *
 * `vector-effect="non-scaling-stroke"` keeps outlines a constant screen width,
 * so a mask is legible on a 400 px thumbnail and in a full-height panel alike.
 *
 * Loading, failure and "no object key at all" are three distinct states and all
 * of them keep the box's footprint, so the surrounding layout never reflows
 * while a frame is fetched - or when a signed URL has expired.
 */

import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import AppSpinner from '@/components/AppSpinner.vue'
import { defectClassStyle, type MaskShape } from '@/utils/defects'

const props = withDefaults(
  defineProps<{
    /** Signed URL of the frame; null when the row has no stored object key. */
    src: string | null
    /** Masks to draw, in normalized [0, 1] coordinates. */
    shapes?: MaskShape[]
    alt?: string
    /** Caption used when there is no image to show at all. */
    emptyLabel?: string
    fit?: 'width' | 'contain'
    /** Scrim the photo outside the masks (the annotated view). */
    dim?: boolean
  }>(),
  {
    shapes: () => [],
    alt: '',
    emptyLabel: '',
    fit: 'width',
    dim: false,
  },
)

const { t } = useI18n()

/**
 * The `emptyLabel` prop is optional because a caller may not have one to give;
 * the fallback is resolved here rather than as a prop default, which cannot
 * reach the catalogue (defaults are evaluated before `useI18n` is available and
 * would be frozen for the life of the component, so a language switch would
 * leave it behind).
 */
const emptyCaption = computed(() => props.emptyLabel || t('inspector.workspace.canvas.noPreview'))

const emit = defineEmits<{
  /** The object the user clicked on the photo. */
  focus: [shape: MaskShape]
}>()

type LoadStatus = 'loading' | 'ready' | 'failed'

const status = ref<LoadStatus>('loading')
/** The frame's own pixel size, known only after it loads. */
const natural = ref<{ width: number; height: number } | null>(null)
const host = ref<HTMLElement | null>(null)
/** Fitted box in CSS pixels, used by `fit="contain"`. */
const fitted = ref<{ width: string; height: string } | null>(null)

let observer: ResizeObserver | null = null

/** Unique per instance: two canvases on one page must not share a mask id. */
const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '')
const maskId = `mask-dim-${uid}`

const FALLBACK_RATIO = '4 / 3'

/**
 * The box that the image *and* the SVG fill - always the frame's ratio.
 *
 * Both branches return the same keys on purpose: a union of different shapes is
 * normalised by TypeScript into optional properties, which then no longer
 * satisfies an index signature, and `height: auto` plus `aspect-ratio` is what
 * the width mode wants anyway.
 */
const mediaStyle = computed<Record<string, string>>(() => {
  // Before the frame loads (and always in `width` mode) the ratio falls back to
  // 4:3, which is what the UAV camera shoots: no layout jump for the common case.
  const ratio = natural.value ? `${natural.value.width} / ${natural.value.height}` : FALLBACK_RATIO

  if (props.fit === 'contain' && fitted.value) {
    return { width: fitted.value.width, height: fitted.value.height, aspectRatio: ratio }
  }
  return { width: '100%', height: 'auto', aspectRatio: ratio }
})

function measure(): void {
  if (props.fit !== 'contain') return
  const element = host.value
  const size = natural.value
  if (!element || !size) return

  const availableWidth = element.clientWidth
  const availableHeight = element.clientHeight
  if (availableWidth <= 0 || availableHeight <= 0) return

  const scale = Math.min(availableWidth / size.width, availableHeight / size.height)
  fitted.value = {
    width: `${Math.round(size.width * scale)}px`,
    height: `${Math.round(size.height * scale)}px`,
  }
}

onMounted(() => {
  if (props.fit !== 'contain' || typeof ResizeObserver === 'undefined') return
  observer = new ResizeObserver(measure)
  if (host.value) observer.observe(host.value)
})

onBeforeUnmount(() => observer?.disconnect())

watch(
  () => props.src,
  () => {
    // A new URL means a new frame: drop the old measurements so the box cannot
    // keep the previous frame's ratio.
    status.value = 'loading'
    natural.value = null
    fitted.value = null
  },
)

function onLoad(event: Event): void {
  const image = event.target as HTMLImageElement
  if (image.naturalWidth > 0 && image.naturalHeight > 0) {
    natural.value = { width: image.naturalWidth, height: image.naturalHeight }
  }
  status.value = 'ready'
  measure()
}

/**
 * Shapes with something to draw: fewer than three points is not a polygon, and a
 * mask whose every part was degenerate is not a shape. An object that is several
 * connected regions is drawn as several polygons of the same colour, which is
 * what keeps a split mask from being closed across the gap between parts.
 */
const drawable = computed(() =>
  props.shapes
    .map((shape) => ({
      ...shape,
      rings: shape.rings.filter((ring) => ring.length >= 3),
    }))
    .filter((shape) => shape.rings.length > 0),
)

function pointsOf(ring: { x: number; y: number }[]): string {
  return ring.map((point) => `${point.x},${point.y}`).join(' ')
}

function onShapeClick(shape: MaskShape): void {
  emit('focus', shape)
}
</script>

<template>
  <div
    ref="host"
    class="relative"
    :class="fit === 'contain' ? 'flex h-full w-full items-center justify-center' : 'w-full'"
  >
    <div class="relative overflow-hidden bg-slate-950" :style="mediaStyle">
      <img
        v-if="src"
        :src="src"
        :alt="alt"
        loading="lazy"
        decoding="async"
        class="absolute inset-0 h-full w-full object-cover"
        @load="onLoad"
        @error="status = 'failed'"
      />

      <svg
        v-if="status === 'ready'"
        class="pointer-events-none absolute inset-0 h-full w-full"
        viewBox="0 0 1 1"
        preserveAspectRatio="none"
      >
        <!--
          The scrim is painted once and then *cut open* where the masks are:
          white keeps the scrim, black punches a hole, so a defect shows the
          untouched photo while the rest of the frame recedes.
        -->
        <defs v-if="dim">
          <mask
            :id="maskId"
            maskUnits="userSpaceOnUse"
            x="0"
            y="0"
            width="1"
            height="1"
            style="mask-type: luminance"
          >
            <rect x="0" y="0" width="1" height="1" fill="white" />
            <template v-for="(shape, index) in drawable" :key="index">
              <polygon
                v-for="(ring, ringIndex) in shape.rings"
                :key="ringIndex"
                :points="pointsOf(ring)"
                fill="black"
              />
            </template>
          </mask>
        </defs>

        <rect
          v-if="dim"
          x="0"
          y="0"
          width="1"
          height="1"
          fill="#020617"
          fill-opacity="0.62"
          :mask="`url(#${maskId})`"
        />

        <g
          v-for="(shape, index) in drawable"
          :key="index"
          :class="[
            shape.active ? 'opacity-100' : 'opacity-60',
            'cursor-pointer hover:opacity-100',
          ]"
        >
          <polygon
            v-for="(ring, ringIndex) in shape.rings"
            :key="ringIndex"
            :points="pointsOf(ring)"
            :fill="defectClassStyle(shape.defectClass).fill"
            :stroke="defectClassStyle(shape.defectClass).stroke"
            :stroke-width="shape.active ? 2.5 : 1.5"
            vector-effect="non-scaling-stroke"
            class="pointer-events-auto"
            @click.stop="onShapeClick(shape)"
          />
        </g>
      </svg>

      <div
        v-if="status === 'loading' && src"
        class="absolute inset-0 flex items-center justify-center bg-slate-900/90 text-slate-400"
      >
        <AppSpinner :label="t('inspector.workspace.canvas.loading')" />
      </div>

      <div
        v-else-if="status === 'failed'"
        class="absolute inset-0 flex items-center justify-center px-4 text-center text-xs text-rose-300"
      >
        {{ t('inspector.workspace.canvas.failed') }}
      </div>

      <div
        v-else-if="!src"
        class="absolute inset-0 flex items-center justify-center px-4 text-center text-xs text-slate-500"
      >
        {{ emptyCaption }}
      </div>
    </div>
  </div>
</template>
