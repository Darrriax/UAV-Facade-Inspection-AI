/**
 * Unauthenticated routes: sign-in and the two error screens.
 *
 * These are also the *only* routes without `requiresAuth`, which is the whole
 * point of splitting the table up - a reviewer can see at a glance what an
 * anonymous visitor can reach.
 */

import type { RouteRecordRaw } from 'vue-router'

import { placeholder } from '../placeholder'
import { ROUTE_NAMES } from './names'

export const publicRoutes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: ROUTE_NAMES.login,
    component: () => import('@/views/auth/LoginView.vue'),
    meta: {
      // Signed-in users are bounced to their landing screen instead of being
      // shown a login form they do not need.
      guestOnly: true,
      title: 'Sign in',
    },
  },
  {
    path: '/forbidden',
    name: ROUTE_NAMES.forbidden,
    component: placeholder('ForbiddenView'),
    meta: {
      // Reachable while signed out on purpose: the page explains that the
      // account lacks a role, and it is where a guard lands a signed-in user
      // whose role does not match the target route.
      title: 'Access denied',
    },
  },
  {
    path: '/not-found',
    name: ROUTE_NAMES.notFound,
    component: placeholder('NotFoundView'),
    meta: { title: 'Page not found' },
  },
  {
    // Kept as a redirect rather than rendering the 404 view directly, so the
    // address bar shows a real, shareable URL instead of the typo.
    //
    // `params: {}` is not decoration: a redirect target that declares no params
    // inherits them from the current location, so the catch-all's `pathMatch`
    // would be carried into `/not-found`, which has no such param. Vue Router
    // logs R0100 ("Discarded invalid param(s)") for exactly that.
    path: '/:pathMatch(.*)*',
    name: 'catch-all',
    redirect: () => ({ name: ROUTE_NAMES.notFound, params: {} }),
  },
]
