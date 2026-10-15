<script setup lang="ts">
/**
 * Edit one project's details: name, location, description and cover photo.
 *
 * The dialog is a *sibling* of the card's link rather than a child of it, for
 * the same reason Delete is - a button inside an anchor navigates. It is also
 * deliberately a dialog and not a route: nothing here needs a URL, and the
 * dashboard grid behind it has to keep its scroll position.
 *
 * Two requests, in a fixed order:
 *
 * 1. `PATCH /projects/{id}` carries the text fields. It runs first so a cover
 *    upload that then fails leaves the details saved - the failure is reported
 *    against the photo alone and the dialog stays open to retry it, which is
 *    the same shape step 1 of the wizard uses.
 * 2. `POST /projects/{id}/main-photo` only when a new file was picked. It
 *    answers with the whole project, so its result is the one handed back to
 *    the caller: it already contains the patched fields, and re-reading them
 *    from an earlier response would show stale data.
 *
 * Nothing here can *delete* a cover: the API replaces the object behind the
 * project's own key, and dropping the reference would orphan it in storage.
 *
 * Every string comes from the catalogues, including the local validation
 * messages below - this dialog is the dashboard's own, so it has to switch
 * language with the grid underneath it.
 */

import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { ApiError, describeApiError } from '@/api/client'
import {
  MAX_FILE_BYTES,
  getProjectMainPhotoUrl,
  updateProject,
  uploadProjectMainPhoto,
} from '@/api/projects'
import AppIcon from '@/components/AppIcon.vue'
import { useToasts } from '@/composables/useToasts'
import type { Project } from '@/types/api'
import { formatBytes, isAcceptedImage } from '@/utils/files'

const props = defineProps<{ project: Project }>()
const emit = defineEmits<{
  close: []
  /** The project as the server last described it, after a successful save. */
  saved: [project: Project]
}>()

const toast = useToasts()
const { t } = useI18n()

const form = reactive({
  name: props.project.name,
  location: props.project.location ?? '',
  description: props.project.description ?? '',
})

const coverFile = ref<File | null>(null)
/** Object URL of the picked file; beats the stored cover while it is set. */
const coverPreview = ref<string | null>(null)
/** Signed URL of the cover the project already has, when it has one. */
const storedCoverUrl = ref<string | null>(null)
const fieldErrors = ref<Record<string, string>>({})
const coverFailure = ref<string | null>(null)
const submitting = ref(false)

const hasStoredCover = computed(() => props.project.main_photo_s3_path !== null)
const coverSrc = computed(() => coverPreview.value ?? storedCoverUrl.value)
/**
 * Text the user changed, as the payload the PATCH will carry.
 *
 * The comparisons normalise `null` to `''` on both sides: the API stores an
 * absent location as null while the form holds an empty string, and comparing
 * the two raw would make every untouched project look dirty.
 */
const payload = computed(() => ({
  name: form.name.trim(),
  location: form.location.trim() || null,
  description: form.description.trim() || null,
}))
const isDirty = computed(
  () =>
    payload.value.name !== props.project.name ||
    (payload.value.location ?? '') !== (props.project.location ?? '') ||
    (payload.value.description ?? '') !== (props.project.description ?? '') ||
    coverFile.value !== null,
)

// ---------------------------------------------------------------- cover photo
function releasePreview(): void {
  if (coverPreview.value !== null) {
    URL.revokeObjectURL(coverPreview.value)
    coverPreview.value = null
  }
}

async function loadStoredCover(): Promise<void> {
  if (props.project.main_photo_s3_path === null) return
  try {
    storedCoverUrl.value = (await getProjectMainPhotoUrl(props.project.id)).url
  } catch {
    // A 404 means the row points at nothing renderable: the dashed placeholder
    // below is the right answer, not an error the user has to dismiss.
    storedCoverUrl.value = null
  }
}

function onCoverChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  // Allow re-picking the same file after a validation failure.
  input.value = ''
  if (file === undefined) return

  if (!isAcceptedImage(file)) {
    fieldErrors.value = {
      ...fieldErrors.value,
      main_photo: t('inspector.projectForm.invalidFile'),
    }
    return
  }
  if (file.size > MAX_FILE_BYTES) {
    fieldErrors.value = {
      ...fieldErrors.value,
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

function removePickedCover(): void {
  releasePreview()
  coverFile.value = null
}

// -------------------------------------------------------------------- submit
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

  let saved = props.project
  try {
    // Only what actually changed: an untouched field is left out of the PATCH
    // rather than written back as itself.
    const patch: Record<string, string | boolean | null> = {}
    if (payload.value.name !== props.project.name) patch.name = payload.value.name
    if ((payload.value.location ?? '') !== (props.project.location ?? ''))
      patch.location = payload.value.location
    if ((payload.value.description ?? '') !== (props.project.description ?? ''))
      patch.description = payload.value.description

    if (Object.keys(patch).length > 0) saved = await updateProject(props.project.id, patch)
  } catch (cause) {
    if (cause instanceof ApiError && cause.isValidationError) {
      fieldErrors.value = { ...fieldErrors.value, ...cause.fieldErrors }
    }
    toast.error(t('inspector.dashboard.dialog.toastSaveFailed'), describeApiError(cause))
    submitting.value = false
    return
  }

  if (coverFile.value !== null) {
    try {
      saved = await uploadProjectMainPhoto(props.project.id, coverFile.value)
    } catch (cause) {
      // The text fields are already on the server: report them as saved and
      // keep the dialog open with the picked file still in place, so pressing
      // Save again retries the photo alone (the PATCH then has nothing to do).
      coverFailure.value = describeApiError(cause)
      emit('saved', saved)
      toast.warning(t('inspector.dashboard.dialog.toastPhotoFailed'), coverFailure.value)
      submitting.value = false
      return
    }
  }

  emit('saved', saved)
  toast.success(
    t('inspector.dashboard.dialog.toastSaved'),
    t('inspector.dashboard.dialog.toastSavedBody', { name: saved.name }),
  )
  emit('close')
}

// ------------------------------------------------------------------ lifecycle
function close(): void {
  if (submitting.value) return
  emit('close')
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') close()
}

onMounted(() => {
  void loadStoredCover()
  window.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  releasePreview()
  window.removeEventListener('keydown', onKeydown)
})

// The dialog is mounted per project, so this only fires when the parent swaps
// it for another one without closing it: re-seed the form rather than leaving
// one project's details on screen above another's name.
watch(
  () => props.project.id,
  () => {
    form.name = props.project.name
    form.location = props.project.location ?? ''
    form.description = props.project.description ?? ''
    fieldErrors.value = {}
    coverFailure.value = null
    releasePreview()
    coverFile.value = null
    storedCoverUrl.value = null
    void loadStoredCover()
  },
)
</script>

<template>
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
    @click.self="close"
  >
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="edit-project-title"
      class="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-slate-200 bg-white p-6 shadow-xl"
    >
      <div class="flex items-start gap-3">
        <span class="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-blue-100">
          <AppIcon name="edit" class="h-5 w-5 text-blue-600" />
        </span>
        <div class="min-w-0 flex-1">
          <!--
            The project's name is in the heading itself: the dialog is opened
            from one card in a grid of them, and "which project am I editing?"
            has to be answerable without closing it to look.
          -->
          <h2 id="edit-project-title" class="text-base font-semibold text-slate-900">
            {{ t('inspector.dashboard.dialog.title', { name: project.name }) }}
          </h2>
        </div>
        <button
          type="button"
          :aria-label="t('common.close')"
          class="-mt-1 -mr-1 rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
          :disabled="submitting"
          @click="close"
        >
          <AppIcon name="close" class="h-5 w-5" />
        </button>
      </div>

      <form class="mt-5" @submit.prevent="submit">
        <div class="grid gap-5">
        <div>
          <label for="edit-project-name" class="block text-sm font-medium text-slate-700">
            {{ t('inspector.projectForm.name') }} <span class="text-red-600">*</span>
          </label>
          <input
            id="edit-project-name"
            v-model="form.name"
            type="text"
            name="name"
            required
            maxlength="255"
            :disabled="submitting"
            class="mt-1.5 block w-full rounded-lg border bg-white px-3 py-2 text-sm text-slate-900 shadow-xs transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 focus:outline-none disabled:bg-slate-50 disabled:text-slate-400"
            :class="fieldErrors.name ? 'border-red-400' : 'border-slate-300'"
          />
          <p v-if="fieldErrors.name" class="mt-1.5 text-sm text-red-600">
            {{ fieldErrors.name }}
          </p>
        </div>

        <div>
          <label for="edit-project-location" class="block text-sm font-medium text-slate-700">
            {{ t('inspector.projectForm.location') }}
          </label>
          <input
            id="edit-project-location"
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
          <label for="edit-project-description" class="block text-sm font-medium text-slate-700">
            {{ t('inspector.projectForm.description') }}
          </label>
          <textarea
            id="edit-project-description"
            v-model="form.description"
            name="description"
            rows="4"
            maxlength="10000"
            :disabled="submitting"
            :placeholder="t('inspector.projectForm.descriptionPlaceholder')"
            class="mt-1.5 block w-full resize-y rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-xs transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 focus:outline-none disabled:bg-slate-50 disabled:text-slate-400"
          />
        </div>

        <!-- Main photo -->
        <div>
          <span class="block text-sm font-medium text-slate-700">
            {{ t('inspector.projectForm.photo') }}
          </span>
          <p class="mt-1 text-sm text-slate-500">
            {{ t('inspector.dashboard.dialog.photoHelp') }}
          </p>

          <div v-if="coverSrc" class="mt-3 flex flex-wrap items-center gap-4">
            <img
              :src="coverSrc"
              :alt="t('inspector.dashboard.dialog.photoAlt')"
              class="h-24 w-40 rounded-lg border border-slate-200 object-cover"
            />
            <div class="min-w-0">
              <p v-if="coverFile" class="truncate text-sm font-medium text-slate-900">
                {{ coverFile.name }}
              </p>
              <p v-else class="text-sm font-medium text-slate-900">
                {{
                  hasStoredCover
                    ? t('inspector.dashboard.dialog.currentPhoto')
                    : t('inspector.dashboard.dialog.selectedPhoto')
                }}
              </p>
              <p class="text-xs text-slate-500">
                <template v-if="coverFile">
                  {{ formatBytes(coverFile.size) }} ·
                  {{ coverFile.type || t('inspector.projectForm.typeFallback') }}
                </template>
                <template v-else>
                  {{ t('inspector.projectForm.coverTypes', { size: formatBytes(MAX_FILE_BYTES) }) }}
                </template>
              </p>
              <div class="mt-2 flex flex-wrap items-center gap-2">
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
                  v-if="coverFile !== null"
                  type="button"
                  class="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-slate-100 hover:text-red-700"
                  :disabled="submitting"
                  @click="removePickedCover"
                >
                  <AppIcon name="trash" class="h-3.5 w-3.5" />
                  {{ t('inspector.projectForm.discard') }}
                </button>
              </div>
            </div>
          </div>

          <label
            v-else
            class="mt-3 flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-slate-300 bg-slate-50 px-6 py-6 text-center transition hover:border-blue-400 hover:bg-blue-50/40 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-blue-600"
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
          <p v-if="coverFailure" class="mt-2 text-sm text-red-600">
            {{ t('inspector.dashboard.dialog.photoFailed', { error: coverFailure }) }}
          </p>
        </div>
        </div>

        <!--
          Sticky: the form is taller than a small viewport, and a Cancel button
          below the fold is a Cancel button nobody can press.
        -->
        <div
          class="sticky right-0 bottom-0 -mx-6 -mb-6 mt-5 flex flex-wrap items-center justify-end gap-2 border-t border-slate-100 bg-white px-6 pt-4 pb-6"
        >
          <button
            type="button"
            class="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100 disabled:opacity-50"
            :disabled="submitting"
            @click="close"
          >
            {{ t('common.cancel') }}
          </button>
          <button
            type="submit"
            :disabled="submitting || !isDirty"
            class="inline-flex items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:bg-blue-400"
          >
            <span
              v-if="submitting"
              class="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
              aria-hidden="true"
            />
            <AppIcon v-else name="check" class="h-4 w-4" />
            {{
              submitting
                ? t('inspector.dashboard.dialog.saving')
                : t('inspector.dashboard.dialog.save')
            }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>
