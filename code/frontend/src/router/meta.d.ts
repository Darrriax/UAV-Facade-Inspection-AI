/**
 * Typed route metadata.
 *
 * Augmenting `RouteMeta` means `to.meta.role` is checked by TypeScript instead
 * of being `unknown`, and a typo like `role: 'adminstrator'` fails the build
 * rather than silently allowing everyone through.
 */

import 'vue-router'

import type { UserRole } from '@/types/api'

declare module 'vue-router' {
  interface RouteMeta {
    /** Signed-in access required; anonymous visitors are sent to `/login`. */
    requiresAuth?: boolean
    /**
     * Role(s) allowed in. A single value for the common case, an array for the
     * read-only routes an Admin shares with an Inspector. Omitted means "any
     * authenticated user". Merged down the matched route chain, so children of
     * a guarded parent inherit the requirement.
     */
    role?: UserRole | UserRole[]
    /** Only for signed-out visitors: an authenticated user is redirected home. */
    guestOnly?: boolean
    /** Document title, suffixed with the app name by the afterEach hook. */
    title?: string
    /**
     * Opt out of the shell's centred, padded column and its footer.
     *
     * For screens that are themselves a full-height tool - today only the
     * project workspace, whose two panels each scroll on their own instead of
     * scrolling the page.
     */
    fullBleed?: boolean
  }
}

export {}
