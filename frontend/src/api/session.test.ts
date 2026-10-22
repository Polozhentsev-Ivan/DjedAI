describe('tab session', () => {
  beforeEach(() => { vi.resetModules(); });
  afterEach(() => { vi.restoreAllMocks(); });

  it('restores only the current tab token after reload', async () => {
    window.localStorage.setItem('access_token', 'other-tab-token');
    window.sessionStorage.setItem('djedai.access_token', 'current-tab-token');
    const { sessionSnapshot } = await import('./session');
    expect(sessionSnapshot().accessToken).toBe('current-tab-token');
  });

  it('replaces credentials in memory/sessionStorage and aborts the previous lifetime', async () => {
    const { SESSION_TOKEN_KEY, sessionSnapshot, setAccessToken, clearSession } = await import('./session');
    const original = sessionSnapshot();
    setAccessToken('first-token');
    expect(original.signal.aborted).toBe(true);
    expect(window.sessionStorage.getItem(SESSION_TOKEN_KEY)).toBe('first-token');
    const first = sessionSnapshot();
    setAccessToken('second-token');
    expect(first.signal.aborted).toBe(true);
    expect(sessionSnapshot().accessToken).toBe('second-token');
    clearSession();
    expect(sessionSnapshot().accessToken).toBeNull();
    expect(window.sessionStorage.getItem(SESSION_TOKEN_KEY)).toBeNull();
    expect(window.localStorage.length).toBe(0);
  });

  it('keeps credentials in memory if sessionStorage is blocked', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('Blocked', 'SecurityError'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('Blocked', 'SecurityError'); });
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new DOMException('Blocked', 'SecurityError'); });
    const { sessionSnapshot, setAccessToken, clearSession } = await import('./session');
    expect(sessionSnapshot().accessToken).toBeNull();
    setAccessToken('memory-token');
    expect(sessionSnapshot().accessToken).toBe('memory-token');
    clearSession();
    expect(sessionSnapshot().accessToken).toBeNull();
  });

  it('does not let an obsolete session invalidate a replacement', async () => {
    const { sessionSnapshot, setAccessToken, clearSessionIfCurrent } = await import('./session');
    setAccessToken('old');
    const previous = sessionSnapshot();
    setAccessToken('new');
    clearSessionIfCurrent(previous);
    expect(sessionSnapshot().accessToken).toBe('new');
  });
});
