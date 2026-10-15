/**
 * Temporary stand-in for views that do not exist yet.
 *
 * Step 1 of the frontend is the data layer, the routing table and the store -
 * the screens come next. Without this, every route would import a `.vue` file
 * that is not there, and the project would not typecheck or build until the
 * last screen was written.
 *
 * Replace `placeholder('DashboardView')` with the real lazy import as each view
 * lands, and delete this file once the last one is gone.
 */

import { defineComponent, h } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import type { RouteLocationNormalizedLoaded } from 'vue-router'
import type { VNode } from 'vue'

import type { UserRole } from '@/types/api'

export interface PlaceholderOptions {
  /**
   * Render the matched child route below the metadata block.
   *
   * Set on the three *layout* routes (`projects.detail`, `profile`, `admin`):
   * without a `<RouterView />` the parent renders and the child is silently
   * swallowed - the URL and the route name are the child's, the screen shown is
   * the parent's. The real layouts render their `<RouterView />` inside the
   * shell chrome; this flag exists only because the placeholder has none.
   */
  nest?: boolean
}

/** Names the screen a route is waiting for, and shows what the guard allowed. */
export function placeholder(view: string, options: PlaceholderOptions = {}) {
  return defineComponent({
    name: view,
    setup() {
      const route = useRoute()

      return () => {
        const blocks: VNode[] = [metadataBlock(view, route)]
        if (options.nest) {
          blocks.push(
            h('hr', { class: 'mx-auto mt-8 max-w-2xl border-slate-200' }),
            h(RouterView),
          )
        }
        return h('div', blocks)
      }
    },
  })
}

function metadataBlock(view: string, route: RouteLocationNormalizedLoaded): VNode {
  return h('div', { class: 'mx-auto max-w-2xl p-8 font-sans text-slate-800' }, [
    h('p', { class: 'text-xs font-medium tracking-wide text-slate-500 uppercase' }, [
      'Placeholder — no view implemented yet',
    ]),
    h('h1', { class: 'mt-2 text-2xl font-semibold' }, [view]),
    h('dl', { class: 'mt-6 grid grid-cols-[8rem_1fr] gap-y-2 text-sm' }, [
      h('dt', { class: 'text-slate-500' }, ['route name']),
      h('dd', { class: 'font-mono' }, [String(route.name ?? '—')]),
      h('dt', { class: 'text-slate-500' }, ['path']),
      h('dd', { class: 'font-mono' }, [route.fullPath]),
      h('dt', { class: 'text-slate-500' }, ['required role']),
      h('dd', { class: 'font-mono' }, [formatRole(route.meta.role)]),
    ]),
    h('p', { class: 'mt-8 text-sm text-slate-500' }, [
      'This screen renders nothing but its route metadata, which is exactly what makes it useful while wiring the ' +
        'router: signing in as an Inspector and visiting /admin/inspectors must land on /forbidden, not here.',
    ]),
  ])
}

function formatRole(role: UserRole | UserRole[] | undefined): string {
  if (!role) return 'any authenticated user'
  return Array.isArray(role) ? role.join(', ') : role
}
