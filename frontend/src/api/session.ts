/** Session credentials belong to this tab; workspace/query cleanup is handled by the UI. */
export const SESSION_TOKEN_KEY = 'djedai.access_token';

function restoredToken(): string | null {
  try {
    return window.sessionStorage.getItem(SESSION_TOKEN_KEY);
  } catch {
    // Storage may be unavailable (for example, browser privacy settings).
    return null;
  }
}

let accessToken = restoredToken();
let controller = new AbortController();

export interface SessionSnapshot {
  readonly accessToken: string | null;
  readonly signal: AbortSignal;
}

/** Captures the credentials and lifetime of one session for an outgoing request. */
export function sessionSnapshot(): SessionSnapshot {
  return { accessToken, signal: controller.signal };
}

export function isCurrentSession(session: SessionSnapshot): boolean {
  return session.signal === controller.signal && !session.signal.aborted;
}

/** Replacing credentials also stops every request/stream started by the previous session. */
export function setAccessToken(token: string | null): void {
  const previous = controller;
  controller = new AbortController();
  accessToken = token;
  try {
    if (token === null) window.sessionStorage.removeItem(SESSION_TOKEN_KEY);
    else window.sessionStorage.setItem(SESSION_TOKEN_KEY, token);
  } catch {
    // Keep the current page usable with an in-memory session when storage is blocked.
  }
  previous.abort();
}

export function clearSession(): void {
  setAccessToken(null);
}

/** A delayed 401/logout must never remove credentials from a newer login. */
export function clearSessionIfCurrent(session: SessionSnapshot): void {
  if (isCurrentSession(session)) clearSession();
}

export function sessionSignal(session: SessionSnapshot, signal?: AbortSignal): AbortSignal {
  return signal ? AbortSignal.any([session.signal, signal]) : session.signal;
}
