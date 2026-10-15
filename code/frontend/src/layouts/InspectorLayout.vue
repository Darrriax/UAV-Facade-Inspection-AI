<script setup lang="ts">
/**
 * Shell for the Inspector area (dashboard, wizard steps, project screens).
 *
 * Supplies the chrome that every signed-in screen shares - brand, primary
 * navigation, the account block, sign-out - and the single `<RouterView />` the
 * child routes render into. Route-level layouts like this one are why the views
 * themselves can stay focused on their own content and never repeat a header.
 *
 * The Admin area has its own layout (step 3+); this one is deliberately the
 * Inspector's, so its navigation does not try to serve both roles.
 *
 * A route may set `meta.fullBleed` to opt out of the centred, padded column -
 * the project workspace does, because two panels that each handle their own
 * scrolling cannot live inside a page that also scrolls. The footer goes too:
 * a full-height tool screen has nothing below the fold to scroll to.
 *
 * For a full-bleed route the shell is pinned to the viewport (`h-dvh`), not just
 * given a `min-height`. That distinction is what makes the panels' `min-h-0`
 * work: with `min-h-screen` the shell's height stays content-driven, so a long
 * defect list would stretch its column and scroll the document instead of the
 * list, however carefully the panels are written.
 */

import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import type { IconName } from '@/components/icons'
import { SUPPORTED_LOCALES, setLocale } from '@/i18n'
import { ROUTE_NAMES, type RouteName } from '@/router/routes/names'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const { t, locale } = useI18n()
const mobileMenuOpen = ref(false)
const fullBleed = computed(() => route.meta.fullBleed === true)

/**
 * The active language, as the code the button shows.
 *
 * The button displays *where the reader is* - "UA" while the interface is
 * Ukrainian - so the thing it labels is the state it describes. The language it
 * moves to is in the tooltip and the accessible name, which is where a target
 * belongs on a one-press cycle.
 */
const currentLocale = computed(
  () => SUPPORTED_LOCALES.find((option) => option.code === locale.value) ?? SUPPORTED_LOCALES[0],
)

/**
 * The language one press moves to. Cycled by index, so a third language would
 * need no change here.
 */
const nextLocale = computed(() => {
  const index = SUPPORTED_LOCALES.findIndex((option) => option.code === locale.value)
  return SUPPORTED_LOCALES[(index + 1) % SUPPORTED_LOCALES.length]
})

const switchLabel = computed(() =>
  nextLocale.value === undefined
    ? t('language.label')
    : t('language.switchTo', { language: nextLocale.value.label }),
)

function switchLanguage(): void {
  if (nextLocale.value !== undefined) setLocale(nextLocale.value.code)
}

const navItems: readonly { name: RouteName; labelKey: string; icon: IconName }[] = [
  { name: ROUTE_NAMES.dashboard, labelKey: 'nav.dashboard', icon: 'grid' },
  { name: ROUTE_NAMES.projectNew, labelKey: 'nav.newProject', icon: 'plus' },
]

function signOut(): void {
  mobileMenuOpen.value = false
  // No API call: the token is stateless, so dropping it *is* the sign-out. Any
  // copy of it left in another tab stops working on its next request only if it
  // was revoked server-side (deactivation), not merely deleted here.
  auth.logout()
  void router.push({ name: ROUTE_NAMES.login })
}
</script>

<template>
  <div
    class="flex flex-col bg-slate-50"
    :class="fullBleed ? 'h-dvh overflow-hidden' : 'min-h-screen'"
  >
    <header class="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur-sm">
      <div class="mx-auto flex h-16 w-full max-w-7xl items-center gap-3 px-4 sm:px-6 lg:px-8">
        <RouterLink :to="{ name: ROUTE_NAMES.dashboard }" class="flex shrink-0 items-center">
          <!--
            Text-only brand. The initials badge that used to lead it is gone, so
            the name is no longer hidden on small screens - it is all that is
            left of the mark, and a header with neither would be blank there.
          -->
          <span class="text-base font-semibold tracking-tight text-slate-900">
            {{ t('app.name') }}
          </span>
        </RouterLink>

        <nav class="ml-2 hidden items-center gap-1 md:flex" :aria-label="t('nav.main')">
          <RouterLink
            v-for="item in navItems"
            :key="item.name"
            :to="{ name: item.name }"
            class="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-slate-600 transition hover:bg-slate-100 hover:text-slate-900"
            active-class="bg-blue-50 text-blue-700 hover:bg-blue-50 hover:text-blue-700"
          >
            <AppIcon :name="item.icon" class="h-4 w-4" />
            {{ t(item.labelKey) }}
          </RouterLink>
        </nav>

        <div class="ml-auto flex items-center gap-2">
          <!--
            The compact language switch: a rectangle carrying the active
            language's own code, as a two-letter control the header has room for
            beside the account block.

            Visible at every width - unlike the account block and Logout below,
            which the small-screen menu carries instead - because a reader who
            cannot read the interface has to be able to reach it without signing
            out first.
          -->
          <button
            type="button"
            class="inline-flex h-9 min-w-11 shrink-0 items-center justify-center rounded-lg border border-slate-300 bg-white px-2.5 text-xs font-semibold tracking-wide text-slate-700 transition hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
            :aria-label="switchLabel"
            :title="switchLabel"
            @click="switchLanguage"
          >
            {{ currentLocale.shortLabel }}
          </button>

          <RouterLink
            :to="{ name: ROUTE_NAMES.profile }"
            class="hidden items-center gap-2.5 rounded-lg px-2 py-1.5 transition hover:bg-slate-100 sm:flex"
          >
            <!--
              Placeholder avatar: `profile_photo_s3_path` is an object key, and
              the API has no presigned-URL endpoint yet, so the bytes are not
              reachable from the browser. Initials until that endpoint lands.
            -->
            <span
              class="inline-flex h-8 w-8 items-center justify-center rounded-full bg-slate-200 text-xs font-semibold text-slate-700 ring-1 ring-slate-300 ring-inset"
            >
              {{ auth.initials || '?' }}
            </span>
            <span class="hidden text-left leading-tight lg:block">
              <span class="block text-sm font-medium text-slate-900">{{ auth.fullName }}</span>
              <span class="block text-xs text-slate-500 capitalize">{{ auth.role ?? '—' }}</span>
            </span>
          </RouterLink>

          <button
            type="button"
            class="hidden items-center gap-2 rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 sm:inline-flex"
            @click="signOut"
          >
            <AppIcon name="logOut" class="h-4 w-4" />
            {{ t('nav.logout') }}
          </button>

          <button
            type="button"
            class="inline-flex h-10 w-10 items-center justify-center rounded-lg text-slate-600 transition hover:bg-slate-100 md:hidden"
            :aria-expanded="mobileMenuOpen"
            aria-controls="mobile-nav"
            :aria-label="mobileMenuOpen ? t('nav.closeMenu') : t('nav.openMenu')"
            @click="mobileMenuOpen = !mobileMenuOpen"
          >
            <AppIcon :name="mobileMenuOpen ? 'close' : 'menu'" class="h-5 w-5" />
          </button>
        </div>
      </div>

      <div v-if="mobileMenuOpen" id="mobile-nav" class="border-t border-slate-200 bg-white md:hidden">
        <nav class="mx-auto max-w-7xl space-y-1 px-4 py-3 sm:px-6" :aria-label="t('nav.mainMobile')">
          <RouterLink
            v-for="item in navItems"
            :key="item.name"
            :to="{ name: item.name }"
            class="flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium text-slate-700 transition hover:bg-slate-100"
            active-class="bg-blue-50 text-blue-700"
            @click="mobileMenuOpen = false"
          >
            <AppIcon :name="item.icon" class="h-4 w-4" />
            {{ t(item.labelKey) }}
          </RouterLink>
          <RouterLink
            :to="{ name: ROUTE_NAMES.profile }"
            class="flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium text-slate-700 transition hover:bg-slate-100"
            active-class="bg-blue-50 text-blue-700"
            @click="mobileMenuOpen = false"
          >
            <AppIcon name="user" class="h-4 w-4" />
            {{ t('nav.profile') }}
          </RouterLink>
          <button
            type="button"
            class="flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-sm font-medium text-slate-700 transition hover:bg-slate-100"
            @click="signOut"
          >
            <AppIcon name="logOut" class="h-4 w-4" />
            {{ t('nav.logout') }}
          </button>
        </nav>
      </div>
    </header>

    <main
      :class="
        fullBleed
          ? 'flex min-h-0 flex-1 flex-col'
          : 'mx-auto w-full max-w-7xl flex-1 px-4 py-8 sm:px-6 lg:px-8'
      "
    >
      <RouterView />
    </main>

    <footer v-if="!fullBleed" class="border-t border-slate-200 bg-white">
      <p class="mx-auto max-w-7xl px-4 py-4 text-xs text-slate-500 sm:px-6 lg:px-8">
        {{ t('app.footer', { name: t('app.name') }) }}
      </p>
    </footer>
  </div>
</template>
