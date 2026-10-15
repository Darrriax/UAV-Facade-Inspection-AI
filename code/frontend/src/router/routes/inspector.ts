/**
 * Inspector routes: the signed-in shell, the dashboard, the creation wizard, the
 * project workspace and the profile pages.
 *
 * Everything the Inspector works inside is a child of the `InspectorLayout`
 * shell, so the header, navigation and account block are declared once. The shell
 * admits Admins too (they may read any project), while the work screens stay
 * Inspector-only - child meta overrides the parent's.
 *
 * ## The wizard is one layout with three children
 *
 * `/projects` is a single layout route: `WizardLayout` renders the stepper once
 * and the active step below it, so the progress bar does not remount as the user
 * moves through the flow. The steps are three *routes* rather than three steps
 * inside one component, because:
 *
 * - the back button behaves, and a half-finished project can be resumed from a
 *   link or a refresh (steps 2 and 3 carry their project id in the URL);
 * - step 1 has to hit `POST /projects` anyway - the id it returns is what steps
 *   2 and 3 are addressed by, and a wizard that only exists in memory loses it
 *   on the first reload.
 *
 * Step 1 lives at `/projects/new` rather than `/projects/<id>/details`, because
 * it runs *before* there is an id - and because it is the creation entry point
 * linked from the dashboard. That is also why the parent redirects `/projects`
 * itself to it.
 *
 * ## The workspace is deliberately outside the wizard
 *
 * It is a full-screen tool (the frame reviewer beside its annotation panel),
 * not a step: no stepper, no "back to the flow". Wrapping it in `WizardLayout`
 * would put a progress bar above a screen that is about the finished result. It
 * reads `role: [...]`, matching the API, where an Admin may GET any project but
 * may not mutate one - which is also why the annotation tools and the review
 * toggle are hidden for an Admin.
 *
 * Both workspace routes carry `meta.fullBleed`, so the layout drops its centred
 * column and footer: the photo and its panel need the whole viewport, each
 * scrolling on its own instead of scrolling the page.
 *
 * ## 2D and 3D are two routes, not two panels
 *
 * The reviewer needs the whole viewport for the photo, so the 3D model is its
 * own page and the header switch between them is a pair of links. Which frame
 * is open, and which mask is focused, stay query parameters on the workspace
 * (`?frame=`, `?defect=`) - view state that has to survive a refresh and be
 * linkable to a colleague, without pretending to be a separate screen.
 *
 * The 3D page is also where a reconstruction is *run* - in the browser, over the
 * project's own frames - so there is one place to look for a building and one
 * place to build it (see `Model3DView`). There is deliberately no second, catch-
 * all photogrammetry screen: a model belongs to a project, and a page without
 * one would have no frames to work from.
 */

import type { RouteRecordRaw } from 'vue-router'

import { placeholder } from '../placeholder'
import { ROUTE_NAMES } from './names'

export const inspectorRoutes: RouteRecordRaw[] = [
  {
    /**
     * The signed-in shell.
     *
     * Registered after the `/` landing route, which is why `/` itself still
     * resolves to the role-based redirect in `routes/index.ts` while its
     * children (`/dashboard`, `/projects/...`) render inside this layout.
     */
    path: '/',
    component: () => import('@/layouts/InspectorLayout.vue'),
    meta: {
      requiresAuth: true,
      role: ['inspector', 'admin'],
    },
    children: [
      {
        path: 'dashboard',
        name: ROUTE_NAMES.dashboard,
        component: () => import('@/views/inspector/DashboardView.vue'),
        meta: {
          role: 'inspector',
          title: 'My projects',
        },
      },

      {
        /**
         * The creation wizard: one layout, three step routes.
         *
         * `role: 'inspector'` and not `[...]`: the API refuses project creation
         * for an Admin (`ProjectService.create`), so offering the wizard to one
         * would only produce a 403 at the end of the form.
         */
        path: 'projects',
        component: () => import('@/views/inspector/wizard/WizardLayout.vue'),
        redirect: { name: ROUTE_NAMES.projectNew },
        meta: {
          role: 'inspector',
          title: 'New project',
        },
        children: [
          {
            // Step 1: details. Creates the project, then hands its id to the
            // next two steps.
            path: 'new',
            name: ROUTE_NAMES.projectNew,
            component: () => import('@/views/inspector/wizard/Step1Info.vue'),
            meta: { title: 'New project' },
          },
          {
            // Step 2: the UAV frames.
            path: ':projectId/photos',
            name: ROUTE_NAMES.projectPhotos,
            component: () => import('@/views/inspector/wizard/Step2Upload.vue'),
            props: true,
            meta: { title: 'Upload photos' },
          },
          {
            // Step 3: YOLO + 3D progress.
            path: ':projectId/progress',
            name: ROUTE_NAMES.projectProgress,
            component: () => import('@/views/inspector/wizard/Step3Progress.vue'),
            props: true,
            meta: { title: 'Processing status' },
          },
        ],
      },

      {
        /**
         * `/projects/<id>` with no step: a hand-typed or bookmarked URL.
         *
         * A redirect-only record, so it renders nothing itself. The progress
         * screen is the useful destination - it states where processing stands
         * and links on to the workspace.
         */
        path: 'projects/:projectId',
        name: ROUTE_NAMES.projectDetail,
        redirect: (to) => ({
          name: ROUTE_NAMES.projectProgress,
          params: { projectId: to.params.projectId },
        }),
      },

      {
        /**
         * The workspace: the frame on screen in the middle, the tools that edit
         * its masks beside it. The focused defect and the frame on screen are
         * view state (query parameters), not routes - both render the same
         * data, and a link to one specific crack has to survive a refresh.
         *
         * No `props: true`: the view reads `route.params` itself, and passing
         * the id as a prop would leak it onto the rendered element as an
         * attribute.
         */
        path: 'projects/:projectId/workspace',
        name: ROUTE_NAMES.projectWorkspace,
        component: () => import('@/views/inspector/WorkspaceView.vue'),
        meta: {
          role: ['inspector', 'admin'],
          title: 'Workspace',
          fullBleed: true,
        },
      },

      {
        /**
         * The reconstruction page.
         *
         * A route of its own rather than a mode of the workspace, because the
         * switch in the header promises a different screen. Reached only from
         * that switch (a project whose `needs_3d_model` is false never shows
         * it), but it stands alone: the URL can be bookmarked and shared.
         */
        path: 'projects/:projectId/model3d',
        name: ROUTE_NAMES.projectModel3d,
        component: () => import('@/views/inspector/Model3DView.vue'),
        meta: {
          role: ['inspector', 'admin'],
          title: '3D model',
          fullBleed: true,
        },
      },
    ],
  },

  {
    /**
     * Profile.
     *
     * Deliberately outside the shell for now, so an Admin (who has a profile
     * but not the Inspector navigation) is not shown the Inspector chrome.
     * Fold it into a shared shell when the Admin layout lands.
     */
    path: '/profile',
    name: ROUTE_NAMES.profile,
    component: placeholder('ProfileLayoutView', { nest: true }),
    // On the parent, so `{ name: 'profile' }` opens the general tab instead of
    // an empty layout - see the note on `projects.detail`.
    redirect: { name: ROUTE_NAMES.profileGeneral },
    meta: {
      // No `role`: an Admin has a profile too. `requiresAuth` alone is enough.
      requiresAuth: true,
      title: 'Profile',
    },
    children: [
      {
        path: 'general',
        name: ROUTE_NAMES.profileGeneral,
        component: placeholder('ProfileGeneralView'),
        meta: { title: 'Profile' },
      },
      {
        path: 'security',
        name: ROUTE_NAMES.profileSecurity,
        component: placeholder('ProfileSecurityView'),
        meta: { title: 'Password & security' },
      },
    ],
  },
]
