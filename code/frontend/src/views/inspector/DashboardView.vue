<script setup lang="ts">
/**
 * Inspector dashboard: the project grid and the entry point into the wizard.
 *
 * Data comes from `GET /projects` (newest first). The counters on each card -
 * photos, defects and the status badge - are rolled up server-side into the
 * same response (`Project.stats` / `Project.status`), so rendering the grid
 * costs exactly one request however many projects it shows. Nothing here is
 * derived by asking the API per card.
 *
 * Cover photos are the one exception, and for a different reason: the API
 * stores an object *key*, never a URL, because presigned URLs expire. Each card
 * that has a cover asks for a fresh signed URL, and a card whose project has no
 * cover yet (a 404, or no `main_photo_s3_path`) renders a generated placeholder
 * facade instead - so the grid is complete even before the first upload. The
 * project's name is drawn on top of either of them, once and in one place,
 * which is what keeps a card with a real photo from being the unnamed one.
 *
 * Every string on the screen comes from the catalogues, and the Ukrainian
 * catalogue is why the counts go through `t()` with a number rather than string
 * interpolation: `3 дефекти` and `34 дефектів` are different words, and only
 * the catalogue knows which form a given count takes.
 *
 * ## A card opens the screen the project is actually at
 *
 * `destinationFor()` reads the same `status` the badge is drawn from. A project
 * mid-analysis - or one the user stopped - opens the *progress* screen, not the
 * workspace: the workspace is about results that do not exist yet, and it used to
 * make the live counters, the only place that says how far the batch has got,
 * unreachable except by hand-editing the URL. A stopped project in particular
 * opens there because that is where "Resume" lives. A draft opens the upload
 * step, which is the next thing that project needs; only a finished project
 * opens the workspace. The workspace keeps a link back to the progress screen,
 * so nothing becomes one-way.
 *
 * ## Editing is a dialog over the grid, like deleting
 *
 * The Edit button sits next to Delete on the card, as a sibling of the link for
 * the same reason: it opens `ProjectEditDialog`, which PATCHes the text fields
 * and then uploads a new cover. The card is replaced with the project the
 * server echoes back, so the grid never shows a name the API has not accepted.
 *
 * ## Deleting is a two-step action on the card itself
 *
 * The button is a *sibling* of the card's link, never a child of it: a
 * `<button>` inside an `<a>` is invalid HTML and would navigate as well as
 * delete. It opens a confirmation dialog rather than deleting on the spot,
 * because the API call takes the frames, the defects and the 3D mapping with it
 * and there is no undo.
 */

import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, useRouter } from 'vue-router'
import type { RouteLocationRaw } from 'vue-router'

import { describeApiError } from '@/api/client'
import { deleteProject, getProjectMainPhotoUrl, listProjects } from '@/api/projects'
import AppIcon from '@/components/AppIcon.vue'
import ProjectEditDialog from '@/components/ProjectEditDialog.vue'
import { useToasts } from '@/composables/useToasts'
import { dateLocaleFor } from '@/i18n'
import { ROUTE_NAMES } from '@/router/routes/names'
import { useAuthStore } from '@/stores/auth'
import { PROJECT_STATUSES } from '@/types/api'
import type { Project } from '@/types/api'
import { formatDate } from '@/utils/format'
import { projectStatusStyle } from '@/utils/projectStatus'

/**
 * One page of projects.
 *
 * Bounded on purpose rather than following the pagination to the end: an Admin
 * sees every project on the installation, and a dashboard that silently issues
 * fifty requests is worse than a grid that says what it is showing.
 */
const PAGE_SIZE = 100

const auth = useAuthStore()
const router = useRouter()
const toast = useToasts()
const { t, locale } = useI18n()

const projects = ref<Project[]>([])
const total = ref(0)
const loading = ref(true)
const error = ref<string | null>(null)
/** Project id -> signed cover URL, for the cards that actually have a cover. */
const covers = ref<Record<string, string>>({})
/** The project an edit dialog is open for, if any. */
const editingProject = ref<Project | null>(null)
/** The project a confirmation dialog is open for, if any. */
const pendingDelete = ref<Project | null>(null)
/** Project id whose DELETE request is in flight. */
const deletingId = ref<string | null>(null)
const cancelButton = ref<HTMLButtonElement | null>(null)

const stats = computed(() => ({
  total: total.value,
  needs3d: projects.value.filter((project) => project.needs_3d_model).length,
  // Everything the worker has not finished with, which is what the header
  // summarises: frames queued or in flight.
  inProgress: projects.value.filter(
    (project) => project.status === PROJECT_STATUSES.PROCESSING || project.status === PROJECT_STATUSES.UPLOADING,
  ).length,
}))

const hasMore = computed(() => projects.value.length < total.value)

onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const page = await listProjects({ limit: PAGE_SIZE })
    projects.value = page.items
    total.value = page.total
    // Not awaited: the cards render immediately and fill in their covers when
    // the signed URLs arrive.
    void loadCovers(page.items)
  } catch (cause) {
    error.value = describeApiError(cause)
  } finally {
    loading.value = false
  }
}

/**
 * Fetch the signed cover URL for every project that has one.
 *
 * `allSettled`, not `all`: one unreachable object must not blank out the covers
 * of the other projects. A project that fails here (or has no cover) simply
 * keeps its generated placeholder.
 */
async function loadCovers(items: Project[]): Promise<void> {
  const withCover = items.filter((project) => project.main_photo_s3_path)
  const results = await Promise.allSettled(
    withCover.map((project) => getProjectMainPhotoUrl(project.id)),
  )

  const resolved: Record<string, string> = {}
  for (const [index, result] of results.entries()) {
    const project = withCover[index]
    if (project && result.status === 'fulfilled') resolved[project.id] = result.value.url
  }
  covers.value = resolved
}

/** Signed cover when there is one, generated facade otherwise. */
function coverFor(project: Project): string {
  return covers.value[project.id] ?? placeholderPhoto(project.name)
}

function createProject(): void {
  // Named route, not a literal: the wizard's step 1 lives at `/projects/new`
  // (see `router/routes/inspector.ts`), which is what the URL says today.
  void router.push({ name: ROUTE_NAMES.projectNew })
}

// --------------------------------------------------------------- editing
function askEdit(project: Project): void {
  editingProject.value = project
}

/**
 * Adopt what the API saved: replace the card's copy and re-sign its cover.
 *
 * The cover is fetched again whenever the row still points at one, because the
 * object key is derived from the file extension and a new photo therefore has
 * a new key - the URL cached for the old one would keep showing the previous
 * cover, or 404 after it is deleted server-side.
 */
async function applySavedProject(saved: Project): Promise<void> {
  projects.value = projects.value.map((item) => (item.id === saved.id ? saved : item))
  // Keep the dialog's input in step with the server too: a cover upload that
  // failed leaves it open, and it must compare against the saved row rather
  // than the one it was opened with.
  if (editingProject.value?.id === saved.id) editingProject.value = saved

  if (saved.main_photo_s3_path === null) {
    const remaining = { ...covers.value }
    delete remaining[saved.id]
    covers.value = remaining
    return
  }

  try {
    covers.value = { ...covers.value, [saved.id]: (await getProjectMainPhotoUrl(saved.id)).url }
  } catch {
    // The card falls back to its generated placeholder; nothing else is lost.
  }
}

/**
 * Where a card leads, given the state the project is actually in.
 *
 * Deliberately answers with the *progress* route while a batch is in flight -
 * see the module docstring - and with the workspace only once there is something
 * to review. `failed` counts as "review it": the counter breakdown and the
 * failure list on the progress screen stay one click away from the workspace.
 */
/** Statuses whose next useful screen is the progress one. */
const LIVE_STATUSES: readonly Project["status"][] = [
  PROJECT_STATUSES.UPLOADING,
  PROJECT_STATUSES.PROCESSING,
  PROJECT_STATUSES.PAUSED,
]

function destinationFor(project: Project): RouteLocationRaw {
  const params = { projectId: project.id }

  if (project.status === PROJECT_STATUSES.DRAFT) {
    return { name: ROUTE_NAMES.projectPhotos, params }
  }
  if (LIVE_STATUSES.includes(project.status)) {
    return { name: ROUTE_NAMES.projectProgress, params }
  }
  return { name: ROUTE_NAMES.projectWorkspace, params }
}

/** One-line description of what the card will open, for an accessible label. */
function destinationLabel(project: Project): string {
  if (project.status === PROJECT_STATUSES.DRAFT) {
    return t('inspector.dashboard.card.destination.upload')
  }
  if (project.status === PROJECT_STATUSES.PAUSED) {
    return t('inspector.dashboard.card.destination.resume')
  }
  if (LIVE_STATUSES.includes(project.status)) {
    return t('inspector.dashboard.card.destination.progress')
  }
  return t('inspector.dashboard.card.destination.workspace')
}

// --------------------------------------------------------------- deletion
function askDelete(project: Project): void {
  pendingDelete.value = project
}

function cancelDelete(): void {
  if (deletingId.value !== null) return
  pendingDelete.value = null
}

/**
 * Put focus on Cancel when the dialog opens.
 *
 * On the *cancel* button, not on Delete: the dialog opens from a click on a
 * card, and a stray Enter or Space must not confirm a destructive action.
 */
watch(pendingDelete, (project) => {
  if (project !== null) void nextTick(() => cancelButton.value?.focus())
})

async function confirmDelete(): Promise<void> {
  const project = pendingDelete.value
  if (project === null || deletingId.value !== null) return

  deletingId.value = project.id
  try {
    await deleteProject(project.id)
    projects.value = projects.value.filter((item) => item.id !== project.id)
    // The header counts and the pagination note read both of these, so they have
    // to move with the grid - a card that is gone while the total still counts
    // it is the kind of thing nobody notices until a "showing 3 of 4" line.
    total.value = Math.max(0, total.value - 1)
    const remainingCovers = { ...covers.value }
    delete remainingCovers[project.id]
    covers.value = remainingCovers
    pendingDelete.value = null
    toast.success(
      t('inspector.dashboard.toast.deleted'),
      t('inspector.dashboard.toast.deletedBody', { name: project.name }),
    )
  } catch (cause) {
    // The card stays exactly where it was: nothing was deleted, and a dialog
    // that closes on failure would leave the user believing it was.
    toast.error(t('inspector.dashboard.toast.deleteFailed'), describeApiError(cause))
  } finally {
    deletingId.value = null
  }
}

/**
 * Stand-in for a project that has no cover photo yet.
 *
 * Drawn as an inline SVG data URI: no network request, no stock photo
 * pretending to be somebody's building, and the same project always renders the
 * same picture. The project's name is *not* written into it - the card draws the
 * name over whatever the cover is, so a generated cover and an uploaded photo
 * label themselves the same way - it only decides the hue, so two projects never
 * look alike by accident.
 */
function placeholderPhoto(label: string): string {
  const panes: string[] = []
  for (let row = 0; row < 3; row += 1) {
    for (let column = 0; column < 6; column += 1) {
      const lit = (row + column) % 5 === 0
      panes.push(
        `<rect x='${40 + column * 95}' y='${58 + row * 72}' width='64' height='46' rx='3' fill='rgba(255,255,255,${lit ? 0.26 : 0.1})'/>`,
      )
    }
  }

  // Hue from the name, so two projects never look alike by accident.
  let hash = 0
  for (const character of label) hash = (hash * 31 + character.charCodeAt(0)) % 360

  const svg = [
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 360'>",
    "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>",
    `<stop offset='0' stop-color='hsl(${hash} 38% 34%)'/>`,
    `<stop offset='1' stop-color='hsl(${(hash + 18) % 360} 34% 16%)'/>`,
    '</linearGradient></defs>',
    "<rect width='640' height='360' fill='url(#g)'/>",
    panes.join(''),
    '</svg>',
  ].join('')

  return `data:image/svg+xml,${encodeURIComponent(svg)}`
}
</script>

<template>
  <div class="space-y-8">
    <!-- Page header -->
    <div class="flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 class="text-2xl font-semibold tracking-tight text-slate-900">
          {{ t('inspector.dashboard.welcome') }}{{ auth.user?.first_name ? `, ${auth.user.first_name}` : '' }}
        </h1>
        <p class="mt-1.5 text-sm text-slate-500">
          {{ t('inspector.dashboard.stats.projects', stats.total) }} ·
          {{ t('inspector.dashboard.stats.with3d', { n: stats.needs3d }) }} ·
          {{ t('inspector.dashboard.stats.inProgress', { n: stats.inProgress }) }}
        </p>
      </div>

      <button
        type="button"
        class="inline-flex shrink-0 items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
        @click="createProject"
      >
        <AppIcon name="plus" class="h-4 w-4" />
        {{ t('inspector.dashboard.createProject') }}
      </button>
    </div>

    <!-- Load failure -->
    <div
      v-if="error"
      class="flex flex-col gap-3 rounded-2xl border border-red-200 bg-red-50 px-5 py-4 sm:flex-row sm:items-center sm:justify-between"
      role="alert"
    >
      <p class="text-sm text-red-800">
        <span class="font-semibold">{{ t('inspector.dashboard.loadError') }}</span> {{ error }}
      </p>
      <button
        type="button"
        class="shrink-0 rounded-lg border border-red-300 bg-white px-3.5 py-2 text-sm font-medium text-red-700 transition hover:bg-red-100"
        @click="load"
      >
        {{ t('common.tryAgain') }}
      </button>
    </div>

    <!-- Loading skeleton: same grid metrics, so the layout does not jump. -->
    <div v-else-if="loading" class="grid gap-6 sm:grid-cols-2 xl:grid-cols-3" aria-busy="true">
      <div
        v-for="index in 3"
        :key="index"
        class="animate-pulse overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm"
      >
        <div class="aspect-video bg-slate-200" />
        <div class="space-y-3 p-5">
          <div class="h-4 w-2/3 rounded bg-slate-200" />
          <div class="h-3 w-1/2 rounded bg-slate-100" />
          <div class="h-3 w-full rounded bg-slate-100" />
        </div>
      </div>
    </div>

    <!-- Project grid -->
    <div v-else-if="projects.length > 0" class="space-y-4">
      <div class="grid gap-6 sm:grid-cols-2 xl:grid-cols-3">
        <div
          v-for="project in projects"
          :key="project.id"
          class="group relative flex flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm transition hover:-translate-y-0.5 hover:border-blue-300 hover:shadow-md"
        >
          <!--
            The link covers the whole card. It is a *sibling* of the actions
            below, not their parent: a <button> inside an <a> is invalid HTML
            and clicking Delete would open the project as well.
          -->
          <RouterLink
            :to="destinationFor(project)"
            :aria-label="`${project.name} — ${destinationLabel(project)}`"
            class="flex flex-1 flex-col rounded-2xl focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-600"
          >
            <div class="relative aspect-video overflow-hidden bg-slate-200">
              <img
                :src="coverFor(project)"
                :alt="t('inspector.dashboard.card.coverAlt', { name: project.name })"
                loading="lazy"
                class="h-full w-full object-cover transition duration-300 group-hover:scale-[1.03]"
              />
              <span
                class="absolute top-3 left-3 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset"
                :class="projectStatusStyle(project.status).badge"
              >
                <span
                  class="h-1.5 w-1.5 rounded-full"
                  :class="projectStatusStyle(project.status).dot"
                />
                {{ t(projectStatusStyle(project.status).labelKey) }}
              </span>

              <!--
                The name is written *over* the cover, not under it: the photo is
                what a reviewer recognises a project by, and a card whose name
                sits below the fold of its own picture reads as an unnamed photo
                with some metadata under it. The gradient is what keeps the name
                legible on a bright facade.
              -->
              <p
                class="absolute inset-x-0 bottom-0 bg-gradient-to-t from-slate-950/85 via-slate-950/45 to-transparent px-4 pt-10 pb-3 text-sm font-semibold text-white"
              >
                <span class="line-clamp-1">{{ project.name }}</span>
              </p>
            </div>

            <div class="flex flex-1 flex-col gap-3 p-5">
              <!--
                The name is not repeated here: it is written over the cover
                above, and the img `alt` and the link's accessible name carry it
                for anything that cannot see the picture.
              -->
              <p
                v-if="project.location"
                class="flex items-center gap-1.5 text-sm text-slate-500"
              >
                <AppIcon name="mapPin" class="h-4 w-4 shrink-0 text-slate-400" />
                <span class="truncate">{{ project.location }}</span>
              </p>

              <p v-if="project.description" class="line-clamp-2 text-sm text-slate-600">
                {{ project.description }}
              </p>

              <div
                class="mt-auto flex items-center gap-4 border-t border-slate-100 pt-3 text-xs text-slate-500"
              >
                <span class="inline-flex items-center gap-1.5">
                  <AppIcon name="image" class="h-4 w-4 text-slate-400" />
                  {{ t('inspector.counts.photos', project.stats.total_images) }}
                </span>
                <span class="inline-flex items-center gap-1.5">
                  <AppIcon name="alert" class="h-4 w-4 text-slate-400" />
                  {{ t('inspector.counts.defects', project.stats.total_defects) }}
                </span>
                <span
                  v-if="project.stats.failed_images > 0"
                  class="inline-flex items-center gap-1.5 text-red-600"
                  :title="t('inspector.dashboard.card.failedTitle', project.stats.failed_images)"
                >
                  {{ t('inspector.dashboard.card.failed', { n: project.stats.failed_images }) }}
                </span>
                <span class="ml-auto">{{ formatDate(project.created_at, dateLocaleFor(locale)) }}</span>
              </div>
            </div>
          </RouterLink>

          <!-- Actions over the cover photo: the 3D marker, Edit and Delete. -->
          <div class="absolute top-3 right-3 flex items-center gap-2">
            <span
              v-if="project.needs_3d_model"
              class="inline-flex items-center gap-1.5 rounded-full bg-slate-900/70 px-2.5 py-1 text-xs font-medium text-white backdrop-blur-sm"
            >
              <AppIcon name="cube" class="h-3.5 w-3.5" />
              3D
            </span>
            <button
              type="button"
              :disabled="deletingId === project.id"
              :aria-label="t('inspector.dashboard.card.edit', { name: project.name })"
              :title="t('inspector.dashboard.card.editTitle', { name: project.name })"
              class="inline-flex h-7 w-7 items-center justify-center rounded-full bg-slate-900/60 text-white backdrop-blur-sm transition hover:bg-blue-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-60"
              @click="askEdit(project)"
            >
              <AppIcon name="edit" class="h-4 w-4" />
            </button>
            <button
              type="button"
              :disabled="deletingId === project.id"
              :aria-label="t('inspector.dashboard.card.delete', { name: project.name })"
              :title="t('inspector.dashboard.card.deleteTitle', { name: project.name })"
              class="inline-flex h-7 w-7 items-center justify-center rounded-full bg-slate-900/60 text-white backdrop-blur-sm transition hover:bg-red-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 disabled:cursor-not-allowed disabled:opacity-60"
              @click="askDelete(project)"
            >
              <AppIcon name="trash" class="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>

      <p v-if="hasMore" class="text-center text-xs text-slate-500">
        {{ t('inspector.dashboard.more', { shown: projects.length, total }) }}
      </p>
    </div>

    <!-- Empty state -->
    <div
      v-else
      class="flex flex-col items-center gap-4 rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-14 text-center"
    >
      <span class="inline-flex h-12 w-12 items-center justify-center rounded-full bg-slate-100">
        <AppIcon name="cube" class="h-6 w-6 text-slate-400" />
      </span>
      <div>
        <h2 class="text-base font-semibold text-slate-900">{{ t('inspector.dashboard.empty.title') }}</h2>
        <p class="mt-1 text-sm text-slate-500">
          {{ t('inspector.dashboard.empty.body') }}
        </p>
      </div>
      <button
        type="button"
        class="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700"
        @click="createProject"
      >
        <AppIcon name="plus" class="h-4 w-4" />
        {{ t('inspector.dashboard.createProject') }}
      </button>
    </div>

    <!--
      Edit dialog.

      Keyed by project id, so swapping which card is being edited mounts a
      fresh dialog instead of leaving the previous project's fields on screen.
    -->
    <ProjectEditDialog
      v-if="editingProject !== null"
      :key="editingProject.id"
      :project="editingProject"
      @close="editingProject = null"
      @saved="applySavedProject"
    />

    <!--
      Delete confirmation.

      A dialog rather than `window.confirm()`: the project's name, its counters
      and the fact that nothing can be recovered are all things the user has to
      be able to read before deciding, and a native confirm cannot be styled,
      cannot show the amber "still processing" note, and blocks the whole tab.
      Escape and a click on the backdrop both mean "cancel".
    -->
    <div
      v-if="pendingDelete"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
      @click.self="cancelDelete"
      @keydown.esc="cancelDelete"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-project-title"
        aria-describedby="delete-project-body"
        class="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 shadow-xl"
      >
        <div class="flex items-start gap-3">
          <span
            class="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-red-100"
          >
            <AppIcon name="trash" class="h-5 w-5 text-red-600" />
          </span>
          <div class="min-w-0">
            <h2 id="delete-project-title" class="text-base font-semibold text-slate-900">
              {{ t('inspector.dashboard.delete.title', { name: pendingDelete.name }) }}
            </h2>
            <p id="delete-project-body" class="mt-1.5 text-sm text-slate-600">
              {{
                t('inspector.dashboard.delete.body', {
                  photos: t('inspector.counts.photos', pendingDelete.stats.total_images),
                  defects: t('inspector.counts.defects', pendingDelete.stats.total_defects),
                })
              }}
              <span class="font-medium text-slate-800">{{ t('inspector.dashboard.delete.noUndo') }}</span>
            </p>
            <p
              v-if="
                pendingDelete.status === PROJECT_STATUSES.PROCESSING ||
                pendingDelete.status === PROJECT_STATUSES.UPLOADING
              "
              class="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
            >
              {{ t('inspector.dashboard.delete.stillRunning') }}
            </p>
          </div>
        </div>

        <div class="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            ref="cancelButton"
            type="button"
            class="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100 disabled:opacity-50"
            :disabled="deletingId !== null"
            @click="cancelDelete"
          >
            {{ t('common.cancel') }}
          </button>
          <button
            type="button"
            class="inline-flex items-center justify-center gap-2 rounded-lg bg-red-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-60"
            :disabled="deletingId !== null"
            @click="confirmDelete"
          >
            <span
              v-if="deletingId !== null"
              class="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
              aria-hidden="true"
            />
            <AppIcon v-else name="trash" class="h-4 w-4" />
            {{ deletingId !== null ? t('inspector.dashboard.delete.deleting') : t('inspector.dashboard.delete.confirm') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
