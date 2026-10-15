<script setup lang="ts">
/**
 * One frame in the review list, with every detection that was found on it.
 *
 * The list used to hold one card per detection, so a single sortie photo with a
 * dozen cracks read as a dozen rows - the same picture repeated, each copy
 * carrying one mask. A frame is the unit an inspector actually reasons about
 * (the 2D inspector navigates frames for the same reason), so the card groups
 * the detections: one thumbnail with *all* the masks drawn over it, and one
 * compact row per detection underneath, each with its own confirmation action.
 *
 * The thumbnail is a small, fixed box (`fit="contain"`). Masks are emitted in
 * normalized [0, 1] coordinates, so they only stay on their defects while the
 * box keeps the frame's aspect ratio - which the overlay guarantees by
 * measuring the box it is given. A fixed box is also what keeps a card from
 * growing to the width of the panel: the photo is a thumbnail here, not the
 * card, and the list is what scrolls.
 */

import { computed } from 'vue'

import AppIcon from '@/components/AppIcon.vue'
import DefectMaskOverlay from '@/components/workspace/DefectMaskOverlay.vue'
import type { Defect } from '@/types/api'
import {
  defectClassStyle,
  formatConfidence,
  type DefectClassStyle,
  type FrameGroup,
  type MaskShape,
} from '@/utils/defects'
import { formatDate } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    frame: FrameGroup
    /** Defect ids whose verification request is in flight. */
    busyIds?: string[]
    /** The detection focused in the viewer, drawn as the solid mask. */
    activeDefectId?: string | null
    /** True while this frame is the one the viewer shows. */
    active?: boolean
    /**
     * False for roles that may read a project but not sign off on it (an Admin).
     * The button is replaced by a note instead of being shown and failing with
     * a 404, which is what the owner-scoped mutation would answer.
     */
    canVerify?: boolean
  }>(),
  { busyIds: () => [], activeDefectId: null, active: false, canVerify: true },
)

const emit = defineEmits<{
  select: [defect: Defect]
  toggle: [defect: Defect, isVerified: boolean]
}>()

/** Every mask on the frame, so the thumbnail shows the whole picture at once. */
const shapes = computed<MaskShape[]>(() =>
  props.frame.defects.map((defect) => ({
    rings: defect.mask_polygon ?? [],
    defectClass: defect.defect_class,
    active: defect.id === props.activeDefectId,
  })),
)

const confirmedCount = computed(
  () => props.frame.defects.filter((defect) => defect.is_verified).length,
)

/** When the batch analysed this frame; every detection shares the run. */
const detectedAt = computed(() => props.frame.defects[0]?.created_at ?? null)

function styleFor(defect: Defect): DefectClassStyle {
  return defectClassStyle(defect.defect_class)
}

/**
 * The confirmation button repeats on every row, so it carries the detection it
 * acts on in its accessible name - "Confirm" ten times over tells a screen
 * reader nothing.
 */
function toggleLabel(defect: Defect): string {
  const action = defect.is_verified ? 'Clear confirmation of' : 'Confirm'
  return `${action} ${styleFor(defect).label.toLowerCase()} (${formatConfidence(defect.confidence_score)}) on ${props.frame.filePath}`
}
</script>

<template>
  <article
    class="flex overflow-hidden rounded-xl border bg-white shadow-sm transition"
    :class="active ? 'border-blue-400 ring-2 ring-blue-100' : 'border-slate-200 hover:border-slate-300'"
  >
    <!-- The photo, small on purpose; every mask on this frame is drawn on it. -->
    <div class="relative h-28 w-40 shrink-0 overflow-hidden border-r border-slate-100 bg-slate-950">
      <DefectMaskOverlay
        :src="frame.imageUrl"
        :shapes="shapes"
        fit="contain"
        :alt="`Frame ${frame.filePath} with ${frame.defects.length} detected defects`"
        empty-label="Frame not stored"
      />
    </div>

    <div class="flex min-w-0 flex-1 flex-col">
      <div class="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-slate-100 px-2.5 py-2">
        <p class="min-w-0 flex-1 truncate font-mono text-[11px] text-slate-600" :title="frame.filePath">
          {{ frame.filePath }}
        </p>
        <span
          class="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700"
        >
          {{ frame.defects.length }}
          {{ frame.defects.length === 1 ? 'detection' : 'detections' }}
        </span>
        <span
          v-if="confirmedCount > 0"
          class="shrink-0 rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 ring-1 ring-emerald-200 ring-inset"
        >
          {{ confirmedCount }} confirmed
        </span>
        <span v-if="detectedAt" class="shrink-0 text-[11px] text-slate-400">
          {{ formatDate(detectedAt) }}
        </span>
      </div>

      <ul class="flex flex-col gap-1 p-2">
        <li v-for="defect in frame.defects" :key="defect.id" class="flex items-center gap-1.5">
          <!--
            Two sibling buttons, never one nested in the other: an inspector who
            tabs to "Confirm" must not walk to another frame by pressing it.
          -->
          <button
            type="button"
            class="flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2 py-1.5 text-left transition hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
            :class="
              defect.id === activeDefectId ? 'bg-blue-50 ring-1 ring-blue-200 ring-inset' : ''
            "
            :aria-pressed="defect.id === activeDefectId"
            @click="emit('select', defect)"
          >
            <span
              class="inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset"
              :class="styleFor(defect).badge"
            >
              {{ styleFor(defect).label }}
            </span>
            <span class="shrink-0 text-xs font-semibold tabular-nums text-slate-900">
              {{ formatConfidence(defect.confidence_score) }}
            </span>
            <span
              v-if="defect.is_verified"
              class="ml-auto inline-flex shrink-0 items-center gap-1 text-[11px] font-medium text-emerald-700"
            >
              <AppIcon name="check" class="h-3 w-3" />
              Verified
            </span>
            <span v-else class="ml-auto shrink-0 text-[11px] text-slate-400">Awaiting review</span>
          </button>

          <button
            v-if="canVerify"
            type="button"
            class="inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-1.5 text-[11px] font-medium transition disabled:opacity-50"
            :class="
              defect.is_verified
                ? 'border-slate-300 bg-white text-slate-600 hover:bg-slate-100'
                : 'border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100'
            "
            :disabled="busyIds.includes(defect.id)"
            :aria-label="toggleLabel(defect)"
            @click="emit('toggle', defect, !defect.is_verified)"
          >
            <AppIcon :name="defect.is_verified ? 'close' : 'check'" class="h-3 w-3" />
            {{ defect.is_verified ? 'Clear' : 'Confirm' }}
          </button>
          <span v-else class="shrink-0 px-1 text-[11px] text-slate-400">Read-only</span>
        </li>
      </ul>
    </div>
  </article>
</template>
