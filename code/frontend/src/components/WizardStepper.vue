<script setup lang="ts">
/**
 * The wizard's three-step progress bar.
 *
 * Presentational: it renders whatever steps it is given, marks the active one
 * and links to the steps that can actually be visited. Which steps are reachable
 * is the caller's business, because that depends on the flow's state (there is
 * no project to upload to until step 1 has run), not on the stepper.
 *
 * A step that cannot be visited yet renders as a plain `<div>` rather than a
 * link with a no-op handler: an inactive control should not be focusable, and
 * "nothing happens on Enter" is worse than not being a link at all.
 */

import { computed } from 'vue'
import { RouterLink, type RouteLocationRaw } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'

interface WizardStep {
  label: string
  /** One line under the label: what the step collects. */
  hint: string
}

const props = defineProps<{
  steps: readonly WizardStep[]
  activeIndex: number
  /** Target route for a step, or `null` when the flow cannot reach it yet. */
  linkFor: (index: number) => RouteLocationRaw | null
  /**
   * Accessible name for the list, already translated by the caller - this
   * component has no i18n of its own (see the docstring).
   */
  ariaLabel: string
}>()

interface RenderedStep extends WizardStep {
  index: number
  /** `null` when this step cannot be opened yet. */
  to: RouteLocationRaw | null
  state: 'done' | 'current' | 'upcoming'
}

const items = computed<RenderedStep[]>(() =>
  props.steps.map((step, index) => ({
    ...step,
    index,
    to: props.linkFor(index),
    state: index === props.activeIndex ? 'current' : index < props.activeIndex ? 'done' : 'upcoming',
  })),
)

const CARD: Record<RenderedStep['state'], string> = {
  current: 'border-blue-300 bg-blue-50/60 ring-1 ring-blue-200',
  done: 'border-slate-200 bg-white',
  upcoming: 'border-slate-200 bg-white',
}

const CIRCLE: Record<RenderedStep['state'], string> = {
  current: 'bg-blue-600 text-white',
  done: 'bg-emerald-600 text-white',
  upcoming: 'bg-slate-100 text-slate-500 ring-1 ring-slate-200 ring-inset',
}
</script>

<template>
  <ol class="grid gap-3 sm:grid-cols-3" :aria-label="ariaLabel">
    <li v-for="item in items" :key="item.label">
      <!-- One content block, two elements: a link when the step is reachable and
           an inert card when it is not. -->
      <component
        :is="item.to ? RouterLink : 'div'"
        v-bind="item.to ? { to: item.to } : {}"
        class="flex h-full items-center gap-3 rounded-xl border p-3.5 transition"
        :class="[
          CARD[item.state],
          item.to ? 'hover:border-blue-300 hover:shadow-sm' : 'cursor-not-allowed',
          item.state === 'current' ? 'cursor-default' : '',
        ]"
        :aria-current="item.state === 'current' ? 'step' : undefined"
      >
        <span
          class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold"
          :class="CIRCLE[item.state]"
        >
          <AppIcon v-if="item.state === 'done'" name="check" class="h-4 w-4" />
          <template v-else>{{ item.index + 1 }}</template>
        </span>
        <span class="min-w-0">
          <span
            class="block text-sm font-semibold"
            :class="item.state === 'current' ? 'text-blue-900' : 'text-slate-900'"
          >
            {{ item.label }}
          </span>
          <span class="block truncate text-xs text-slate-500">{{ item.hint }}</span>
        </span>
      </component>
    </li>
  </ol>
</template>
