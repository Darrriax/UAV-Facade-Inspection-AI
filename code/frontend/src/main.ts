/**
 * Application entry point.
 *
 * The bootstrap is an async function rather than top-level code, and the ordering
 * inside it matters:
 *
 * 1. `app.use(i18n)` before mounting. The locale is resolved from storage and
 *    the browser inside the i18n module, so installing it here is what makes
 *    the first paint appear in the reader's language instead of flashing
 *    English; `applyDocumentLanguage()` puts the same value on `<html lang>`,
 *    which `index.html` can only guess at statically.
 * 2. `setUnauthorizedHandler()` - the HTTP layer has no store and no router of
 *    its own, so the "your session ended" reaction is wired here, where both
 *    exist.
 * 3. `restoreSession()` **before** `app.use(router)`. The access token lives in
 *    `localStorage`, so a reload has a token but no profile. Installing the
 *    router triggers the first navigation, and its guard has to see the real
 *    role: otherwise an Admin reopening `/` is routed as an Inspector. The
 *    request is shared with the guard (see the store), so whichever of the two
 *    arrives first, the app makes exactly one call to `GET /users/me`.
 * 4. `router.isReady()` before mounting, so the first paint is the resolved
 *    route and not an empty router view.
 */

import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from '@/App.vue'
import router from '@/router'
import { setUnauthorizedHandler } from '@/api/client'
import { applyDocumentLanguage, i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

// `@/assets/main.css` is deliberately not imported here: `index.html` links it,
// so it downloads alongside this module instead of after it.

const pinia = createPinia()

const app = createApp(App)
app.use(pinia)
// The locale the instance resolved (stored choice -> browser -> English) is
// also what `<html lang>` has to say; index.html can only ship a static guess.
app.use(i18n)
applyDocumentLanguage()

const auth = useAuthStore(pinia)

setUnauthorizedHandler(() => {
  // Already signed out (or the login request itself failed): nothing to do.
  if (!auth.accessToken) return

  auth.clearSession()
  const current = router.currentRoute.value
  if (current.meta.requiresAuth) {
    void router.push({ name: 'login', query: { redirect: current.fullPath } })
  }
})

async function bootstrap(): Promise<void> {
  // Restore the session *before* the router is installed. Installing the router
  // triggers the first navigation, and that navigation's guard has to see the
  // real role - otherwise an Admin reopening `/` is routed as an Inspector.
  // `restoreSession()` is shared with the guard, so this costs one request no
  // matter which of the two wins the race.
  await auth.restoreSession()

  app.use(router)
  // Let the initial navigation resolve before the first paint, so the app does
  // not flash an empty router view. A failed lookup still mounts: the app is
  // usable, just signed out.
  await router.isReady()
  app.mount('#app')
}

void bootstrap()
