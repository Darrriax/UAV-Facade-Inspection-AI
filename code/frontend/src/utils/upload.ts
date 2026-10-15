/**
 * Transport for large uploads: how a selection is split, how the pieces are
 * sent, and how a dropped folder is read.
 *
 * A 400-frame sortie is gigabytes. Sending it as one multipart request would
 * build that body in memory, and the API caps a request at 100 files anyway -
 * so the selection is split into chunks and the chunks are sent a few at a
 * time. The logic lives here rather than in the view because it is the part
 * with the sharp edges (partial failure, cancellation, in-flight accounting),
 * and it has no dependency on Vue.
 */

import { ACCEPTED_IMAGE_TYPES, MAX_FILE_BYTES } from '@/api/projects'

/**
 * Chunk policy: bounded on **both** axes.
 *
 * Files alone are not a bound: twenty 10 MB DJI frames are a 200 MB request,
 * which is exactly the shape that breaks a browser tab or a proxy. Bytes alone
 * are not one either, since a thousand 40 KB thumbnails would become a single
 * enormous request. A chunk closes when either limit would be exceeded, and a
 * file larger than the byte cap travels alone.
 *
 * 20 files / 96 MiB with three in flight keeps the worst case around 300 MB of
 * buffered payload, while the API's own cap (100 files per request) stays a
 * safety net the client never reaches.
 */
export const UPLOAD_CHUNK_MAX_FILES = 20
export const UPLOAD_CHUNK_MAX_BYTES = 96 * 1024 * 1024

/**
 * How many chunks may be in flight.
 *
 * One at a time would make a 4 GB sortie take as long as its slowest piece
 * forever; more than a handful multiplies memory and competes for the same disk
 * and network on the server, which is where the work actually is.
 */
export const UPLOAD_CONCURRENCY = 3

export interface UploadChunk {
  /** 1-based, for messages: "batch 3 of 21". */
  number: number
  files: File[]
  bytes: number
}

export interface ChunkPlanOptions {
  maxFiles?: number
  maxBytes?: number
}

/** Split files into chunks that respect both limits, preserving order. */
export function planUploadChunks(
  files: readonly File[],
  options: ChunkPlanOptions = {},
): UploadChunk[] {
  const maxFiles = options.maxFiles ?? UPLOAD_CHUNK_MAX_FILES
  const maxBytes = options.maxBytes ?? UPLOAD_CHUNK_MAX_BYTES
  const chunks: UploadChunk[] = []

  let current: File[] = []
  let bytes = 0

  const close = (): void => {
    if (current.length === 0) return
    chunks.push({ number: chunks.length + 1, files: current, bytes })
    current = []
    bytes = 0
  }

  for (const file of files) {
    const wouldOverflow =
      current.length >= maxFiles || (current.length > 0 && bytes + file.size > maxBytes)
    if (wouldOverflow) close()
    current.push(file)
    bytes += file.size
  }
  close()

  return chunks
}

/** What the progress indicator renders, in whole files and whole batches. */
export interface UploadProgress {
  totalFiles: number
  totalChunks: number
  completedFiles: number
  completedChunks: number
  inFlight: number
}

export interface UploadFailure {
  chunk: UploadChunk
  error: unknown
}

export interface UploadRunResult {
  /** Files the server confirmed storing - the caller prunes exactly these. */
  storedFiles: File[]
  /** The first chunk that failed; the run stops there. */
  failure: UploadFailure | null
  /** True when the caller cancelled; not a failure and never a queue trigger. */
  aborted: boolean
}

export interface UploadRunOptions {
  concurrency?: number
  signal?: AbortSignal
  onProgress?: (progress: UploadProgress) => void
}

/**
 * Send the chunks, at most `concurrency` at a time.
 *
 * Failure policy: **stop at the first failed chunk**. Already-sent chunks are
 * reported back instead of being rolled back - they are durable rows, and
 * re-sending them would duplicate frames - so the caller keeps only the files
 * that were *not* stored and can retry with those. Nothing is thrown: the
 * result describes what happened, which keeps abort / failure / success as three
 * ordinary outcomes rather than three error types to tell apart.
 *
 * `send` returns the number of files the server acknowledged for its chunk.
 */
export async function uploadInChunks(
  chunks: readonly UploadChunk[],
  send: (chunk: UploadChunk, signal: AbortSignal) => Promise<number>,
  options: UploadRunOptions = {},
): Promise<UploadRunResult> {
  const concurrency = Math.max(1, options.concurrency ?? UPLOAD_CONCURRENCY)
  const signal = options.signal ?? new AbortController().signal
  // An internal controller so the pool can stop the chunks it is no longer
  // waiting for once one of them has failed.
  const stop = new AbortController()
  const forwardAbort = (): void => stop.abort()
  signal.addEventListener('abort', forwardAbort, { once: true })

  const totalFiles = chunks.reduce((sum, chunk) => sum + chunk.files.length, 0)
  let completedFiles = 0
  let completedChunks = 0
  let inFlight = 0
  const storedFiles: File[] = []
  let failure: UploadFailure | null = null
  let aborted = false

  const report = (): void => {
    options.onProgress?.({
      totalFiles,
      totalChunks: chunks.length,
      completedFiles,
      completedChunks,
      inFlight,
    })
  }
  report()

  let next = 0

  async function worker(): Promise<void> {
    for (;;) {
      if (failure !== null || aborted) return
      const index = next
      next += 1
      const chunk = chunks[index]
      if (chunk === undefined) return

      inFlight += 1
      report()
      try {
        await send(chunk, stop.signal)
        storedFiles.push(...chunk.files)
        completedChunks += 1
        completedFiles += chunk.files.length
      } catch (error) {
        if (signal.aborted) {
          // A cancellation is not a chunk failure: the client asked for this.
          aborted = true
          stop.abort()
        } else {
          // The *first* failure is the one reported. Siblings still in flight
          // are aborted right here and reject with an abort error, which says
          // nothing about why the run stopped - letting one of those overwrite
          // the real error would leave the user with "the operation was
          // aborted" instead of "storage rejected the batch".
          if (failure === null) failure = { chunk, error }
          // Do not start further chunks, and let the ones already running finish
          // so their stored files are accounted for.
          stop.abort()
        }
      } finally {
        inFlight -= 1
        report()
      }
    }
  }

  try {
    await Promise.all(Array.from({ length: Math.min(concurrency, chunks.length) }, worker))
  } finally {
    signal.removeEventListener('abort', forwardAbort)
  }

  return { storedFiles, failure, aborted }
}

// ------------------------------------------------------------------ dropping
/**
 * Files a dropped selection contains, folders included.
 *
 * `dataTransfer.files` is empty when a *folder* is dropped, so without the entry
 * API a user dragging the sortie folder onto the drop zone would watch nothing
 * happen. `webkitGetAsEntry()` is non-standard but implemented by every browser
 * that matters here (Chrome, Edge, Safari); when it is missing, the plain file
 * list is the fallback.
 *
 * A broken entry resolves to nothing rather than rejecting the whole drop: one
 * unreadable file must not discard four hundred good ones.
 */
export async function filesFromDataTransfer(dataTransfer: DataTransfer | null): Promise<File[]> {
  const items = dataTransfer?.items
  const supportsEntries =
    items !== undefined &&
    items.length > 0 &&
    typeof (items[0] as DataTransferItem & { webkitGetAsEntry?: unknown }).webkitGetAsEntry ===
      'function'

  if (!supportsEntries) return Array.from(dataTransfer?.files ?? [])

  const roots: FileSystemEntry[] = []
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index] as DataTransferItem & {
      webkitGetAsEntry?: () => FileSystemEntry | null
    }
    if (item.kind !== 'file') continue
    const entry = item.webkitGetAsEntry?.() ?? null
    if (entry !== null) roots.push(entry)
  }

  const files: File[] = []
  for (const root of roots) await walkEntry(root, 0, files)
  return files
}

/** Guards against a symlinked or pathological tree; the UI has no depth limit. */
const MAX_DROP_DEPTH = 8

async function walkEntry(entry: FileSystemEntry, depth: number, out: File[]): Promise<void> {
  if (depth > MAX_DROP_DEPTH) return

  if (entry.isFile) {
    const file = await readFileEntry(entry as FileSystemFileEntry)
    if (file !== null) out.push(file)
    return
  }

  if (entry.isDirectory) {
    const children = await readDirectory(entry as FileSystemDirectoryEntry)
    for (const child of children) await walkEntry(child, depth + 1, out)
  }
}

function readFileEntry(entry: FileSystemFileEntry): Promise<File | null> {
  return new Promise((resolve) => {
    entry.file(
      (file) => resolve(file),
      () => resolve(null),
    )
  })
}

/** Read a directory to the end: `readEntries` returns batches, not everything. */
function readDirectory(entry: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> {
  return new Promise((resolve) => {
    const reader = entry.createReader()
    const entries: FileSystemEntry[] = []

    const readBatch = (): void => {
      reader.readEntries(
        (batch) => {
          if (batch.length === 0) {
            resolve(entries)
            return
          }
          entries.push(...batch)
          readBatch()
        },
        () => resolve(entries),
      )
    }

    readBatch()
  })
}

// ------------------------------------------------------- the directory picker
/**
 * The File System Access API's directory picker, as this project needs it.
 *
 * `showDirectoryPicker` is not in the DOM typings the project compiles against,
 * so it is described here instead of being cast away at the call site.
 */
interface DirectoryPickerHost {
  showDirectoryPicker?: (options?: { mode?: 'read' }) => Promise<FileSystemDirectoryHandle>
}

/** What an attempt to pick a folder produced. */
export type DirectoryPickResult =
  /** The reader chose a folder: every file in it, recursively. */
  | { status: 'picked'; files: File[] }
  /** The dialog was dismissed - an answer, not a failure, and not a fallback. */
  | { status: 'cancelled' }
  /** No picker in this browser (Firefox, Safari), or it refused: use the input. */
  | { status: 'unavailable' }

/**
 * Ask for a folder and read everything in it.
 *
 * `showDirectoryPicker()` is the API that exists *for this gesture*: the dialog
 * it opens offers directories and nothing else, which is the whole point of the
 * button. The `webkitdirectory` input is the older spelling of the same idea and
 * remains the fallback for browsers without it - but it is a hidden *file* input
 * asked to behave like a folder chooser, and whether it obliges is the browser's
 * business, not ours.
 *
 * The files come back with bare names, the same shape a dropped folder produces
 * (the entry API drops subpaths too), so the selection list keeps showing
 * `file.name` and nothing here has to know about paths.
 */
export async function pickDirectory(): Promise<DirectoryPickResult> {
  const picker = (window as unknown as DirectoryPickerHost).showDirectoryPicker
  if (typeof picker !== 'function') return { status: 'unavailable' }

  try {
    const handle = await picker({ mode: 'read' })
    return { status: 'picked', files: await filesInDirectory(handle) }
  } catch (cause) {
    // Closing the dialog rejects with AbortError. Reporting that as unavailable
    // would make the caller open a second picker on top of the first refusal.
    if (cause instanceof DOMException && cause.name === 'AbortError') return { status: 'cancelled' }
    return { status: 'unavailable' }
  }
}

/** Every file under `handle`, depth-first, with the same depth guard as a drop. */
async function filesInDirectory(handle: FileSystemDirectoryHandle, depth = 0): Promise<File[]> {
  if (depth > MAX_DROP_DEPTH) return []

  const files: File[] = []
  for await (const entry of handle.values()) {
    if (entry.kind === 'file') {
      files.push(await (entry as FileSystemFileHandle).getFile())
    } else {
      files.push(...(await filesInDirectory(entry as FileSystemDirectoryHandle, depth + 1)))
    }
  }
  return files
}

// ------------------------------------------------------------------ filtering
/** Files that come along with a folder but are not part of the sortie. */
const SYSTEM_FILENAMES = new Set(['.DS_Store', 'Thumbs.db', 'desktop.ini', 'Icon\r'])

/**
 * macOS/Windows bookkeeping files.
 *
 * A DJI folder copied to a Mac carries an AppleDouble twin (`._DJI_0001.JPG`)
 * for every frame; reporting 395 of those as "skipped, not an image" would bury
 * the one file that actually needed attention, so they are dropped silently.
 */
export function isSystemNoise(file: File): boolean {
  const base = String(file.name ?? '').split('/').pop() ?? ''
  return base.startsWith('._') || base.startsWith('~$') || SYSTEM_FILENAMES.has(base)
}

/** Re-exported so the view validates exactly what the API accepts. */
export const ACCEPTED_TYPES = ACCEPTED_IMAGE_TYPES
export const MAX_BYTES_PER_FILE = MAX_FILE_BYTES
