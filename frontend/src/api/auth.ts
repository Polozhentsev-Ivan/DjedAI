import { request, type Schemas } from './client';
import { clearSession, clearSessionIfCurrent, isCurrentSession, sessionSnapshot, setAccessToken } from './session';

/** Registration does not create a session; login is a separate explicit action. */
export function register(input: Schemas['UserCreate']): Promise<Schemas['UserRead']> {
  return request('/auth/register', { method: 'POST', json: input, authenticated: false });
}

export async function login(input: Schemas['UserLogin']): Promise<Schemas['TokenResponse']> {
  clearSession();
  const session = sessionSnapshot();
  const response = await request<Schemas['TokenResponse']>('/auth/login', { method: 'POST', json: input, authenticated: false });
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
  try {
    await request<undefined>('/auth/logout', { method: 'POST' });
  } finally {
    clearSessionIfCurrent(session);
  }
}
