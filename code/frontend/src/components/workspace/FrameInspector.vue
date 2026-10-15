<script setup lang="ts">
/**
 * The 2D inspector: one drone frame at a time, at full size, with every mask
 * that was found on it.
 *
 * This is the fallback for a project without a reconstruction - but it is not a
 * degraded view. Photogrammetry only runs from three overlapping frames up and
 * is the slowest, most failure-prone stage of the pipeline, while the defect
 * analysis is the primary result; this inspector therefore has to stand on its
 * own, with keyboard-reachable navigation, a thumbnail strip and a legend.
 *
 * Masks are drawn per frame rather than per defect because several detections
 * commonly share a panel: the frame under review is the unit an inspector
 * actually reasons about.
 */

import { computed } from 'vue'

import AppIcon from '@/components/AppIcon.vue'
import DefectMaskOverlay from '@/components/workspace/DefectMaskOverlay.vue'
import { defectClassStyle, type FrameGroup, type MaskShape } from '@/utils/defects'

const props = withDefaults(
  defineProps<{
    frames: FrameGroup[]
    activeImageId?: string | null
    /** Highlighted mask, i.e. the card selected in the review list. */
    activeDefectId?: string | null
  }>(),
  { activeImageId: null, activeDefectId: null },
)

const emit = defineEmits<{ select: [imageId: string] }>()

const index = computed(() => {
  const found = props.frames.findIndex((frame) => frame.imageId === props.activeImageId)
  return found === -1 ? 0 : found
})

const active = computed<FrameGroup | null>(() => props.frames[index.value] ?? null)

const shapes = computed<MaskShape[]>(
  () =>
    active.value?.defects.map((defect) => ({
      rings: defect.mask_polygon ?? [],
      defectClass: defect.defect_class,
      active: defect.id === props.activeDefectId,
    })) ?? [],
)

/** The classes actually present on this frame, for the legend. */
const legend = computed(() => {
  const classes = new Set(active.value?.defects.map((defect) => defect.defect_class) ?? [])
  return [...classes].map((defectClass) => defectClassStyle(defectClass))
})

function step(offset: number): void {
  const next = props.frames[index.value + offset]
  if (next) emit('select', next.imageId)
}
</script>

<template>
  <div class="flex h-full min-h-0 flex-col bg-slate-950">
    <template v-if="active">
      <!-- Frame caption -->
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-slate-800 px-4 py-2.5">
        <p class="truncate font-mono text-xs text-slate-300" :title="active.filePath">
          {{ active.filePath }}
        </p>
        <span class="text-[11px] text-slate-500">
          frame {{ index + 1 }} / {{ frames.length }}
        </span>
        <span class="rounded-full bg-slate-800 px-2 py-0.5 text-[11px] font-medium text-slate-200">
          {{ active.defects.length }}
          {{ active.defects.length === 1 ? 'detection' : 'detections' }}
        </span>
        <span class="ml-auto flex items-center gap-2">
          <span
            v-for="style in legend"
            :key="style.label"
            class="inline-flex items-center gap-1.5 text-[11px] text-slate-300"
          >
            <span class="h-2.5 w-2.5 rounded-sm" :style="{ backgroundColor: style.stroke }" />
            {{ style.label }}
          </span>
        </span>
      </div>

      <!-- Frame under review -->
      <div class="grid min-h-0 flex-1 grid-cols-[auto_1fr_auto] items-center gap-2 p-3">
        <button
          type="button"
          class="inline-flex h-9 w-9 items-center justify-center rounded-full border border-slate-700 bg-slate-900/80 text-slate-200 transition hover:bg-slate-800 disabled:opacity-30"
          :disabled="index === 0"
          aria-label="Previous frame"
          @click="step(-1)"
        >
          <AppIcon name="arrowLeft" class="h-4 w-4" />
        </button>

        <DefectMaskOverlay
          :src="active.imageUrl"
          :shapes="shapes"
          fit="contain"
          :alt="`Drone frame ${active.filePath} with detected defects`"
          empty-label="This frame has no stored object key"
        />

        <button
          type="button"
          class="inline-flex h-9 w-9 items-center justify-center rounded-full border border-slate-700 bg-slate-900/80 text-slate-200 transition hover:bg-slate-800 disabled:opacity-30"
          :disabled="index >= frames.length - 1"
          aria-label="Next frame"
          @click="step(1)"
        >
          <AppIcon name="arrowRight" class="h-4 w-4" />
        </button>
      </div>

      <!-- Thumbnails: the frames that actually contain a detection. -->
      <div class="flex gap-2 overflow-x-auto border-t border-slate-800 px-4 py-3">
        <button
          v-for="(frame, position) in frames"
          :key="frame.imageId"
          type="button"
          class="relative h-14 w-20 shrink-0 overflow-hidden rounded-md border transition"
          :class="
            position === index
              ? 'border-blue-400 ring-1 ring-blue-400'
              : 'border-slate-700 opacity-70 hover:opacity-100'
          "
          :aria-label="`Show frame ${position + 1}`"
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
            no image
          </span>
          <span
            class="absolute right-0.5 bottom-0.5 rounded bg-slate-900/85 px-1 text-[10px] font-medium text-slate-100"
          >
            {{ frame.defects.length }}
          </span>
        </button>
      </div>
    </template>

    <div v-else class="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
      <span class="inline-flex h-12 w-12 items-center justify-center rounded-full bg-slate-900">
        <AppIcon name="image" class="h-6 w-6 text-slate-500" />
      </span>
      <p class="text-sm font-medium text-slate-300">No analysed frames to inspect</p>
      <p class="max-w-sm text-xs text-slate-500">
        Frames appear here once the worker has stored at least one detection on them. Upload photos
        from the project wizard to populate this view.
      </p>
    </div>
  </div>
</template>
