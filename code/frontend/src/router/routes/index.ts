/**
 * The route table.
 *
 * Split by audience rather than by URL prefix, because that is the axis the
 * access rules are written on:
 *
 *   public.ts     - reachable while signed out (login, error pages)
 *   inspector.ts  - the Inspector's work: dashboard, wizard, workspace, profile
 *   admin.ts      - the Admin's work: inspectors, all projects
 *
 * The `/` landing route lives here because it belongs to no audience: it
 * resolves to the screen the *current role* starts from.
 */

import type { RouteLocationRaw, RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import type { UserRole } from '@/types/api'

import { adminRoutes } from './admin'
import { inspectorRoutes } from './inspector'
import { LANDING_ROUTE_BY_ROLE, ROUTE_NAMES } from './names'
import { publicRoutes } from './public'

/** Where a role starts after signing in (also used by the login view). */
export function landingRouteFor(role: UserRole | null): RouteLocationRaw {
  return { name: role ? LANDING_ROUTE_BY_ROLE[role] : ROUTE_NAMES.login }
}

const homeRoute: RouteRecordRaw = {
  path: '/',
  name: ROUTE_NAMES.home,
  /**
   * Resolved, not static: Inspectors and Admins start on different screens.
   *
   * `useAuthStore()` needs an installed Pinia instance, which is guaranteed
   * here: `main.ts` installs Pinia and restores the session *before* mounting
   * the app, and a redirect callback only runs during a navigation.
   */
  redirect: () => landingRouteFor(useAuthStore().role),
  meta: { requiresAuth: true, title: 'Home' },
}

export const routes: RouteRecordRaw[] = [
  homeRoute,
  // Public first, then the two authenticated areas. Order matters only inside a
  // level (Vue Router ranks by path specificity), but reading it top-down tells
  // the story of what is reachable to whom.
  ...publicRoutes,
  ...inspectorRoutes,
  ...adminRoutes,
]
