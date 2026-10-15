# Frontend — Automated Building Facade Inspection System

Vue 3 + Vite + TypeScript SPA for the facade inspection system. The auth screens,
the Inspector dashboard and the three-step project-creation wizard are built; the
project workspace, the profile pages and the Admin area still render placeholders
that print their own route metadata.

Starting the whole stack (Docker + migrations + dev server) is documented in
[RUNBOOK.md](../RUNBOOK.md).

## Stack

| Concern     | Choice                                              |
| ----------- | --------------------------------------------------- |
| Framework   | Vue 3.5 (`<script setup>`, Composition API)         |
| Build       | Vite 8                                              |
| Language    | TypeScript 6 (`vue-tsc` for SFC type checking)      |
| Routing     | Vue Router 5, `createWebHistory` (real URLs)        |
| State       | Pinia 4 (setup stores)                              |
| Styling     | Tailwind CSS 4 via `@tailwindcss/vite` (CSS-first)  |
| i18n        | Vue I18n 11 (Composition mode), `src/i18n/locales`  |
| 3D          | Three.js + TresJS 5 (`@tresjs/core`, `@tresjs/cientos`) |
| HTTP        | `fetch`, wrapped once in `src/api/client.ts`        |

Two deliberate version choices:

- **TypeScript is pinned to `~6.0.3`, not the `7.x` latest.** `vue-tsc` 3.3
  still drives the classic TypeScript compiler API, which the native TS 7
  compiler no longer ships — the pairing fails with
  `ERR_PACKAGE_PATH_NOT_EXPORTED`. Revisit once vue-tsc documents TS 7 support.
- **No HTTP client library.** The API is JSON plus one multipart upload, both of
  which `fetch` handles. The only thing it cannot do is upload progress for the
  UAV batches; if that becomes a requirement, swap the internals of
  `src/api/client.ts` for an axios instance and keep its exported surface.

## Structure

```
src/
├── App.vue               root: the active route + the toast layer
├── api/                  one module per backend router - the only place that talks HTTP
│   ├── client.ts         base URL, bearer token, ApiError (+ fieldErrors), 401 hook
│   ├── auth.ts           POST /login, GET /users/me
│   ├── users.ts          profile updates + the admin surface (/users, /users/{id})
│   └── projects.ts       projects, images, multipart UAV upload, cover photo, status stats
├── assets/main.css       Tailwind entry + `@theme` design tokens (defect colours, status colours)
├── components/
│   ├── AppIcon.vue       stroke icon wrapper; icons.ts holds the path data
│   ├── LanguageSwitcher.vue  the EN/UK control that sits in the login footer
│   ├── ToastHost.vue     renders the toast queue (mounted once, in App.vue)
│   └── WizardStepper.vue the three-step progress bar
├── composables/
│   └── useToasts.ts      module-level toast queue any layer can push to
├── i18n/
│   ├── index.ts          instance, supported locales, locale policy, setLocale
│   └── locales/          en.json (schema source of truth) + uk.json
├── layouts/
│   └── InspectorLayout.vue  header, navigation, account block, sign-out
├── router/
│   ├── index.ts          router factory (WebHistory, scroll behaviour)
│   ├── guards.ts         RBAC guards + document title
│   ├── meta.d.ts         typed RouteMeta (requiresAuth / role / guestOnly / title)
│   ├── placeholder.ts    temporary stand-in for not-yet-written views
│   └── routes/           names.ts + one table per audience
├── stores/
│   ├── auth.ts           session: token, profile, login/logout/restore
│   └── wizard.ts         the creation flow: created project, batch outcome
├── types/api.ts          DTO mirror of app/schemas/*.py
├── utils/                storage.ts (token, locale), app.ts (constants), format.ts (dates),
│                         files.ts (accepted types, upload limits, byte formatting)
└── views/
    ├── auth/             LoginView (sign-in only - accounts are Admin-created)
    └── inspector/
        ├── DashboardView.vue
        └── wizard/
            ├── WizardLayout.vue   stepper + <RouterView />, mounted on `/projects`
            ├── Step1Info.vue      details, cover photo, 2D/3D toggle -> POST /projects
            ├── Step2Upload.vue    drag-and-drop, multipart upload -> POST /images
            └── Step3Progress.vue  polls the status counters, then links to the workspace
```

## Scripts

```bash
npm install
npm run dev         # http://localhost:5173 (proxies /api to :8000)
npm run typecheck   # vue-tsc --noEmit
npm run build       # typecheck, then production bundle into dist/
npm run preview     # serve the built bundle
```

The dev server proxies `/api` to `http://localhost:8000`, so the SPA is
same-origin with the API and needs no CORS. `VITE_API_BASE_URL` (see
`.env.example`) overrides the base path for a split deployment.

## Languages

English and Ukrainian, through Vue I18n in Composition mode. **Sign-in, the
shell, the dashboard, the whole creation wizard and the workspace are translated
so far** - the login under `auth.login.*`, the dashboard under
`inspector.dashboard.*` and the wizard under `inspector.wizard.*`. Shared
vocabulary sits in its own namespaces: `app.*`, `common.*`, `language.*` and
`status.*` (the project statuses, which `utils/projectStatus.ts` points at by
key). Every remaining screen gets a namespace of its own (`inspector.*`,
`admin.*`) as it is translated: the 3D viewer's error state, the frame
inspector's empty state, the profile page and the Admin area. `en.json` is the source of truth for the schema: the Ukrainian
catalogue has to keep the same shape, which `createI18n` checks at compile
time.

Ukrainian needs three plural forms (`3 дефекти`, `35 дефектів`), so the i18n
module registers its own selector under `pluralRules.uk`; counts therefore go
through `t(key, n)` rather than string interpolation. Dates follow the same
idea: `formatDate()` takes an Intl locale, and a translated screen passes
`dateLocaleFor(locale)` - a bare `en` would format US-style, the cards are laid
out around `14 Oct 2026` / `14 жовт. 2026 р.`.

The switcher sits in the login footer, i.e. where an unauthenticated reader
actually meets the app. The chosen locale is persisted in `localStorage` and
resolved as stored choice -> browser preference (`uk-UA` -> `uk`) -> English;
`<html lang>` follows the active locale. Known limits: a missing key at a call
site is a dev-time warning, not a type error (vue-i18n types `t()` to accept
any string); the two fixed 401 messages from the API are mapped to catalogue
keys on the login screen while other API text - 422 validation details - is
shown as the API wrote it; and the shared shell (header nav, footer, the brand
name) is still English until it gets a namespace of its own.

## Route map

| Route                                | Name                  | Meta                                            | Screen                            |
| ------------------------------------ | --------------------- | ----------------------------------------------- | --------------------------------- |
| `/login`                             | `login`               | `guestOnly`                                     | sign in                           |
| `/forbidden`                         | `forbidden`           | —                                               | 403                               |
| `/not-found`, `*`                    | `not-found`           | —                                               | 404                               |
| `/`                                  | `home`                | `requiresAuth`                                  | redirects by role                 |
| `/dashboard`                         | `dashboard`           | `requiresAuth`, `role: 'inspector'`             | project cards                     |
| `/projects`                          | —                     | `requiresAuth`, `role: 'inspector'`             | wizard layout; redirects to step 1 |
| `/projects/new`                      | `projects.new`        | (inherited)                                     | wizard step 1 — basic info        |
| `/projects/:projectId/photos`        | `projects.photos`     | (inherited)                                     | wizard step 2 — UAV upload        |
| `/projects/:projectId/progress`      | `projects.progress`   | (inherited)                                     | wizard step 3 — YOLO / 3D status  |
| `/projects/:projectId`               | `projects.detail`     | `requiresAuth`, `role: ['inspector','admin']`   | redirects to step 3               |
| `/projects/:projectId/workspace`     | `projects.workspace`  | (inherited)                                     | defects + 2D/3D model             |
| `/profile`                           | `profile`             | `requiresAuth`                                  | redirects to `profile.general`    |
| `/profile/general`                   | `profile.general`     | (inherited)                                     | name, country, age, phone, photo  |
| `/profile/security`                  | `profile.security`    | (inherited)                                     | password change                   |
| `/admin`                             | `admin`               | `requiresAuth`, `role: 'admin'`                 | redirects to inspector list       |
| `/admin/inspectors`                  | `admin.inspectors`    | (inherited)                                     | list + storage usage + on/off     |
| `/admin/inspectors/new`              | `admin.inspectors.new`| (inherited)                                     | create an inspector account       |
| `/admin/inspectors/:userId`          | `admin.inspectors.detail` | (inherited)                                 | one inspector                     |
| `/admin/projects`                    | `admin.projects`      | (inherited)                                     | every project (read-only)         |

`meta` is merged down the matched route chain, so children only carry a `title`.
Route names are constants in `src/router/routes/names.ts` — use
`router.push({ name: ROUTE_NAMES.x })`, never a literal path.

Guards (`src/router/guards.ts`) do exactly three things: hydrate a session that
outlived a reload, redirect anonymous visitors to `/login?redirect=<path>`, and
send a role mismatch to `/forbidden`. They are **not** a security boundary — the
API re-checks the role from the database on every request.

After signing in, send the user to `route.query.redirect` if it is a string,
otherwise `landingRouteFor(role)` from `src/router/routes/index.ts`.

## Session model

- The access token is persisted in `localStorage` (`src/utils/storage.ts`) and
  pushed into the HTTP client — the client never reads storage itself. The
  trade-off versus an httpOnly cookie is documented in that file; it needs a
  server-side cookie flow to change.
- `main.ts` awaits `auth.restoreSession()` (`GET /users/me`) **before mounting**,
  so the first navigation already knows the role. A 401 clears the token; a
  network failure keeps it, because the token may still be valid.
- Any authenticated request that comes back 401 triggers the handler registered
  in `main.ts`: the session is cleared and the user lands on `/login` with a
  `redirect` back to where they were. Deactivating an Inspector therefore ends
  their session on the next request, which matches the backend (it checks
  `is_active` on every request, not just at login).

## API contract notes

- Paginated collections are `{ items, total, limit, offset }` (`Page<T>`);
  `listProjects`/`listUsers` mirror that, `listAllProjects` follows the pages.
- The API returns **object keys** (`main_photo_s3_path`,
  `InspectionImage.s3_path`, `profile_photo_s3_path`), never URLs. Presigned
  URLs are requested when a screen needs bytes, because they expire.
- `POST /projects/{id}/images` takes repeated `files` parts, max 100 files and
  25 MiB each (`MAX_FILES_PER_UPLOAD`, `MAX_FILE_BYTES` are exported for the UI
  to pre-validate). `task_id: null` means stored-but-not-queued: offer a
  re-queue, never a re-upload.
- `POST /projects/{id}/main-photo` takes a single `file` part. It is a separate
  call from `POST /projects` because the object key is pinned to the project's
  own prefix (`projects/<id>/cover<ext>`) and that prefix only exists once the
  project row does. Re-uploading replaces the cover.
- `GET /projects/{id}/images/stats` returns `{ total, pending, processing,
  completed, failed }` in one query. The progress screen polls it every few
  seconds; deriving the same numbers from the listing would mean paging the whole
  collection on every tick.
- Profiling endpoints: `GET/PATCH /users/me` (self-service profile) and
  `GET /users?role=inspector` (admin listing). `PATCH /users/{id}` with
  `{ is_active: false }` deactivates an account.
- `fetch` cannot report upload progress, so the upload indicator is
  indeterminate. Real progress needs `XMLHttpRequest` (or presigned PUTs straight
  to object storage) — that is the one thing that would justify replacing
  `api/client.ts`, keeping its exported surface.

## Not yet built (needed by the screens)

1. **Project workspace** (defect list + 2D/3D model), the profile pages and the
   whole Admin area — the routes exist and render placeholders. Until the Admin
   area exists, an Inspector account is created from the API (`POST /users` as an
   Admin) or with `python -m app.cli create-user`: there is no self-service
   sign-up screen, on purpose.
2. **Backend: presigned URL endpoints** for project photos, inspection images
   and the profile photo, so the UI can render them. Until then the dashboard
   draws its own placeholder covers and the avatar is initials: the API returns
   object *keys*, which are not fetchable from the browser.
3. **Backend: per-inspector storage usage.** The Admin dashboard calls for it,
   but nothing in the schema records object sizes (and S3 keys are not
   per-user), so the number cannot honestly be shown yet. Needs a `size_bytes`
   column written at upload time plus an aggregate endpoint.
4. **Backend: defect listing** (`GET /projects/{id}/defects`) and the 3D
   mapping read for the workspace, plus `POST /users/me/photo` for the avatar.
5. **Backend: a re-queue endpoint.** `task_id: null` (stored but not queued) can
   currently only be recovered by uploading the same batch again; the frames stay
   `pending`, so a `POST /projects/{id}/images/requeue` would be a small addition.
