<script setup lang="ts">
/**
 * Indeterminate spinner for work whose duration is unknown (a `.glb` download,
 * a frame being signed and fetched).
 *
 * Not an icon: it animates, so it stays out of `icons.ts`, which is static path
 * data. Colour comes from `currentColor`, so a caller restyles it with a text
 * class instead of a prop.
 */

withDefaults(defineProps<{ label?: string }>(), { label: '' })
</script>

<template>
  <span role="status" class="inline-flex items-center gap-2">
    <svg class="h-5 w-5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-opacity="0.25" stroke-width="3" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        stroke-width="3"
        stroke-linecap="round"
      />
    </svg>
    <span v-if="label" class="text-xs">{{ label }}</span>
    <span v-else class="sr-only">Loading</span>
  </span>
</template>
