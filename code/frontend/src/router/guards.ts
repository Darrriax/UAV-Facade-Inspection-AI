/**
 * Router guards - the client half of RBAC.
 *
 * The other half is the API, which re-checks the role from the database row on
 * every request. These guards only decide what to render and where to send
 * people; they are not a security boundary, and nothing here should ever be the
 * reason a mutation is allowed.
 */

import type { Router } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { APP_NAME } from '@/utils/app'

import { ROUTE_NAMES } from './routes/names'

export function installGuards(router: Router): void {
  /**
   * Return-value style guard (Vue Router 4 and 5): return nothing/`true` to
   * continue, a location to redirect, `false` to abort.
   */
  router.beforeEach(async (to) => {
    const auth = useAuthStore()

    // A token that outlived a reload, with no profile fetched yet. `main.ts`
    // starts the same restore, and `restoreSession()` shares the in-flight
    // request, so awaiting it here is free and there is no race between the two:
    // installing the router triggers this navigation *while* the bootstrap
    // request is still running.
    if (!auth.isAuthenticated && auth.accessToken !== null) {
      await auth.restoreSession()
    }

    if (to.meta.guestOnly && auth.isAuthenticated) {
      return { name: ROUTE_NAMES.home }
    }

    if (to.meta.requiresAuth && !auth.isAuthenticated) {
      // `redirect` brings the user back to the page they asked for - guarded
      // deep links are normal here (a project URL pasted into a chat).
      return { name: ROUTE_NAMES.login, query: { redirect: to.fullPath } }
    }

    const required = to.meta.role
    if (required !== undefined) {
      const allowed = Array.isArray(required) ? required : [required]
      if (auth.role === null || !allowed.includes(auth.role)) {
        // A dedicated screen, not a silent redirect home: an Inspector following
        // an admin link should be told, not quietly moved somewhere else.
        return { name: ROUTE_NAMES.forbidden }
      }
    }

    return true
  })

  router.afterEach((to) => {
    const title = to.meta.title
    document.title = title ? `${title} · ${APP_NAME}` : APP_NAME
  })
}
