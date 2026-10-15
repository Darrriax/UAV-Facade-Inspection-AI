/**
 * Authentication session.
 *
 * One store owns three things: the access token, the current user's profile and
 * the in-flight status of either. Views read it, the router guards read it, and
 * nothing else touches `localStorage` or the bearer header.
 *
 * Setup-store style (`defineStore('auth', () => { ... })`), so the pieces below
 * are plain refs/computed/async functions and TypeScript infers everything.
 * Because of that, there is no implicit `$reset` - `clearSession()` *is* the
 * reset, and every sign-out path goes through it.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { fetchCurrentUser, login as loginRequest } from '@/api/auth'
import { ApiError, describeApiError, setAccessToken } from '@/api/client'
import { updateMyProfile } from '@/api/users'
import { readStoredToken, writeStoredToken } from '@/utils/storage'
import type { LoginPayload, MyProfileUpdatePayload, User, UserRole } from '@/types/api'

export type AuthStatus =
  /** Token on disk, profile not fetched yet (the state right after a reload). */
  | 'idle'
  /** `restoreSession()` is running. */
  | 'restoring'
  /** `login()` is running. */
  | 'authenticating'
  /** Signed in, profile loaded. */
  | 'authenticated'
  /** No valid session. */
  | 'anonymous'

export const useAuthStore = defineStore('auth', () => {
  // ----------------------------------------------------------------- state
  const user = ref<User | null>(null)
  const accessToken = ref<string | null>(readStoredToken())
  const status = ref<AuthStatus>(accessToken.value ? 'idle' : 'anonymous')
  const error = ref<string | null>(null)

  // The HTTP client is deliberately store-agnostic (the store imports it, so
  // importing back would be a cycle), which means the token has to be pushed
  // into it. Done at store creation so a restored token is usable before the
  // first navigation has resolved.
  setAccessToken(accessToken.value)

  // --------------------------------------------------------------- getters
  /**
   * A profile is what "authenticated" means here. A token alone is not enough:
   * it can be expired, revoked by a deactivated account, or simply unknown to
   * this browser - all of which the API answers with 401.
   */
  const isAuthenticated = computed(() => user.value !== null)
  const role = computed<UserRole | null>(() => user.value?.role ?? null)
  const isAdmin = computed(() => role.value === 'admin')
  const isInspector = computed(() => role.value === 'inspector')
  const isLoading = computed(
    () => status.value === 'restoring' || status.value === 'authenticating',
  )

  /** "Dmytro Yakovenko", falling back to the email when no name is set yet. */
  const fullName = computed(() => {
    const name = [user.value?.first_name, user.value?.last_name]
      .filter((part): part is string => Boolean(part))
      .join(' ')
    return name || user.value?.email || ''
  })

  /** Initials for the avatar placeholder: "DY". */
  const initials = computed(() => {
    const parts = [user.value?.first_name, user.value?.last_name].filter(
      (part): part is string => Boolean(part),
    )
    const source = parts.length ? parts : [user.value?.email ?? '']
    return source
      .map((part) => part.trim().charAt(0).toUpperCase())
      .filter(Boolean)
      .slice(0, 2)
      .join('')
  })

  function hasRole(...roles: UserRole[]): boolean {
    return role.value !== null && roles.includes(role.value)
  }

  // --------------------------------------------------------------- actions
  /**
   * Swap the session token.
   *
   * Writes through to the HTTP client *and* to storage in one place, so the
   * three copies can never drift apart.
   */
  function applyToken(token: string | null): void {
    accessToken.value = token
    setAccessToken(token)
    writeStoredToken(token)
  }

  /** Reset everything a sign-out must clear. The store's `$reset`. */
  function clearSession(): void {
    applyToken(null)
    user.value = null
    status.value = 'anonymous'
    error.value = null
  }

  /**
   * Sign in and load the profile.
   *
   * Two requests on purpose: `/login` returns a token and nothing else, and the
   * role in the token claim is only a debugging aid - RBAC must be driven by
   * the database row, so the profile is fetched rather than decoded.
   */
  async function login(payload: LoginPayload): Promise<User> {
    status.value = 'authenticating'
    error.value = null

    try {
      const token = await loginRequest(payload)
      applyToken(token.access_token)

      const profile = await fetchCurrentUser()
      user.value = profile
      status.value = 'authenticated'
      return profile
    } catch (cause) {
      // Nothing half-applied: a failed login or a rejected token leaves the
      // store exactly as anonymous as it was before.
      clearSession()
      error.value = describeApiError(cause)
      throw cause
    }
  }

  /**
   * The in-flight `restoreSession()` call, shared by every caller.
   *
   * `main.ts` starts the restore and the router guard waits on it, and those two
   * overlap by construction: installing the router triggers the first
   * navigation. Without sharing, the guard would either fire a second
   * `GET /users/me` or - worse - judge the session while the first one is still
   * in flight. See `restoreSession()`.
   */
  let pendingRestore: Promise<User | null> | null = null

  /**
   * Rebuild the session after a page reload.
   *
   * Idempotent and safe to await from several places at once (see
   * `pendingRestore`). A 401 means the token is dead - drop it. A network/5xx
   * failure does *not*: the token may still be perfectly good and the API
   * merely unreachable, so it is kept for the next attempt.
   */
  async function restoreSession(): Promise<User | null> {
    if (user.value) {
      status.value = 'authenticated'
      return user.value
    }

    if (!accessToken.value) {
      status.value = 'anonymous'
      return null
    }

    pendingRestore ??= performRestore().finally(() => {
      pendingRestore = null
    })
    return pendingRestore
  }

  async function performRestore(): Promise<User | null> {
    status.value = 'restoring'
    try {
      const profile = await fetchCurrentUser()
      user.value = profile
      status.value = 'authenticated'
      error.value = null
      return profile
    } catch (cause) {
      if (cause instanceof ApiError && cause.isUnauthorized) {
        clearSession()
      } else {
        user.value = null
        status.value = 'anonymous'
        error.value = describeApiError(cause)
      }
      return null
    }
  }

  /** Re-read the profile from the API (after an edit made elsewhere). */
  async function refreshProfile(): Promise<User | null> {
    if (!accessToken.value) return null
    try {
      const profile = await fetchCurrentUser()
      user.value = profile
      return profile
    } catch (cause) {
      error.value = describeApiError(cause)
      throw cause
    }
  }

  /**
   * Patch the signed-in user's own profile.
   *
   * The response is the updated row, so the store is refreshed from the
   * server's copy instead of guessing what the API normalised (country is
   * up-cased, names are collapsed, timestamps change).
   */
  async function updateProfile(payload: MyProfileUpdatePayload): Promise<User> {
    const profile = await updateMyProfile(payload)
    user.value = profile
    return profile
  }

  /** Change the password through the same profile endpoint. */
  async function changePassword(password: string): Promise<User> {
    return updateProfile({ password })
  }

  function logout(): void {
    clearSession()
  }

  return {
    // state
    user,
    accessToken,
    status,
    error,
    // getters
    isAuthenticated,
    isLoading,
    role,
    isAdmin,
    isInspector,
    fullName,
    initials,
    // actions
    login,
    logout,
    clearSession,
    restoreSession,
    refreshProfile,
    updateProfile,
    changePassword,
    hasRole,
  }
})
