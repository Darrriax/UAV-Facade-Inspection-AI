/**
 * The one place that talks HTTP.
 *
 * Everything else (stores, views) calls the typed wrappers in `api/*.ts`, so
 * the base URL, the bearer header, error normalisation and the "your session
 * ended" hook live in exactly one file.
 *
 * Built on `fetch` rather than a client library: the API is JSON plus one
 * multipart endpoint, which fetch handles natively. If upload progress for the
 * UAV batches becomes a requirement, that is the one thing fetch cannot do -
 * swap this file for an axios instance then, keeping the same exported surface.
 */

import type { Page, PaginationQuery } from '@/types/api'

/** Base URL of the REST API, without a trailing slash. */
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/+$/, '')

export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'

export type QueryValue = string | number | boolean | null | undefined

export interface RequestOptions {
  method?: HttpMethod
  /** JSON body; serialised with `JSON.stringify`. */
  body?: unknown
  /** Multipart body, for uploads. Mutually exclusive with `body`. */
  form?: FormData
  /** Appended as a query string; `null`/`undefined` entries are dropped. */
  query?: Record<string, QueryValue>
  /** Attach the bearer token. Only `/login` passes `false`. */
  auth?: boolean
  signal?: AbortSignal
}

/**
 * A non-2xx response, or a transport failure (in which case `status` is 0).
 *
 * `detail` is kept as received: FastAPI answers both `{"detail": "message"}`
 * (domain errors) and `{"detail": [{loc, msg, type}]}` (422 validation), and
 * the views need the structured form to attach messages to form fields.
 */
export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }

  get isUnauthorized(): boolean {
    return this.status === 401
  }

  get isForbidden(): boolean {
    return this.status === 403
  }

  get isNotFound(): boolean {
    return this.status === 404
  }

  get isConflict(): boolean {
    return this.status === 409
  }

  get isValidationError(): boolean {
    return this.status === 422
  }

  /**
   * FastAPI 422 details keyed by field name, ready to bind onto a form.
   *
   * The location is `["body", "country"]` (sometimes with list indices); the
   * last string element is the field the user can actually fix.
   */
  get fieldErrors(): Record<string, string> {
    if (!Array.isArray(this.detail)) return {}
    const errors: Record<string, string> = {}
    for (const item of this.detail) {
      if (typeof item !== 'object' || item === null) continue
      const { loc, msg } = item as { loc?: unknown; msg?: unknown }
      if (!Array.isArray(loc) || typeof msg !== 'string') continue
      const field = [...loc].reverse().find((part) => typeof part === 'string')
      if (typeof field === 'string' && field !== 'body') errors[field] = msg
    }
    return errors
  }
}

/**
 * Human-readable label for anything a request can throw.
 *
 * The API explains itself (``{"detail": "A user with email ... exists."}``), so
 * its message is preferred over the generic `Error.message` a transport failure
 * produces. Shared by the store and, later, by form components.
 */
export function describeApiError(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message
  if (cause instanceof Error) return cause.message
  return 'Unexpected error. Please try again.'
}

// ------------------------------------------------------------------- session
// A module-level holder, not a store import: the store imports this file, so
// importing the store back would create a cycle.
let accessToken: string | null = null

export function setAccessToken(token: string | null): void {
  accessToken = token
}

export function getAccessToken(): string | null {
  return accessToken
}

/** Called when an *authenticated* request comes back 401. */
export type UnauthorizedHandler = () => void

let unauthorizedHandler: UnauthorizedHandler | null = null

/**
 * Registered once from `main.ts`, where both the store and the router exist.
 * Keeping it out of this file is what stops `api/` from depending on the app's
 * navigation and store layers.
 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

// ------------------------------------------------------------------- requests
function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = `${API_BASE_URL}${path.startsWith('/') ? path : `/${path}`}`
  if (!query) return url

  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined) continue
    search.append(key, String(value))
  }
  const queryString = search.toString()
  return queryString ? `${url}?${queryString}` : url
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204) return null
  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) {
    // An HTML error page from a proxy, or an empty body.
    const text = await response.text().catch(() => '')
    return text || null
  }
  return response.json().catch(() => null)
}

function messageFrom(status: number, body: unknown): string {
  if (typeof body === 'string' && body.trim()) return body.trim()
  if (typeof body === 'object' && body !== null && 'detail' in body) {
    const { detail } = body as { detail: unknown }
    if (typeof detail === 'string') return detail
    // 422: fastapi returns a list; a plain join reads fine inline, and the
    // structured version is still available through `fieldErrors`.
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => (typeof item === 'object' && item !== null ? (item as { msg?: unknown }).msg : null))
        .filter((msg): msg is string => typeof msg === 'string')
      if (messages.length) return messages.join('; ')
    }
  }
  return `Request failed with status ${status}`
}

/**
 * Perform a request and return the parsed body.
 *
 * Rejects with :class:`ApiError` for every failure - including a network error,
 * which is reported as status 0 - so callers only ever catch one error type.
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, form, query, auth = true, signal } = options

  if (body !== undefined && form !== undefined) {
    throw new Error('apiRequest: pass either `body` or `form`, not both')
  }

  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  // Never set Content-Type for FormData: the browser must add the multipart
  // boundary itself.
  const tokenAttached = auth && accessToken !== null
  if (tokenAttached) headers.Authorization = `Bearer ${accessToken}`

  let response: Response
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, 'Could not reach the server. Check your connection and try again.', error)
  }

  const responseBody = await readBody(response)

  if (!response.ok) {
    if (response.status === 401 && tokenAttached) {
      // The token was sent and rejected: the session is over. Fired here rather
      // than in each caller so a stale tab cannot keep hammering the API.
      unauthorizedHandler?.()
    }
    throw new ApiError(response.status, messageFrom(response.status, responseBody), responseBody)
  }

  return responseBody as T
}

/**
 * Follow an offset/limit collection to its end.
 *
 * The Admin screens need totals, and a project can hold thousands of frames -
 * paginating explicitly is what the listings are for. Kept here so the loop is
 * written once.
 */
export async function apiRequestAllPages<T>(
  path: string,
  query: PaginationQuery & Record<string, QueryValue> = {},
  { pageSize = 100, signal }: { pageSize?: number; signal?: AbortSignal } = {},
): Promise<T[]> {
  const items: T[] = []
  let offset = 0

  for (;;) {
    const page = await apiRequest<Page<T>>(path, {
      query: { ...query, offset, limit: pageSize },
      signal,
    })
    items.push(...page.items)
    offset += pageSize
    if (offset >= page.total || page.items.length === 0) return items
  }
}
