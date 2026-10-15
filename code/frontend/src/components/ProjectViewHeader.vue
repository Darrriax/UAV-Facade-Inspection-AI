<script setup lang="ts">
/**
 * The project header both project screens wear: identity, status, the 2D/3D
 * switch, and the project's totals.
 *
 * Extracted rather than copied, because the workspace and the reconstruction
 * viewer are two halves of one project screen - the header is the part that says
 * so, and a copy drifts. It did: the 3D page carried a dark variant of this
 * markup, so moving between the two views of the same project looked like
 * moving between two products.
 *
 * The switch renders only for a project that asked for a 3D model - a 2D-only
 * project has nothing to switch to - and the haloed half is the screen the
 * caller is on (`view`), not whichever page happens to render this.
 */

import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import { ROUTE_NAMES } from '@/router/routes/names'
import type { Project } from '@/types/api'
import { projectStatusStyle } from '@/utils/projectStatus'

const props = defineProps<{
  /** `null` while the page is still loading: the header renders its skeleton. */
  project: Project | null
  /** Which half of the switch this screen is; the other half is a link. */
  view: 'defects' | 'model'
  /**
   * Totals to print, when the caller counts them from rows it has loaded. The
   * workspace does, because confirming a defect changes the number before the
   * server is asked again. Omitted, the project's own rollup is used - the same
   * three numbers, computed in SQL, for a screen that holds no rows.
   */
  counts?: { frames: number; total: number; verified: number }
}>()

const { t } = useI18n()

const projectId = computed(() => props.project?.id ?? '')

const statusStyle = computed(() =>
  props.project ? projectStatusStyle(props.project.status) : null,
)

const totals = computed(
  () =>
    props.counts ?? {
      frames: props.project?.stats.total_images ?? 0,
      total: props.project?.stats.total_defects ?? 0,
      verified: props.project?.stats.verified_defects ?? 0,
    },
)
</script>

<template>
  <header
    class="flex flex-wrap items-center gap-x-4 gap-y-3 border-b border-slate-200 bg-white px-4 py-3"
  >
    <RouterLink
      :to="{ name: ROUTE_NAMES.dashboard }"
      class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm font-medium text-slate-700 transition hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
    >
      <AppIcon name="arrowLeft" class="h-4 w-4" />
      {{ t('inspector.workspace.back') }}
    </RouterLink>

    <div class="min-w-0">
      <h1 class="truncate text-lg font-semibold tracking-tight text-slate-900">
        {{ project?.name ?? t('inspector.workspace.untitled') }}
      </h1>
      <p v-if="project?.location" class="flex items-center gap-1.5 text-xs text-slate-500">
        <AppIcon name="mapPin" class="h-3.5 w-3.5 text-slate-400" />
        <span class="truncate">{{ project.location }}</span>
      </p>
    </div>

    <span
      v-if="statusStyle"
      class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset"
      :class="statusStyle.badge"
    >
      <span class="h-1.5 w-1.5 rounded-full" :class="statusStyle.dot" />
      {{ t(statusStyle.labelKey) }}
    </span>

    <!--
      The switch between the two views of one project. It is the project flag
      that decides, not whether a mesh exists yet: a project that asked for 3D is
      a 3D project throughout the pipeline, and each page states where its own
      half stands.
    -->
    <div
      v-if="project?.needs_3d_model"
      class="flex items-center gap-1 rounded-lg bg-slate-100 p-1"
      role="group"
      :aria-label="t('inspector.workspace.view.group')"
    >
      <span
        v-if="view === 'defects'"
        class="inline-flex items-center gap-1.5 rounded-md bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-900 shadow-sm"
        aria-current="page"
      >
        <AppIcon name="image" class="h-3.5 w-3.5" />
        {{ t('inspector.workspace.view.defects2d') }}
      </span>
      <RouterLink
        v-else
        :to="{ name: ROUTE_NAMES.projectWorkspace, params: { projectId } }"
        class="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-white hover:text-slate-900"
      >
        <AppIcon name="image" class="h-3.5 w-3.5" />
        {{ t('inspector.workspace.view.defects2d') }}
      </RouterLink>

      <span
        v-if="view === 'model'"
        class="inline-flex items-center gap-1.5 rounded-md bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-900 shadow-sm"
        aria-current="page"
      >
        <AppIcon name="cube" class="h-3.5 w-3.5" />
        {{ t('inspector.workspace.view.model3d') }}
      </span>
      <RouterLink
        v-else
        :to="{ name: ROUTE_NAMES.projectModel3d, params: { projectId } }"
        class="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-white hover:text-slate-900"
      >
        <AppIcon name="cube" class="h-3.5 w-3.5" />
        {{ t('inspector.workspace.view.model3d') }}
      </RouterLink>
    </div>

    <div class="ml-auto flex items-center gap-3">
      <!--
        The header carries the project's identity and its totals, and nothing
        else: these screens are for their own job, so a second navigation link
        and a manual reload button here were two ways out of them that the
        dashboard and the processing screen already provide.
      -->
      <p class="text-xs text-slate-500">
        {{ t('inspector.counts.frames', totals.frames) }} ·
        {{ t('inspector.counts.defects', totals.total) }} ·
        {{ t('inspector.counts.confirmed', totals.verified) }}
      </p>
    </div>
  </header>
</template>
