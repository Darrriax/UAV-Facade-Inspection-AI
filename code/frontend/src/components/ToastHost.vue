<script setup lang="ts">
/**
 * Renders the toast queue.
 *
 * Mounted exactly once, in `App.vue`, so every screen gets the same stack in the
 * same corner and no view has to be aware of it. The queue itself lives in
 * `composables/useToasts.ts` - pushing a toast needs no component at all, which
 * is what lets an error be reported from an API call's `catch` without a
 * prop-drilled callback.
 *
 * Accessibility: the container is a polite live region, so a toast is announced
 * without interrupting; the error toasts switch to `role="alert"` because a
 * failed mutation *is* worth interrupting for. The icon is decorative - the
 * tone is also spelled out for screen readers, since "red icon" is not
 * information.
 */

import AppIcon from '@/components/AppIcon.vue'
import type { IconName } from '@/components/icons'
import { useToasts, type ToastTone } from '@/composables/useToasts'

const { toasts, dismiss } = useToasts()

const TONES: Record<
  ToastTone,
  { icon: IconName; label: string; wrapper: string; iconClass: string }
> = {
  success: {
    icon: 'check',
    label: 'Success',
    wrapper: 'border-emerald-200 bg-emerald-50 text-emerald-900',
    iconClass: 'text-emerald-600',
  },
  error: {
    icon: 'alert',
    label: 'Error',
    wrapper: 'border-red-200 bg-red-50 text-red-900',
    iconClass: 'text-red-600',
  },
  warning: {
    icon: 'alert',
    label: 'Warning',
    wrapper: 'border-amber-200 bg-amber-50 text-amber-900',
    iconClass: 'text-amber-600',
  },
  info: {
    icon: 'info',
    label: 'Notice',
    wrapper: 'border-slate-200 bg-white text-slate-900',
    iconClass: 'text-slate-500',
  },
}
</script>

<template>
  <div
    class="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex flex-col items-stretch gap-2 p-4 sm:items-end sm:p-6"
    aria-live="polite"
  >
    <TransitionGroup
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 translate-y-2 sm:translate-y-0 sm:translate-x-4"
      enter-to-class="opacity-100 translate-y-0 sm:translate-x-0"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0 translate-x-4"
      move-class="transition duration-200"
    >
      <div
        v-for="toast in toasts"
        :key="toast.id"
        :role="toast.tone === 'error' ? 'alert' : 'status'"
        class="pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-xl border p-3.5 shadow-lg sm:w-96"
        :class="TONES[toast.tone].wrapper"
      >
        <AppIcon :name="TONES[toast.tone].icon" class="mt-0.5 h-5 w-5 shrink-0" :class="TONES[toast.tone].iconClass" />
        <div class="min-w-0 flex-1">
          <p class="text-sm font-semibold">
            <span class="sr-only">{{ TONES[toast.tone].label }}: </span>{{ toast.title }}
          </p>
          <p v-if="toast.description" class="mt-0.5 text-sm break-words opacity-90">
            {{ toast.description }}
          </p>
        </div>
        <button
          type="button"
          class="-m-1 shrink-0 rounded-lg p-1 opacity-60 transition hover:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-current"
          aria-label="Dismiss notification"
          @click="dismiss(toast.id)"
        >
          <AppIcon name="close" class="h-4 w-4" />
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>
