/**
 * Project status presentation, in one table.
 *
 * The dashboard grid and the workspace header both show the same badge, and a
 * status whose colour or wording differed between two screens would read as two
 * different states. The *value* comes from the API (`Project.status`), never
 * from a client-side guess.
 */

import { PROJECT_STATUSES } from '@/types/api'
import type { ProjectStatus } from '@/types/api'

export interface ProjectStatusStyle {
  /** Catalogue key for the badge text - a translated screen calls `t()` on it. */
  labelKey: string
  /**
   * The English wording, for the screens that are not translated yet (the
   * workspace). Kept beside `labelKey` so the status vocabulary stays in one
   * table; it can go once every screen reads the catalogue.
   */
  label: string
  /** Tailwind classes for the pill (background, text, ring). */
  badge: string
  /** Tailwind classes for the leading dot. */
  dot: string
}

export const PROJECT_STATUS_STYLES: Record<ProjectStatus, ProjectStatusStyle> = {
  draft: {
    labelKey: 'status.draft',
    label: 'Draft',
    badge: 'bg-slate-100 text-slate-600 ring-slate-200',
    dot: 'bg-slate-400',
  },
  uploading: {
    labelKey: 'status.uploading',
    label: 'Uploading',
    badge: 'bg-blue-50 text-blue-700 ring-blue-200',
    dot: 'bg-blue-500 animate-pulse',
  },
  processing: {
    labelKey: 'status.processing',
    label: 'Processing',
    badge: 'bg-amber-50 text-amber-700 ring-amber-200',
    dot: 'bg-amber-500 animate-pulse',
  },
  // Paused reads as deliberate, not as a warning: slate-on-violet rather than
  // another amber pill, because it is a state the user created on purpose and
  // the way out of it (Resume) is on the progress screen.
  paused: {
    labelKey: 'status.paused',
    label: 'Paused',
    badge: 'bg-violet-50 text-violet-700 ring-violet-200',
    dot: 'bg-violet-500',
  },
  completed: {
    labelKey: 'status.completed',
    label: 'Completed',
    badge: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    dot: 'bg-emerald-500',
  },
  failed: {
    labelKey: 'status.failed',
    label: 'Failed',
    badge: 'bg-red-50 text-red-700 ring-red-200',
    dot: 'bg-red-500',
  },
}

/**
 * Style lookup with a fallback.
 *
 * The API derives the status, so a future value this build has never heard of
 * should render as plain draft chrome rather than crashing the screen or
 * leaving a card with no state at all.
 */
export function projectStatusStyle(status: ProjectStatus): ProjectStatusStyle {
  return PROJECT_STATUS_STYLES[status] ?? PROJECT_STATUS_STYLES[PROJECT_STATUSES.DRAFT]
}
