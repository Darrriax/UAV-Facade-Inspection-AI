<script setup lang="ts">
/**
 * A single stroke icon.
 *
 * Decorative by default (`aria-hidden`): the accessible name belongs on the
 * button or link around it, where the text already is. Size and colour come
 * from utility classes on the call site (`class="h-4 w-4 text-slate-400"`),
 * because `currentColor` and `h-*`/`w-*` compose better than props do.
 */

import { computed } from 'vue'

import { ICONS, type IconName } from './icons'

const props = withDefaults(
  defineProps<{
    name: IconName
    /** Thicker strokes read better at small sizes, e.g. inside a badge. */
    strokeWidth?: number
  }>(),
  { strokeWidth: 1.75 },
)

const paths = computed(() => ICONS[props.name])
</script>

<template>
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path v-for="d in paths" :key="d" :d="d" />
  </svg>
</template>
