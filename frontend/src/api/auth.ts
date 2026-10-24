import { request, send, type Schemas } from './client';
import { clearSession, clearSessionIfCurrent, isCurrentSession, sessionSnapshot, setAccessToken } from './session';

/** Registration does not create a session; login is a separate explicit action. */
export function register(input: Schemas['UserCreate'], signal?: AbortSignal): Promise<Schemas['UserRead']> {
  return request('/auth/register', { method: 'POST', json: input, authenticated: false, signal });
}

export async function login(input: Schemas['UserLogin'], signal?: AbortSignal): Promise<Schemas['TokenResponse']> {
  if (signal?.aborted) throw new DOMException('Request aborted', 'AbortError');
  clearSession();
  const session = sessionSnapshot();
  const response = await request<Schemas['TokenResponse']>('/auth/login', { method: 'POST', json: input, authenticated: false, signal });
  if (!isCurrentSession(session)) throw new DOMException('Session changed', 'AbortError');
  setAccessToken(response.access_token);
  return response;
}

/** Called after a tab reload before the UI restores its authenticated workspace. */
export function me(): Promise<Schemas['UserRead']> {
  return request('/auth/me');
}

export async function logout(): Promise<void> {
  const session = sessionSnapshot();
  // Capture the Bearer before clearing UI credentials. Revocation has its own
  // lifetime; it must neither keep old streams alive nor clear a newer login.
  const revocation = send('/auth/logout', { method: 'POST', sessionBound: false, signal: AbortSignal.timeout(10_000) });
  clearSessionIfCurrent(session);
  await revocation;
}
