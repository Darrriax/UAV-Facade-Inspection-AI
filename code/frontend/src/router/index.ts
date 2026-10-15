/**
 * Application router.
 *
 * `createWebHistory` (HTML5 history) rather than a hash: this is a single-page
 * application served behind a real web server, so `/projects/<id>/workspace` is
 * a URL the user can bookmark and paste. That server must therefore fall back
 * to `index.html` for unmatched paths - Vite's dev server does it already, and
 * `sites`-style static hosting needs the same rule in production.
 *
 * RBAC lives in the route `meta` and is enforced by `installGuards`; see
 * `meta.d.ts` for the shape of the metadata.
 */

import { createRouter, createWebHistory } from 'vue-router'

import { installGuards } from './guards'
import { routes } from './routes'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  /**
   * Restore the scroll position on back/forward, but start every *new* route at
   * the top. Without the hash/`savedPosition` handling, going back from a long
   * defect list lands the user at the bottom of the previous screen.
   */
  scrollBehavior(_to, _from, savedPosition) {
    return savedPosition ?? { top: 0 }
  },
})

installGuards(router)

export default router
