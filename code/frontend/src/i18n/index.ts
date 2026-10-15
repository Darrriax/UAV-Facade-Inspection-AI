/**
 * Vue I18n instance and the app's locale policy.
 *
 * The message catalogues live in `locales/*.json`, one file per language, keyed
 * by feature (`auth.login.*`, `app.*`). `en.json` is the source of truth for the
 * schema: `MessageSchema` is inferred from it, so `createI18n` below fails the
 * type check the moment another catalogue loses a key. A typo *at a call site*
 * is not caught the same way - vue-i18n types `t()` to accept any string - so
 * there a missing key surfaces as the key itself plus a dev-time warning.
 *
 * The translated surfaces are the auth screens, the signed-in shell, the
 * dashboard, the whole creation wizard (details, upload, processing), the
 * workspace and the 3D model page - viewer included; what is left is the frame
 * inspector's empty state, the profile placeholder and the Admin area, which
 * still read from their own English literals. Their keys belong in their own
 * namespaces (`inspector.*`, `admin.*`) as they get translated - nothing here
 * needs to change to add them.
 *
 * `legacy: false` selects the Composition API: the global locale is a ref, and
 * `t`/`locale` come from `useI18n()` rather than from `this.$t`.
 */

import { createI18n } from 'vue-i18n'

import { readStoredLocale, writeStoredLocale } from '@/utils/storage'

import en from './locales/en.json'
import uk from './locales/uk.json'

/**
 * The languages the UI is actually written in. `label` is each language's own
 * name - a switcher that renders "Ukrainian" while the interface is Ukrainian
 * is the one label a reader may not recognise. `dateLocale` is the Intl tag
 * behind the code: a bare `en` formats dates US-style ("Oct 14, 2026"), while
 * the screens are laid out around "14 Oct 2026".
 *
 * `shortLabel` is the code the compact header switch shows. `UA` rather than
 * the ISO 639-1 `uk` on purpose: Ukrainian interfaces write it that way, and
 * "UK" in a language button is read as the United Kingdom.
 */
export const SUPPORTED_LOCALES = [
  { code: 'en', label: 'English', shortLabel: 'EN', dateLocale: 'en-GB' },
  { code: 'uk', label: 'Українська', shortLabel: 'UA', dateLocale: 'uk-UA' },
] as const

export type AppLocale = (typeof SUPPORTED_LOCALES)[number]['code']

/** Inferred from `en.json`; every other catalogue must match it. */
export type MessageSchema = typeof en

/** What the untranslated screens are written in. */
const DEFAULT_LOCALE: AppLocale = 'en'

function isSupported(value: unknown): value is AppLocale {
  return SUPPORTED_LOCALES.some(({ code }) => code === value)
}

/**
 * The locale to start in: the reader's own last choice, else what their browser
 * asks for (`uk-UA` -> `uk`), else English.
 *
 * The stored value is validated rather than trusted - storage is editable and
 * survives upgrades that may drop a language.
 */
function initialLocale(): AppLocale {
  const stored = readStoredLocale()
  if (isSupported(stored)) return stored

  for (const tag of navigator.languages ?? []) {
    const base = tag.toLowerCase().split('-')[0]
    if (base !== undefined && isSupported(base)) return base
  }

  return DEFAULT_LOCALE
}

/**
 * Plural selection for Ukrainian.
 *
 * vue-i18n ships an English-shaped rule only (`1` vs *everything else*), while
 * Ukrainian has three forms - one, few (2-4) and many - so a message like
 * `{n} дефект | {n} дефекти | {n} дефектів` needs its own selector. Anything
 * ending in 1 but not 11 is "one" (1, 21, 31…); 2-4 - except 12-14 - are
 * "few" (2-4, 22-24…); the rest, including 0 and the teens, are "many".
 *
 * A message with a single form never consults this (there is nothing to
 * choose); a two-form message falls back to one/other.
 */
function ukrainianPlural(choice: number, choicesLength: number): number {
  const count = Math.abs(choice)
  if (choicesLength <= 1) return 0

  const one = count % 10 === 1 && count % 100 !== 11
  const few = count % 10 >= 2 && count % 10 <= 4 && (count % 100 < 10 || count % 100 >= 20)

  if (choicesLength === 2) return one ? 0 : 1
  return one ? 0 : few ? 1 : 2
}

export const i18n = createI18n<[MessageSchema], AppLocale, false>({
  legacy: false,
  locale: initialLocale(),
  fallbackLocale: DEFAULT_LOCALE,
  messages: { en, uk },
  pluralRules: { uk: ukrainianPlural },
})

/**
 * The Intl locale behind an app locale code, for `Intl.DateTimeFormat` and
 * friends. Unknown codes fall back to English - the same policy as the boot
 * locale, and never a crash for a value that came out of storage.
 */
export function dateLocaleFor(locale: string): string {
  const option = SUPPORTED_LOCALES.find((candidate) => candidate.code === locale)
  return option?.dateLocale ?? 'en-GB'
}

/** Keeps the document's own language in step with the active locale. */
export function applyDocumentLanguage(locale: AppLocale = i18n.global.locale.value): void {
  document.documentElement.lang = locale
}

/**
 * Switch language from a component. Persisted so the choice survives a reload;
 * `<html lang>` follows immediately, which is what screen readers and the
 * browser's own hyphenation read.
 */
export function setLocale(locale: AppLocale): void {
  i18n.global.locale.value = locale
  writeStoredLocale(locale)
  applyDocumentLanguage(locale)
}
