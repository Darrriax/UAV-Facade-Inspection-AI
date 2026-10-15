<script setup lang="ts">
/**
 * Sign-in screen.
 *
 * The heavy lifting is in the store (`auth.login`); this component owns the
 * form, its validation optics, the post-login redirect, and the language
 * switcher - the only chrome this otherwise bare screen carries. Every string
 * on it comes from the message catalogues (`src/i18n/locales`), so the screen
 * flips language without a reload.
 *
 * There is no "create an account" link: Inspectors are registered by an Admin,
 * so an invitation - not this screen - is how somebody gets credentials. A
 * self-service form here would contradict that, so the screen says exactly one
 * thing: sign in.
 */

import { computed, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import LanguageSwitcher from '@/components/LanguageSwitcher.vue'
import { ApiError, describeApiError } from '@/api/client'
import { landingRouteFor } from '@/router/routes'
import { useAuthStore } from '@/stores/auth'

const { t } = useI18n()

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const credentials = reactive({ email: '', password: '' })
const submitting = ref(false)
const errorMessage = ref<string | null>(null)

/**
 * Where to go after signing in.
 *
 * The router guard puts the page that was requested into `?redirect=`, so a
 * deep link survives the sign-in detour. Only same-origin paths are honoured:
 * forwarding an attacker-supplied absolute or protocol-relative URL would turn
 * the login form into an open redirect.
 */
const redirectTarget = computed<string | null>(() => {
  const raw = route.query.redirect
  const path = Array.isArray(raw) ? raw[0] : raw
  return typeof path === 'string' && path.startsWith('/') && !path.startsWith('//') ? path : null
})

/**
 * Sign-in failures the API phrases itself, mapped to catalogue keys.
 *
 * The mapping is by exact message, which is a stopgap: the API is a service of
 * this same project, so the two literals are as stable as the backend's own
 * tests. Anything not listed here (a 422 validation message, a transport
 * failure) is shown as the API wrote it rather than inventing a translation for
 * text this app does not own; the proper fix is error codes on the API side.
 */
const API_ERROR_KEYS = {
  'Incorrect email or password.': 'auth.login.errors.invalidCredentials',
  'This account has been deactivated. Contact an administrator.': 'auth.login.errors.deactivated',
} as const

function describeLoginError(cause: unknown): string {
  if (cause instanceof ApiError && cause.message in API_ERROR_KEYS) {
    return t(API_ERROR_KEYS[cause.message as keyof typeof API_ERROR_KEYS])
  }
  return describeApiError(cause)
}

async function submit(): Promise<void> {
  if (submitting.value) return

  submitting.value = true
  errorMessage.value = null

  try {
    await auth.login({ email: credentials.email.trim(), password: credentials.password })
    // `replace`, not `push`: the sign-in page should not come back on Back.
    await router.replace(redirectTarget.value ?? landingRouteFor(auth.role))
  } catch (cause) {
    errorMessage.value = describeLoginError(cause)
    // Wrong password is the common case, but a 422 (e.g. a malformed address)
    // should point at the offending field rather than only at the banner.
    if (cause instanceof ApiError && cause.isValidationError) {
      errorMessage.value = Object.values(cause.fieldErrors)[0] ?? errorMessage.value
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="flex min-h-screen flex-col bg-slate-100">
    <main class="flex flex-1 flex-col justify-center px-4 py-12 sm:px-6 lg:px-8">
      <div class="mx-auto w-full max-w-md">
        <!-- Brand -->
        <div class="text-center">
          <h1 class="text-2xl font-semibold tracking-tight text-slate-900">{{ t('app.name') }}</h1>
          <p class="mt-1 text-sm text-slate-500">{{ t('app.tagline') }}</p>
        </div>

        <!-- Native validation is left on: `required` + `type="email"` block the
             submit event before our handler runs, so an empty form never reaches
             the API. No `novalidate` here - it would disable exactly that. -->
        <form
          class="mt-8 space-y-5 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8"
          @submit.prevent="submit"
        >
          <div
            v-if="errorMessage"
            role="alert"
            class="flex items-start gap-2.5 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700"
          >
            <AppIcon name="alert" class="mt-0.5 h-4 w-4 shrink-0" />
            <span>{{ errorMessage }}</span>
          </div>

          <div>
            <label for="email" class="block text-sm font-medium text-slate-700">
              {{ t('auth.login.email') }}
            </label>
            <input
              id="email"
              v-model="credentials.email"
              type="email"
              name="email"
              autocomplete="email"
              required
              :disabled="submitting"
              placeholder="inspector@example.com"
              class="mt-1.5 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-xs transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 focus:outline-none disabled:bg-slate-50 disabled:text-slate-400"
            />
          </div>

          <div>
            <label for="password" class="block text-sm font-medium text-slate-700">
              {{ t('auth.login.password') }}
            </label>
            <input
              id="password"
              v-model="credentials.password"
              type="password"
              name="password"
              autocomplete="current-password"
              required
              :disabled="submitting"
              placeholder="••••••••"
              class="mt-1.5 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-xs transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 focus:outline-none disabled:bg-slate-50 disabled:text-slate-400"
            />
          </div>

          <button
            type="submit"
            :disabled="submitting"
            class="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:bg-blue-400"
          >
            <span
              v-if="submitting"
              class="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
              aria-hidden="true"
            />
            {{ submitting ? t('auth.login.submitting') : t('auth.login.submit') }}
          </button>
        </form>
      </div>
    </main>

    <!-- The switcher sits in the footer, off the way of the one task this
         screen has, and is reachable without signing in - which is exactly
         where an untranslated screen is the most likely to be met. -->
    <footer class="border-t border-slate-200 bg-white">
      <div class="mx-auto flex w-full max-w-md items-center justify-center px-4 py-3 sm:px-6">
        <LanguageSwitcher />
      </div>
    </footer>
  </div>
</template>
