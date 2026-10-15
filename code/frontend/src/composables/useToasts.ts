/**
 * The toast queue.
 *
 * Module-level state rather than a Pinia store: a toast is a detail of the
 * *view* layer, and the stores and the API client must stay callable without a
 * UI. Any component, at any depth, reaches the same queue through
 * `useToasts()`, and exactly one component renders it (`ToastHost.vue`,
 * mounted in `App.vue`).
 *
 * Failures are the messages a user actually has to read - and the ones they
 * quote when reporting a problem - so an error toast stays until dismissed
 * instead of sliding away after four seconds.
 */

import { ref } from 'vue'

export type ToastTone = 'success' | 'error' | 'warning' | 'info'

export interface Toast {
  id: number
  tone: ToastTone
  title: string
  /** Optional second line: the API's message, a count, a file name. */
  description: string | null
}

const AUTO_DISMISS_MS: Record<ToastTone, number> = {
  success: 4_000,
  info: 5_000,
  warning: 7_000,
  error: 10_000,
}

/** Beyond this the stack covers the page it is reporting on. Oldest goes first. */
const MAX_VISIBLE = 4

const toasts = ref<Toast[]>([])
const timers = new Map<number, ReturnType<typeof setTimeout>>()
let nextId = 1

function dismiss(id: number): void {
  const timer = timers.get(id)
  if (timer !== undefined) {
    clearTimeout(timer)
    timers.delete(id)
  }
  toasts.value = toasts.value.filter((toast) => toast.id !== id)
}

function push(tone: ToastTone, title: string, description: string | null = null): number {
  // A polling screen can produce the same failure every few seconds. Repeat the
  // message in place rather than stacking a copy per tick.
  const duplicate = toasts.value.find(
    (toast) => toast.tone === tone && toast.title === title && toast.description === description,
  )
  if (duplicate !== undefined) {
    const timer = timers.get(duplicate.id)
    if (timer !== undefined) {
      clearTimeout(timer)
      timers.set(
        duplicate.id,
        setTimeout(() => dismiss(duplicate.id), AUTO_DISMISS_MS[tone]),
      )
    }
    // Move it to the newest position so a refreshed message is not hidden
    // behind the ones it was raised before.
    toasts.value = [...toasts.value.filter((toast) => toast.id !== duplicate.id), duplicate]
    return duplicate.id
  }

  const toast: Toast = { id: nextId, tone, title, description }
  nextId += 1
  toasts.value = [...toasts.value, toast]

  while (toasts.value.length > MAX_VISIBLE) {
    dismiss(toasts.value[0]!.id)
  }

  timers.set(
    toast.id,
    setTimeout(() => dismiss(toast.id), AUTO_DISMISS_MS[tone]),
  )
  return toast.id
}

function clear(): void {
  for (const timer of timers.values()) clearTimeout(timer)
  timers.clear()
  toasts.value = []
}

export function useToasts() {
  return {
    toasts,
    push,
    success: (title: string, description?: string | null) => push('success', title, description),
    error: (title: string, description?: string | null) => push('error', title, description),
    warning: (title: string, description?: string | null) => push('warning', title, description),
    info: (title: string, description?: string | null) => push('info', title, description),
    dismiss,
    clear,
  }
}
