/**
 * Every route name in one place.
 *
 * Names are the API of the route table: `router.push({ name: ... })` survives a
 * path change, and a typo is a compile error instead of a silent no-op
 * navigation. Keys are camelCase, values are dotted and hierarchical so a
 * DevTools route list reads like the folder structure.
 */

import type { UserRole } from '@/types/api'

export const ROUTE_NAMES = {
  // public
  login: 'login',
  forbidden: 'forbidden',
  notFound: 'not-found',
  // shared, authenticated
  home: 'home',
  profile: 'profile',
  profileGeneral: 'profile.general',
  profileSecurity: 'profile.security',
  // inspector
  dashboard: 'dashboard',
  projectNew: 'projects.new',
  projectDetail: 'projects.detail',
  projectPhotos: 'projects.photos',
  projectProgress: 'projects.progress',
  projectWorkspace: 'projects.workspace',
  projectModel3d: 'projects.model3d',
  // admin
  admin: 'admin',
  adminInspectors: 'admin.inspectors',
  adminInspectorNew: 'admin.inspectors.new',
  adminInspectorDetail: 'admin.inspectors.detail',
  adminProjects: 'admin.projects',
} as const

export type RouteName = (typeof ROUTE_NAMES)[keyof typeof ROUTE_NAMES]

/** Where each role lands after signing in. */
export const LANDING_ROUTE_BY_ROLE: Record<UserRole, RouteName> = {
  admin: ROUTE_NAMES.adminInspectors,
  inspector: ROUTE_NAMES.dashboard,
}
