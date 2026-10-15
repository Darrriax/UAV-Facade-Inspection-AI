/**
 * Tiny typed wrapper around `localStorage`.
 *
 * Two things are persisted: the access token and the chosen interface language.
 * The token is a trade-off, and a conscious one: a token in Web Storage is
 * readable by any script on the origin, whereas an httpOnly cookie cannot be
 * read by the SPA at all. Cookies would be the safer default, but they need a
 * server-side BFF (or CSRF handling on every mutation plus a cookie-based login
 * endpoint) that this API does not have - it issues bearer tokens. Revisit
 * before production: shorter token TTL plus a refresh flow is the cheapest
 * hardening step. The locale is not sensitive and is read straight back by the
 * i18n layer, which validates it.
 *
 * Every access is wrapped because storage throws in private-mode Safari and
 * when cookies are blocked, and a login screen must not crash over it.
 */

const TOKEN_KEY = 'facade.auth.access-token'
const LOCALE_KEY = 'facade.ui.locale'

function storage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function readStoredToken(): string | null {
  try {
    return storage()?.getItem(TOKEN_KEY) ?? null
  } catch {
    return null
  }
}

export function writeStoredToken(token: string | null): void {
  try {
    if (token === null) {
      storage()?.removeItem(TOKEN_KEY)
      return
    }
    storage()?.setItem(TOKEN_KEY, token)
  } catch {
    // Storage unavailable or full: the session then simply does not survive a
    // reload, which is better than failing the login itself.
  }
}

/**
 * The reader's language choice, or `null` if they never made one (or storage is
 * unreachable). Validated by the caller against the supported locales.
 */
export function readStoredLocale(): string | null {
  try {
    return storage()?.getItem(LOCALE_KEY) ?? null
  } catch {
    return null
  }
}

export function writeStoredLocale(locale: string): void {
  try {
    storage()?.setItem(LOCALE_KEY, locale)
  } catch {
    // Storage unavailable: the language still switches, it just resets on the
    // next reload instead of taking the switch down with it.
  }
}
