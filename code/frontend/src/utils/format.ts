/** Display formatting shared by the views. */

/**
 * Short, unambiguous date: "12 Oct 2026", "12 жовт. 2026 р.".
 *
 * The formatter is per Intl locale rather than one pinned to `en-GB`: a screen
 * that speaks Ukrainian has to write its dates the way Ukrainian does (the
 * translated dashboard passes `dateLocaleFor(locale)` from the i18n module).
 * `en-GB` remains the default so the screens that are not translated yet keep
 * the format they were laid out around, region included. Formatters are cached
 * because building one is the expensive part of `Intl` and a grid renders a
 * date per card.
 */
const shortDateFormatters = new Map<string, Intl.DateTimeFormat>()

function shortDateFormatter(locale: string): Intl.DateTimeFormat {
  let formatter = shortDateFormatters.get(locale)
  if (formatter === undefined) {
    formatter = new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'short', year: 'numeric' })
    shortDateFormatters.set(locale, formatter)
  }
  return formatter
}

/** Formats an ISO timestamp; an unparseable value degrades to an em dash. */
export function formatDate(
  value: string | number | Date | null | undefined,
  locale = 'en-GB',
): string {
  if (value === null || value === undefined) return '—'
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : shortDateFormatter(locale).format(date)
}

/**
 * Wall-clock time with seconds: "18:56:31".
 *
 * For "last updated" lines, where the useful question is "how fresh is this?" -
 * a date is noise when a screen refreshes every few seconds.
 */
const TIME_OF_DAY = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

export function formatTime(value: string | number | Date | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : TIME_OF_DAY.format(date)
}
