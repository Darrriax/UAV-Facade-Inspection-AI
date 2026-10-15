/**
 * Country options for the profile form.
 *
 * The API stores an ISO 3166-1 alpha-2 code (validated server-side), so the form
 * must offer exactly those codes. Names are resolved through `Intl.DisplayNames`
 * rather than a hand-typed table: a translatable, always-correct list for no
 * data file, and the code is the only thing that has to stay stable.
 */

export interface CountryOption {
  /** ISO 3166-1 alpha-2, upper-case. */
  code: string
  name: string
}

/**
 * The codes offered in the picker.
 *
 * A curated set rather than every ISO region: the list stays scannable, and a
 * missing country is a one-line addition. The API still accepts any valid
 * alpha-2 code, so nothing beyond this list is ever rejected on the server.
 */
const COUNTRY_CODES = [
  'AT', 'AU', 'BE', 'BG', 'BR', 'CA', 'CH', 'CN', 'CY', 'CZ',
  'DE', 'DK', 'EE', 'ES', 'FI', 'FR', 'GB', 'GR', 'HR', 'HU',
  'IE', 'IL', 'IN', 'IT', 'JP', 'KR', 'LT', 'LU', 'LV', 'MD',
  'MT', 'NL', 'NO', 'NZ', 'PL', 'PT', 'RO', 'RS', 'SE', 'SI',
  'SK', 'TR', 'UA', 'US',
] as const

const regionNames = typeof Intl.DisplayNames === 'function'
  ? new Intl.DisplayNames(['en'], { type: 'region' })
  : null

/**
 * Flag emoji from a region code.
 *
 * Built from the two regional-indicator symbols: 'UA' -> U+1F1FA U+1F1E6.
 * Returns an empty string for anything that is not a two-letter code, so a
 * half-typed value cannot render mojibake into the form.
 */
export function flagEmoji(code: string): string {
  const upper = code.trim().toUpperCase()
  if (!/^[A-Z]{2}$/.test(upper)) return ''
  return String.fromCodePoint(
    ...upper.split('').map((letter) => 0x1f1e6 + letter.charCodeAt(0) - 65),
  )
}

export const COUNTRIES: readonly CountryOption[] = COUNTRY_CODES.map((code) => ({
  code,
  name: regionNames?.of(code) ?? code,
})).sort((a, b) => a.name.localeCompare(b.name))

/** Human-readable name for a stored code; falls back to the code itself. */
export function countryLabel(code: string | null | undefined): string {
  if (!code) return '—'
  return COUNTRIES.find((country) => country.code === code.toUpperCase())?.name ?? code.toUpperCase()
}
