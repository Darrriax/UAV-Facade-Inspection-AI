<script setup lang="ts">
/**
 * Wizard chrome: the stepper, the flow's title, and the active step below it.
 *
 * A *layout* route, mounted once on `/projects`, with the three steps as its
 * children - so it stays mounted while the user moves through them and the
 * stepper never flickers. It serves both path shapes on purpose: `/projects/new`
 * (which creates the project) and `/projects/<id>/{photos,progress}` (which
 * operate on it). Vue Router needs one record per path shape, and duplicating
 * this chrome per branch would mean two copies of the same progress bar.
 *
 * The active step is read from the route *name*, not from the URL: the URL also
 * carries a project id, and matching on strings is how a step indicator ends up
 * disagreeing with the router.
 *
 * The header carries the title only: the walkthrough sentence and the "All
 * projects" escape hatch were dropped, because the sidebar already links back
 * to the dashboard and the stepper itself says what the three steps are.
 */

import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterView, useRoute, type RouteLocationRaw } from 'vue-router'

import WizardStepper from '@/components/WizardStepper.vue'
import { ROUTE_NAMES } from '@/router/routes/names'
import { useWizardStore } from '@/stores/wizard'

const { t } = useI18n()

/**
 * The three steps, in order, as catalogue keys rather than copy.
 *
 * Translated here and not inside the stepper: that component is presentational
 * on purpose, so it keeps rendering whatever strings it is handed - which is
 * also why the list's accessible name is passed down instead of looked up.
 * Spelled out rather than imported from the stepper because a `<script setup>`
 * block cannot export a type, and the shape is two strings.
 */
const STEP_KEYS: readonly { label: string; hint: string }[] = [
  { label: 'inspector.wizard.steps.details.label', hint: 'inspector.wizard.steps.details.hint' },
  { label: 'inspector.wizard.steps.upload.label', hint: 'inspector.wizard.steps.upload.hint' },
  { label: 'inspector.wizard.steps.processing.label', hint: 'inspector.wizard.steps.processing.hint' },
]

const steps = computed(() => STEP_KEYS.map((step) => ({ label: t(step.label), hint: t(step.hint) })))

/** Route names of the three steps, in order - indices match `STEPS`. */
const STEP_ROUTES = [ROUTE_NAMES.projectNew, ROUTE_NAMES.projectPhotos, ROUTE_NAMES.projectProgress] as const

const route = useRoute()
const wizard = useWizardStore()

const activeIndex = computed(() => {
  const index = STEP_ROUTES.indexOf(route.name as (typeof STEP_ROUTES)[number])
  return index === -1 ? 0 : index
})

/**
 * The project this flow is about: the one in the URL, or the one step 1 just
 * created. Steps 2 and 3 always have it in the URL; step 1 does not yet.
 */
const projectId = computed<string | null>(() => {
  const raw = route.params.projectId
  const fromRoute = Array.isArray(raw) ? raw[0] : raw
  return typeof fromRoute === 'string' && fromRoute.length > 0 ? fromRoute : wizard.projectId
})

/**
 * Steps 2 and 3 are only addressable once a project exists, so they are inert
 * until step 1 has run (in this session, or in an earlier one - the id is in the
 * URL after a refresh, which is enough).
 */
function linkFor(index: number): RouteLocationRaw | null {
  if (index === 0) return { name: ROUTE_NAMES.projectNew }
  const id = projectId.value
  if (id === null) return null
  return index === 1
    ? { name: ROUTE_NAMES.projectPhotos, params: { projectId: id } }
    : { name: ROUTE_NAMES.projectProgress, params: { projectId: id } }
}
</script>

<template>
  <div class="space-y-7">
    <header class="flex flex-wrap items-start justify-between gap-4">
      <div class="min-w-0">
        <p class="text-xs font-medium tracking-wide text-slate-500 uppercase">
          {{ t('inspector.wizard.eyebrow') }}
        </p>
        <h1 class="mt-1 truncate text-2xl font-semibold tracking-tight text-slate-900">
          {{ wizard.projectName ?? t('inspector.wizard.untitled') }}
        </h1>
      </div>
    </header>

    <WizardStepper
      :steps="steps"
      :active-index="activeIndex"
      :link-for="linkFor"
      :ariaLabel="t('inspector.wizard.steps.aria')"
    />

    <RouterView />
  </div>
</template>
