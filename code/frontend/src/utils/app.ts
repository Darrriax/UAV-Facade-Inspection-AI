/** Small app-wide constants that more than one layer needs. */

/** Shown in the document title and the header. */
export const APP_NAME = 'Facade Inspection'

/**
 * Page size for the offset/limit listings.
 *
 * Matches the API's own default (and its maximum of 100), so the first page a
 * screen requests is the one the server would have produced anyway.
 */
export const DEFAULT_PAGE_SIZE = 20
export const MAX_PAGE_SIZE = 100
