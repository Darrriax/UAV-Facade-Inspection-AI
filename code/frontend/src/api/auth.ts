/** `/login` and the current-session lookup. */

import { apiRequest } from './client'
import type { LoginPayload, TokenResponse, User } from '@/types/api'

/**
 * Exchange credentials for an access token.
 *
 * `auth: false` because there is no session yet - and because a 401 from this
 * call is a wrong password, not an expired session, so it must not trip the
 * global "signed out" handler.
 */
export function login(payload: LoginPayload): Promise<TokenResponse> {
  return apiRequest<TokenResponse>('/login', { method: 'POST', body: payload, auth: false })
}

/** The authenticated user's own record - `GET /users/me`. */
export function fetchCurrentUser(signal?: AbortSignal): Promise<User> {
  return apiRequest<User>('/users/me', { signal })
}
