/**
 * Defect presentation: the class colours, the objects a frame is reviewed by,
 * and the shape the overlay draws.
 *
 * Colours are defined once for the same reason the project badge is: a crack
 * must be the same amber in the list badge, in the mask drawn over the photo,
 * and in the frame carousel, or the same detection appears to change class
 * as the user moves between panels.
 *
 * Grouping is the *server's* answer, not a client-side calculation. The model
 * reports one crack as several overlapping fragments; which of them are the same
 * damage is decided when the frame is analysed and stored on every row as
 * `object_index` (see the backend's `app.services.mask_objects`). Nothing here
 * re-derives that geometry, so the numbers on a project card, in the workspace
 * panel and in this file's callers cannot drift apart - and switching frame no
 * longer costs a raster pass over a million-cell grid.
 */

import { DEFECT_CLASSES, IMAGE_STATUSES } from '@/types/api'
import type { Defect, DefectClass, ImageStatus, MaskRing, ReviewFrame } from '@/types/api'

export interface DefectClassStyle {
  /** Catalogue key for the class name - a translated screen calls `t()` on it. */
  labelKey: string
  /**
   * The English wording, for the screens that are not translated yet (the
   * wizard's later steps). Kept beside `labelKey` so the class vocabulary stays
   * in one table; it can go once every screen reads the catalogue.
   */
  label: string
  /** Tailwind classes for the pill on a card. */
  badge: string
  /** SVG stroke for the mask, in inline-style form (not a Tailwind class). */
  stroke: string
  /** Translucent mask fill. */
  fill: string
}

export const DEFECT_CLASS_STYLES: Record<DefectClass, DefectClassStyle> = {
  crack: {
    labelKey: 'defectClass.crack',
    label: 'Crack',
    badge: 'bg-amber-50 text-amber-700 ring-amber-200',
    stroke: '#f59e0b',
    fill: 'rgba(245, 158, 11, 0.28)',
  },
  spalling: {
    labelKey: 'defectClass.spalling',
    label: 'Spalling',
    badge: 'bg-violet-50 text-violet-700 ring-violet-200',
    stroke: '#8b5cf6',
    fill: 'rgba(139, 92, 246, 0.28)',
  },
}

export function defectClassStyle(defectClass: DefectClass): DefectClassStyle {
  return DEFECT_CLASS_STYLES[defectClass] ?? DEFECT_CLASS_STYLES[DEFECT_CLASSES.CRACK]
}

/**
 * Which annotation tool is armed on the review canvas.
 *
 * `null` is a real state and the default: the canvas is read-only until the
 * user picks a tool, so an idle click on a mask focuses it instead of editing
 * anything. Shared by the canvas and the panel that arms it.
 *
 * Two tools, and they are the two halves of one gesture. `brush` adds what is
 * painted to the mask being built; `erase` takes what is painted out of the
 * masks underneath and leaves the rest of them alone. Both are press, drag,
 * release - which is why they are one switch rather than two unrelated buttons.
 */
export type CanvasTool = 'brush' | 'erase' | null

/**
 * One detection's mask, ready to draw over a frame.
 *
 * A frame usually carries several detections, and the inspector highlights the
 * one currently under review, so the overlay takes a list of shapes rather than
 * a single outline: it renders them all over one copy of the image instead of
 * stacking one <img> per defect.
 *
 * `rings` holds one closed ring per connected part of that detection's mask. A
 * mask split into two patches by whatever the model saw is two rings; joining
 * them into one would draw a straight edge across the gap that is not there.
 * An object made of several detections carries all of their rings, in order.
 */
export interface MaskShape {
  rings: MaskRing[]
  defectClass: DefectClass
  /** Draws this mask above the others, with an opaque stroke. */
  active?: boolean
  /**
   * The row this shape came from. Needed by the annotation tools, which have to
   * name the defect to focus or delete when a shape is clicked; a purely
   * decorative thumbnail can leave it out.
   */
  id?: string
}

/** Formats a model confidence as a whole percentage: 0.4775 -> "48%". */
export function formatConfidence(score: number): string {
  return `${Math.round(score * 100)}%`
}

/**
 * One *object* on a frame: a defect as the building has it, not as the model
 * reported it.
 *
 * A segmentation model run at a low confidence threshold returns the same crack
 * several times, as overlapping fragments; drawn as-is, the canvas shows the
 * model's uncertainty instead of the damage. An object is therefore every
 * detection that shares an `object_index` on one frame: `rings` are the members'
 * own outlines - never a re-traced union, because the masks are frequently
 * thinner than any grid can represent - and `members` keeps each detection that
 * went into it, because confirmations are still recorded per detection.
 */
export interface MaskObject {
  /** Representative detection: the object's identity in the URL and the panel. */
  id: string
  defectClass: DefectClass
  rings: MaskRing[]
  members: Defect[]
}

/**
 * Fold a frame's detections into the objects they describe.
 *
 * Pure grouping, no geometry: `object_index` already says which detections are
 * one thing, and it was assigned where the masks were written. Callers pass the
 * detections of **one** frame (the workspace does), and the key is the frame plus
 * the index even so - object indices are per frame, and folding two frames'
 * first objects together is the kind of mistake this should not be able to make.
 *
 * Objects come back ordered by `object_index`, which is the order the detector
 * produced the damage in: stable across requests rather than following whatever
 * order the rows happened to be read in.
 */
export function mergeDetections(defects: readonly Defect[]): MaskObject[] {
  const byObject = new Map<string, MaskObject>()

  for (const defect of defects) {
    const key = `${defect.image_id}:${defect.object_index}`
    const existing = byObject.get(key)
    if (existing) {
      existing.members.push(defect)
      existing.rings.push(...(defect.mask_polygon ?? []))
      continue
    }
    byObject.set(key, {
      id: defect.id,
      defectClass: defect.defect_class,
      rings: [...(defect.mask_polygon ?? [])],
      members: [defect],
    })
  }

  return [...byObject.values()].sort(
    (left, right) => (left.members[0]?.object_index ?? 0) - (right.members[0]?.object_index ?? 0),
  )
}

/** True when a detection is the one an object is identified by. */
export function isObjectFocused(object: MaskObject, defectId: string | null): boolean {
  return defectId !== null && object.members.some((member) => member.id === defectId)
}

/** The object's confidence: its most confident detection, which is the best
 *  evidence any part of it was found at all. */
export function objectConfidence(object: MaskObject): number {
  return object.members.reduce((best, member) => Math.max(best, member.confidence_score), 0)
}

/** An object counts as confirmed only once every detection in it is. */
export function objectVerified(object: MaskObject): boolean {
  return object.members.every((member) => member.is_verified)
}

/**
 * Every detection found on one frame.
 *
 * The 2D inspector navigates *frames*, not defects: a facade panel usually has
 * several cracks on it, and reviewing one at a time would mean loading the same
 * photo over and over. Grouping is derived from the defect list, so it needs no
 * extra request.
 */
export interface FrameGroup {
  imageId: string
  imageUrl: string | null
  filePath: string
  /** Pipeline status of the frame, shown as a chip while it is still moving. */
  status: ImageStatus
  defects: Defect[]
}

/**
 * Pairs a project's frames with the detections that sit on them.
 *
 * Frames come first and in the order the API returned them (upload order), so
 * the reviewer's carousel can page through the *whole sortie*: a photo the
 * model found nothing on still appears, with an empty `defects` list. Building
 * this from the defect list alone - as it used to be done - silently dropped
 * exactly those photos, which is what made a clean facade look like a missing
 * frame.
 *
 * The fallback branch covers a detection whose frame was not in the page the
 * API returned (a frame deleted between the two requests). It is defensive: the
 * shape is drawn rather than dropped, but the group cannot claim a status it
 * does not know, so it is treated as a completed frame.
 */
export function groupByFrame(
  frames: readonly ReviewFrame[],
  defects: readonly Defect[],
): FrameGroup[] {
  const groups: FrameGroup[] = []
  const byImage = new Map<string, FrameGroup>()

  for (const frame of frames) {
    const group: FrameGroup = {
      imageId: frame.id,
      imageUrl: frame.image_url,
      filePath: frame.file_path,
      status: frame.status,
      defects: [],
    }
    byImage.set(frame.id, group)
    groups.push(group)
  }

  for (const defect of defects) {
    const existing = byImage.get(defect.image_id)
    if (existing) {
      existing.defects.push(defect)
      continue
    }
    const orphan: FrameGroup = {
      imageId: defect.image_id,
      imageUrl: defect.image_url,
      filePath: defect.image_file_path,
      status: IMAGE_STATUSES.COMPLETED,
      defects: [defect],
    }
    byImage.set(defect.image_id, orphan)
    groups.push(orphan)
  }

  return groups
}
