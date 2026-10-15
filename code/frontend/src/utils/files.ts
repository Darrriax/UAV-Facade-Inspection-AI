/**
 * File checks and formatting for the two upload steps.
 *
 * The accepted types and the size limit mirror the API's (`ACCEPTED_IMAGE_TYPES`
 * and `MAX_FILE_BYTES` in `api/projects.ts`, which in turn mirror the backend
 * settings). Checking here is a courtesy that saves the user a round trip for a
 * file that was never going to be accepted - the API still validates, and it is
 * still the boundary.
 */

import { ACCEPTED_IMAGE_TYPES } from '@/api/projects'

/** Extensions the API folds onto a canonical type (see `ALLOWED_IMAGE_SUFFIXES`). */
export const IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'] as const

export function extensionOf(filename: string): string {
  const dot = filename.lastIndexOf('.')
  return dot === -1 ? '' : filename.slice(dot).toLowerCase()
}

/**
 * Whether the API will accept this file.
 *
 * A declared MIME type is trusted when it is one of the four; otherwise the
 * extension decides, because browsers report an empty type - or
 * `application/octet-stream` - for some perfectly ordinary TIFFs. This mirrors
 * the backend's fallback exactly, so a file accepted here is not rejected there.
 */
export function isAcceptedImage(file: File): boolean {
  // Total on purpose: this runs inside the drop handler's loop, and a File-like
  // entry with no `type` (some directory-entry implementations) must be judged,
  // not allowed to abort the walk that is reading the other 400 frames.
  const declared = String(file.type ?? '').split(';')[0]?.trim().toLowerCase() ?? ''
  if (ACCEPTED_IMAGE_TYPES.split(',').includes(declared)) return true
  return IMAGE_EXTENSIONS.includes(extensionOf(file.name) as (typeof IMAGE_EXTENSIONS)[number])
}

/** Human-readable size: "1.4 MB". Binary units, because that is what uploads use. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`
}
