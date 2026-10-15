<script setup lang="ts">
/**
 * Interface language switch.
 *
 * Two states rendered as one segmented control rather than a dropdown: the app
 * supports two languages, both fit inline, and the active one is visible
 * without opening anything. Each option is labelled in its own language - that
 * is the only label a reader is guaranteed to recognise - and carries `lang`
 * so a screen reader pronounces it correctly.
 *
 * The switch itself is silent for the rest of the app: writing the locale on
 * the i18n instance is what re-renders every translated string, and `lang` on
 * `<html>` is updated by `setLocale`.
 */

import { useI18n } from 'vue-i18n'

import { SUPPORTED_LOCALES, setLocale } from '@/i18n'

// No options: `useI18n()` then resolves to the app-wide composer, which is the
// one `setLocale` writes to.
const { t, locale } = useI18n()
</script>

<template>
  <div
    class="inline-flex items-center gap-0.5 rounded-full border border-slate-200 bg-slate-100 p-0.5"
    role="group"
    :aria-label="t('language.label')"
  >
    <button
      v-for="option in SUPPORTED_LOCALES"
      :key="option.code"
      type="button"
      :lang="option.code"
      :aria-pressed="locale === option.code"
      class="rounded-full px-3 py-1 text-xs font-medium transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
      :class="
        locale === option.code
          ? 'bg-white text-slate-900 shadow-sm'
          : 'text-slate-500 hover:text-slate-900'
      "
      @click="setLocale(option.code)"
    >
      {{ option.label }}
    </button>
  </div>
</template>
