/**
 * Account endpoints.
 *
 * Two surfaces live here, matching the backend: the Admin-only administration
 * calls (`/users`) and the self-service profile calls (`/users/me`). Both are
 * just typed wrappers - the store owns the session state, not this module.
 */

import { apiRequest } from './client'
import type {
  MyProfileUpdatePayload,
  Page,
  User,
  UserCreatePayload,
  UserListQuery,
  UserUpdatePayload,
} from '@/types/api'

// ------------------------------------------------------------------- profile
/** `PATCH /users/me` - names, country, age, phone, password, photo key. */
export function updateMyProfile(payload: MyProfileUpdatePayload): Promise<User> {
  return apiRequest<User>('/users/me', { method: 'PATCH', body: payload })
}

// --------------------------------------------------------------------- admin
/** `GET /users` - Admin only. Pass `role: 'inspector'` for the dashboard list. */
export function listUsers(
  query: UserListQuery = {},
  signal?: AbortSignal,
): Promise<Page<User>> {
  return apiRequest<Page<User>>('/users', { query: { ...query }, signal })
}

/** `POST /users` - Admin creates an Inspector account with a password. */
export function createUser(payload: UserCreatePayload): Promise<User> {
  return apiRequest<User>('/users', { method: 'POST', body: payload })
}

/** `GET /users/{id}` - Admins may read anyone; others only themselves. */
export function getUser(userId: string, signal?: AbortSignal): Promise<User> {
  return apiRequest<User>(`/users/${userId}`, { signal })
}

/**
 * `PATCH /users/{id}` - Admin-only in practice for `role` and `is_active`.
 * Deactivating an Inspector is `{ is_active: false }` on this endpoint; their
 * existing tokens stop working immediately (checked on every request).
 */
export function updateUser(userId: string, payload: UserUpdatePayload): Promise<User> {
  return apiRequest<User>(`/users/${userId}`, { method: 'PATCH', body: payload })
}

/** `DELETE /users/{id}` - cascades to the account's projects and images. */
export function deleteUser(userId: string): Promise<void> {
  return apiRequest<void>(`/users/${userId}`, { method: 'DELETE' })
}
