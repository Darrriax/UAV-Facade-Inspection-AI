<script setup lang="ts">
/**
 * Wizard step 1: the project's own details.
 *
 * This is the step that *creates* the project (`POST /projects`), because
 * everything after it needs a project id. The cover photo is a second request
 * (`POST /projects/{id}/main-photo`): the object key is pinned to the project's
 * own prefix, so the row has to exist first.
 *
 * That two-request shape has one failure mode worth naming: the project is
 * created and the photo is not. The user is kept here with an explicit choice
 * - retry the upload, or continue without it - instead of being bounced back to
 * a form whose resubmission would create a second project.
 *
 * The field vocabulary (labels, placeholders, the file checks) comes from
 * `inspector.projectForm.*`, which is the same block the dashboard's edit dialog
 * reads, so the two forms cannot drift apart in wording.
 */

import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import { ApiError, describeApiError } from '@/api/client'
import { MAX_FILE_BYTES, createProject, uploadProjectMainPhoto } from '@/api/projects'
import { ROUTE_NAMES } from '@/router/routes/names'
import { useToasts } from '@/composables/useToasts'
import { useWizardStore, type ProjectDraft } from '@/stores/wizard'
import { formatBytes, isAcceptedImage } from '@/utils/files'

const router = useRouter()
const wizard = useWizardStore()
const toast = useToasts()
const { t } = useI18n()

const form = reactive<ProjectDraft>({
  name: '',
  location: '',
  description: '',
  /** 2D-only by default: 3D reconstruction adds a photogrammetry pass. */
  needs3dModel: false,
})

/**
 * Show what the user already decided.
 *
 * The component owns the fields but not the *step*: walking to step 2 unmounts
 * it, and coming back used to hand the user a blank form for the project they
 * had just named. The draft - every keystroke, via `saveDraft` - wins over the
 * saved row, because an edit nobody has submitted yet is still theirs.
 */
function seedForm(): void {
  const { draft, project } = wizard
  form.name = draft?.name ?? project?.name ?? ''
  form.location = draft?.location ?? project?.location ?? ''
  form.description = draft?.description ?? project?.description ?? ''
  form.needs3dModel = draft?.needs3dModel ?? project?.needs_3d_model ?? false
}
seedForm()

// Live: the next visit to this step reads the fields from the store, not from
// the (by then gone) component.
watch(form, () => wizard.saveDraft({ ...form }))

const coverFile = ref<File | null>(null)
const coverPreview = ref<string | null>(null)
const fieldErrors = ref<Record<string, string>>({})
const submitting = ref(false)
/** Set when the project exists but its cover photo does not. */
const coverFailure = ref<string | null>(null)

/** A flow started earlier (this session) that can be resumed instead of restarted. */
const resumable = computed(() => wizard.resumableProject)

// ------------------------------------------------------------------ cover photo
function releasePreview(): void {
  if (coverPreview.value !== null) {
    URL.revokeObjectURL(coverPreview.value)
    coverPreview.value = null
  }
}

function onCoverChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  // Allow re-picking the same file after a validation failure or a failed upload.
  input.value = ''
  if (file === undefined) return

  if (!isAcceptedImage(file)) {
    fieldErrors.value = { main_photo: t('inspector.projectForm.invalidFile') }
    return
  }
  if (file.size > MAX_FILE_BYTES) {
    fieldErrors.value = {
      main_photo: t('inspector.projectForm.fileTooLarge', {
        size: formatBytes(file.size),
        limit: formatBytes(MAX_FILE_BYTES),
      }),
    }
    return
  }

  fieldErrors.value = { ...fieldErrors.value, main_photo: '' }
  releasePreview()
  coverFile.value = file
  coverPreview.value = URL.createObjectURL(file)
}

function removeCover(): void {
  releasePreview()
  coverFile.value = null
}

onUnmounted(releasePreview)

// ---------------------------------------------------------------------- submit
async function submit(): Promise<void> {
  if (submitting.value) return

  const name = form.name.trim()
  if (name.length === 0) {
    fieldErrors.value = { ...fieldErrors.value, name: t('inspector.projectForm.nameRequired') }
    return
  }

  submitting.value = true
  fieldErrors.value = {}
  coverFailure.value = null

  try {
    // `detect_defects` is deliberately not sent: detection is part of every run,
    // so the API's own default (on) is the only value there is.
    const project = await createProject({
      name,
      location: form.location.trim() || null,
      description: form.description.trim() || null,
      needs_3d_model: form.needs3dModel,
    })
    wizard.remember(project)

    if (coverFile.value !== null) {
      const uploaded = await uploadCover(project.id)
      if (!uploaded) return
    }

    toast.success(
      t('inspector.wizard.toast.created'),
      t('inspector.wizard.toast.createdBody', { name: project.name }),
    )
    await router.push({ name: ROUTE_NAMES.projectPhotos, params: { projectId: project.id } })
  } catch (cause) {
    // A 422 names the offending field; bind it instead of only showing a banner.
    if (cause instanceof ApiError && cause.isValidationError) {
      fieldErrors.value = { ...fieldErrors.value, ...cause.fieldErrors }
    }
    toast.error(t('inspector.wizard.toast.createFailed'), describeApiError(cause))
  } finally {
    submitting.value = false
  }
}

/** Upload the selected cover photo. Returns false (with the failure shown) if it did not work. */
async function uploadCover(projectId: string): Promise<boolean> {
  const file = coverFile.value
  if (file === null) return true

  try {
    wizard.remember(await uploadProjectMainPhoto(projectId, file))
    return true
  } catch (cause) {
    coverFailure.value = describeApiError(cause)
    toast.warning(t('inspector.wizard.toast.coverFailed'), coverFailure.value)
    return false
  }
}

async function retryCover(): Promise<void> {
  const projectId = wizard.projectId
  if (projectId === null || submitting.value) return

  submitting.value = true
  try {
    if (await uploadCover(projectId)) {
      toast.success(t('inspector.wizard.toast.coverUploaded'))
      await goToUpload(projectId)
    }
  } finally {
    submitting.value = false
  }
}

async function goToUpload(projectId: string | null): Promise<void> {
  if (projectId === null) return
  await router.push({ name: ROUTE_NAMES.projectPhotos, params: { projectId } })
}

function continueWithoutCover(): void {
  wizard.markCoverPhotoSkipped()
  toast.info(
    t('inspector.wizard.toast.skipCover'),
    t('inspector.wizard.toast.skipCoverBody'),
  )
  void goToUpload(wizard.projectId)
}

/** Abandon the remembered flow so this form creates a new project instead. */
function startOver(): void {
  wizard.clear()
  toast.info(
    t('inspector.wizard.toast.startingOver'),
    t('inspector.wizard.toast.startingOverBody'),
  )
}
</script>

<template>
  <div class="space-y-5">
    <!-- Resume banner: a project from this session is still open. -->
    <div
      v-if="resumable && coverFailure === null"
      class="flex flex-wrap items-center gap-3 rounded-xl border border-blue-200 bg-blue-50 p-4"
    >
      <AppIcon name="info" class="h-5 w-5 shrink-0 text-blue-600" />
      <p class="min-w-0 flex-1 text-sm text-blue-900">
        <span class="font-semibold">{{ resumable.name }}</span>
        {{ t('inspector.wizard.step1.resume') }}
      </p>
      <div class="flex shrink-0 items-center gap-2">
        <button
          type="button"
          class="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white transition hover:bg-blue-700"
          @click="goToUpload(resumable.id)"
        >
          {{ t('inspector.wizard.step1.continueWithIt') }}
        </button>
        <button
          type="button"
          class="rounded-lg border border-blue-300 px-3 py-1.5 text-sm font-medium text-blue-800 transition hover:bg-blue-100"
          @click="startOver"
        >
          {{ t('inspector.wizard.step1.startNew') }}
        </button>
      </div>
    </div>

    <!-- Cover upload failed: the project exists, so do not create another. -->
    <div
      v-if="coverFailure"
      role="alert"
      class="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900"
    >
      <div class="flex items-start gap-2.5">
        <AppIcon name="alert" class="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
        <div class="min-w-0">
          <p class="font-semibold">{{ t('inspector.wizard.step1.coverFailedTitle') }}</p>
          <p class="mt-1">{{ coverFailure }}</p>
          <p class="mt-1 text-amber-800">{{ t('inspector.wizard.step1.coverFailedHint') }}</p>
        </div>
      </div>
      <div class="mt-3 flex flex-wrap items-center gap-2 pl-7">
        <button
          type="button"
          :disabled="submitting || coverFile === null"
          class="inline-flex items-center gap-2 rounded-lg bg-amber-600 px-3 py-2 text-sm font-semibold text-white transition hover:bg-amber-700 disabled:cursor-not-allowed disabled:bg-amber-300"
          @click="retryCover"
        >
          <span
            v-if="submitting"
            class="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white"
            aria-hidden="true"
          />
          {{
            coverFile === null
              ? t('inspector.wizard.step1.selectAgain')
              : t('inspector.wizard.step1.retryUpload')
          }}
        </button>
        <button
          type="button"
          class="rounded-lg border border-amber-300 px-3 py-2 text-sm font-medium text-amber-900 transition hover:bg-amber-100"
          @click="continueWithoutCover"
        >
          {{ t('inspector.wizard.step1.continueWithoutCover') }}
        </button>
      </div>
    </div>

    <form
      class="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8"
      @submit.prevent="submit"
    >
      <div class="grid gap-6">
        <div>
          <label for="project-name" class="block text-sm font-medium text-slate-700">
            {{ t('inspector.projectForm.name') }} <span class="text-red-600">*</span>
          </label>
          <input
            id="project-name"
            v-model="form.name"
            type="text"
            name="name"
            required
            maxlength="255"
            :disabled="submitting"
            :placeholder="t('inspector.projectForm.namePlaceholder')"
            class="mt-1.5 block w-full rounded-lg border bg-white px-3 py-2 text-sm text-slate-900 shadow-xs transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 focus:outline-none disabled:bg-slate-50 disabled:text-slate-400"
            :class="fieldErrors.name ? 'border-red-400' : 'border-slate-300'"
          />
          <p v-if="fieldErrors.name" class="mt-1.5 text-sm text-red-600">{{ fieldErrors.name }}</p>
          <p v-else class="mt-1.5 text-sm text-slate-500">
            {{ t('inspector.wizard.step1.nameHint') }}
          </p>
        </div>

        <div>
          <label for="project-location" class="block text-sm font-medium text-slate-700">
            {{ t('inspector.projectForm.location') }}
          </label>
          <input
            id="project-location"
            v-model="form.location"
            type="text"
            name="location"
            maxlength="512"
            :disabled="submitting"
            :placeholder="t('inspector.projectForm.locationPlaceholder')"
            class="mt-1.5 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-xs transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 focus:outline-none disabled:bg-slate-50 disabled:text-slate-400"
          />
        </div>

        <div>
          <label for="project-description" class="block text-sm font-medium text-slate-700">
            {{ t('inspector.projectForm.description') }}
          </label>
          <textarea
            id="project-description"
            v-model="form.description"
            name="description"
            rows="4"
            maxlength="10000"
            :disabled="submitting"
            :placeholder="t('inspector.projectForm.descriptionPlaceholder')"
            class="mt-1.5 block w-full resize-y rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-xs transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 focus:outline-none disabled:bg-slate-50 disabled:text-slate-400"
          />
        </div>

        <!-- Cover photo -->
        <div>
          <span class="block text-sm font-medium text-slate-700">
            {{ t('inspector.projectForm.photo') }}
          </span>
          <p class="mt-1 text-sm text-slate-500">
            {{ t('inspector.wizard.step1.photoHelp') }}
          </p>

          <div v-if="coverPreview" class="mt-3 flex flex-wrap items-center gap-4">
            <img
              :src="coverPreview"
              :alt="t('inspector.wizard.step1.coverAlt')"
              class="h-24 w-40 rounded-lg border border-slate-200 object-cover"
            />
            <div class="min-w-0">
              <p class="truncate text-sm font-medium text-slate-900">{{ coverFile?.name }}</p>
              <p class="text-xs text-slate-500">
                {{ coverFile ? formatBytes(coverFile.size) : '' }} ·
                {{ coverFile ? (coverFile.type || t('inspector.projectForm.typeFallback')) : '' }}
              </p>
              <div class="mt-2 flex items-center gap-2">
                <label
                  class="cursor-pointer rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-100 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-blue-600"
                >
                  {{ t('inspector.projectForm.replace') }}
                  <input
                    type="file"
                    class="sr-only"
                    accept="image/jpeg,image/png,image/tiff,image/webp"
                    :disabled="submitting"
                    @change="onCoverChange"
                  />
                </label>
                <button
                  type="button"
                  class="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-slate-100 hover:text-red-700"
                  :disabled="submitting"
                  @click="removeCover"
                >
                  <AppIcon name="trash" class="h-3.5 w-3.5" />
                  {{ t('inspector.projectForm.remove') }}
                </button>
              </div>
            </div>
          </div>

          <label
            v-else
            class="mt-3 flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-slate-300 bg-slate-50 px-6 py-8 text-center transition hover:border-blue-400 hover:bg-blue-50/40 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-blue-600"
          >
            <AppIcon name="image" class="h-6 w-6 text-slate-400" />
            <span class="text-sm font-medium text-slate-700">
              {{ t('inspector.projectForm.choose') }}
            </span>
            <span class="text-xs text-slate-500">
              {{ t('inspector.projectForm.coverTypes', { size: formatBytes(MAX_FILE_BYTES) }) }}
            </span>
            <input
              type="file"
              class="sr-only"
              accept="image/jpeg,image/png,image/tiff,image/webp"
              :disabled="submitting"
              @change="onCoverChange"
            />
          </label>

          <p v-if="fieldErrors.main_photo" class="mt-2 text-sm text-red-600">
            {{ fieldErrors.main_photo }}
          </p>
        </div>

        <!--
          What processing produces. Detection is part of every run and is not a
          choice here, so the only switch left is the reconstruction: the run is
          always 2D, and 3D adds a photogrammetry pass on top of it.
        -->
        <div class="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-200 p-4">
          <div class="min-w-0">
            <p class="text-sm font-medium text-slate-900">
              {{ t('inspector.wizard.step1.modelOutput') }}
            </p>
            <p class="mt-0.5 text-sm text-slate-500">
              {{ t('inspector.wizard.step1.modelOutputHelp') }}
            </p>
          </div>
          <div class="flex shrink-0 items-center gap-3">
            <span
              class="text-sm transition"
              :class="form.needs3dModel ? 'text-slate-400' : 'font-semibold text-slate-900'"
            >
              2D
            </span>
            <button
              type="button"
              role="switch"
              :aria-checked="form.needs3dModel"
              :aria-label="t('inspector.wizard.step1.generate3d')"
              :disabled="submitting"
              class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:opacity-60"
              :class="form.needs3dModel ? 'bg-blue-600' : 'bg-slate-300'"
              @click="form.needs3dModel = !form.needs3dModel"
            >
              <span
                class="inline-block h-5 w-5 transform rounded-full bg-white shadow-sm transition"
                :class="form.needs3dModel ? 'translate-x-5' : 'translate-x-0.5'"
              />
            </button>
            <span
              class="text-sm transition"
              :class="form.needs3dModel ? 'font-semibold text-blue-700' : 'text-slate-400'"
            >
              3D
            </span>
            <AppIcon
              v-if="form.needs3dModel"
              name="cube"
              class="h-4 w-4 text-blue-600"
            />
          </div>
        </div>
      </div>

      <div class="mt-7 flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-5">
        <RouterLink
          :to="{ name: ROUTE_NAMES.dashboard }"
          class="rounded-lg px-3 py-2 text-sm font-medium text-slate-600 transition hover:bg-slate-100"
        >
          {{ t('common.cancel') }}
        </RouterLink>

        <button
          type="submit"
          :disabled="submitting"
          class="inline-flex items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:bg-blue-400"
        >
          <span
            v-if="submitting"
            class="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
            aria-hidden="true"
          />
          <AppIcon v-else name="arrowRight" class="h-4 w-4" />
          {{
            submitting
              ? t('inspector.wizard.step1.creating')
              : t('inspector.wizard.step1.create')
          }}
        </button>
      </div>
    </form>
  </div>
</template>
