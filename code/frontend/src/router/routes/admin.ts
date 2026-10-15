/**
 * Admin routes.
 *
 * Everything here is `role: 'admin'`: the inspector list with per-account
 * storage usage and the activate/deactivate switch, the create-inspector form,
 * and the read-only overview of every project on the installation.
 *
 * A single `role` value is enough - unlike the project routes an Admin never
 * shares these screens with an Inspector, since an Inspector's view of their
 * own projects already exists at `/dashboard`.
 */

import type { RouteRecordRaw } from 'vue-router'

import { placeholder } from '../placeholder'
import { ROUTE_NAMES } from './names'

export const adminRoutes: RouteRecordRaw[] = [
  {
    path: '/admin',
    name: ROUTE_NAMES.admin,
    component: placeholder('AdminLayoutView', { nest: true }),
    // The inspector list is the Admin landing screen, so `/admin` and the
    // role-based redirect from `/` both end up here. Declared on the parent so
    // it also fires for `{ name: 'admin' }` - see the note in inspector.ts.
    redirect: { name: ROUTE_NAMES.adminInspectors },
    meta: {
      requiresAuth: true,
      role: 'admin',
      title: 'Administration',
    },
    children: [
      {
        path: 'inspectors',
        name: ROUTE_NAMES.adminInspectors,
        component: placeholder('AdminInspectorListView'),
        meta: { title: 'Inspectors' },
      },
      {
        path: 'inspectors/new',
        name: ROUTE_NAMES.adminInspectorNew,
        component: placeholder('AdminInspectorCreateView'),
        meta: { title: 'New inspector' },
      },
      {
        // Declared after `inspectors/new` on purpose: the static segment has to
        // be matched first, or `new` would be read as a user id.
        path: 'inspectors/:userId',
        name: ROUTE_NAMES.adminInspectorDetail,
        component: placeholder('AdminInspectorDetailView'),
        props: true,
        meta: { title: 'Inspector' },
      },
      {
        path: 'projects',
        name: ROUTE_NAMES.adminProjects,
        component: placeholder('AdminProjectListView'),
        meta: { title: 'All projects' },
      },
    ],
  },
]
