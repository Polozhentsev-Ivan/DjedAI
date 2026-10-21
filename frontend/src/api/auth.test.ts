import { login, logout, me, register } from './auth';
import { clearSession, SESSION_TOKEN_KEY, sessionSnapshot, setAccessToken } from './session';

const user = { user_id: 'u1', username: 'alice', created_at: '2026-10-21T10:00:00Z' };
const credentials = { username: 'alice', password: 'not-a-real-password' };

function pendingResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  return { promise, resolve };
}

describe('auth API', () => {
  beforeEach(() => { clearSession(); });
  afterEach(() => { clearSession(); vi.restoreAllMocks(); });

  it('registers without authorizing or creating a session', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json(user, { status: 201 }));
    expect(await register(credentials)).toEqual(user);
    const [url, init] = fetchSpy.mock.calls[0]!;
    expect(url).toBe('http://localhost:3000/api/auth/register');
    expect(init?.body).toBe(JSON.stringify(credentials));
    expect(init?.headers).not.toHaveProperty('Authorization');
    expect(sessionSnapshot().accessToken).toBeNull();
  });

  it('stores a login token and uses it for me and logout, without leaking it elsewhere', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(Response.json({ access_token: 'secret-session-token', token_type: 'bearer', expires_in: 86400 }))
      .mockResolvedValueOnce(Response.json(user))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const localWrite = vi.spyOn(window.localStorage, 'setItem');
    const log = vi.spyOn(console, 'log');
    const warn = vi.spyOn(console, 'warn');
    const error = vi.spyOn(console, 'error');
    await login(credentials);
    expect(window.sessionStorage.getItem(SESSION_TOKEN_KEY)).toBe('secret-session-token');
    expect(fetchSpy.mock.calls[0]![1]?.headers).not.toHaveProperty('Authorization');
    expect(await me()).toEqual(user);
    const session = sessionSnapshot();
    await logout();
    expect(session.signal.aborted).toBe(true);
    expect(sessionSnapshot().accessToken).toBeNull();
    expect(window.sessionStorage.getItem(SESSION_TOKEN_KEY)).toBeNull();
    for (const [url, init] of fetchSpy.mock.calls.slice(1)) {
      expect(init?.headers).toHaveProperty('Authorization', 'Bearer secret-session-token');
      expect(url).not.toContain('secret-session-token');
      expect(init?.body ?? '').not.toContain('secret-session-token');
    }
    expect(localWrite).not.toHaveBeenCalled();
    expect(log).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
  });

  it('does not overwrite a newer login with an older response', async () => {
    const firstResponse = pendingResponse();
    const secondResponse = pendingResponse();
    vi.spyOn(globalThis, 'fetch').mockReturnValueOnce(firstResponse.promise).mockReturnValueOnce(secondResponse.promise);
    const first = expect(login(credentials)).rejects.toMatchObject({ name: 'AbortError' });
    const second = login({ ...credentials, username: 'bob' });
    secondResponse.resolve(Response.json({ access_token: 'newer-login', expires_in: 86400 }));
    await second;
    firstResponse.resolve(Response.json({ access_token: 'obsolete-login', expires_in: 86400 }));
    await first;
    expect(sessionSnapshot().accessToken).toBe('newer-login');
    expect(window.sessionStorage.getItem(SESSION_TOKEN_KEY)).toBe('newer-login');
  });

  it('does not restore a login that was cleared while it was pending', async () => {
    const response = pendingResponse();
    vi.spyOn(globalThis, 'fetch').mockReturnValue(response.promise);
    const result = expect(login(credentials)).rejects.toMatchObject({ name: 'AbortError' });
    clearSession();
    response.resolve(Response.json({ access_token: 'obsolete-login', expires_in: 86400 }));
    await result;
    expect(sessionSnapshot().accessToken).toBeNull();
  });

  it('clears local credentials after logout failure', async () => {
    setAccessToken('current-session');
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('network'));
    await expect(logout()).rejects.toMatchObject({ code: 'network' });
    expect(sessionSnapshot().accessToken).toBeNull();
  });

  it('does not let a delayed logout remove a newer login', async () => {
    setAccessToken('old-session');
    const response = pendingResponse();
    vi.spyOn(globalThis, 'fetch').mockReturnValue(response.promise);
    const result = expect(logout()).rejects.toMatchObject({ name: 'AbortError' });
    setAccessToken('new-session');
    response.resolve(new Response(null, { status: 204 }));
    await result;
    expect(sessionSnapshot().accessToken).toBe('new-session');
  });

  it('keeps the current session on public registration 401', async () => {
    setAccessToken('current-session');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ error: { code: 'unauthorized', message: 'Denied' } }, { status: 401 }));
    await expect(register(credentials)).rejects.toMatchObject({ status: 401 });
    expect(sessionSnapshot().accessToken).toBe('current-session');
  });
});
