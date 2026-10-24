import { ApiError, apiUrl, request, send } from './client';
import { clearSession, sessionSnapshot, SESSION_TOKEN_KEY, setAccessToken } from './session';

describe('api client', () => {
  beforeEach(() => { clearSession(); });
  afterEach(() => { clearSession(); vi.restoreAllMocks(); });

  it('builds URLs from the configured base', () => {
    expect(apiUrl('/datasets')).toBe('http://localhost:3000/api/datasets');
  });

  it('sends JSON and parses the answer', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ chat_id: 'c_1' }, { status: 201 }));
    await expect(request('/chats', { method: 'POST', json: { dataset_id: 'd' } })).resolves.toEqual({ chat_id: 'c_1' });
    const init = fetchSpy.mock.calls[0]![1]!;
    expect(init.headers).toMatchObject({ 'Content-Type': 'application/json' });
  });

  it('returns undefined for 204', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    await expect(request('/datasets/d', { method: 'DELETE' })).resolves.toBeUndefined();
  });

  it('passes form data through untouched', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({}));
    const form = new FormData();
    await send('/datasets/upload', { method: 'POST', form });
    expect(fetchSpy.mock.calls[0]![1]!.body).toBe(form);
  });

  it('uses the same session Bearer for JSON and multipart without putting it in URLs or bodies', async () => {
    setAccessToken('tab-secret');
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(Response.json({})));
    await request('/chats', { method: 'POST', json: { dataset_id: 'd' }, headers: { authorization: 'Bearer forged' } });
    const form = new FormData();
    form.append('files', new File(['a\n1'], 'table.csv', { type: 'text/csv' }));
    await request('/datasets/upload', { method: 'POST', form });
    for (const [url, init] of fetchSpy.mock.calls) {
      expect(init?.headers).toHaveProperty('Authorization', 'Bearer tab-secret');
      expect(init?.headers).not.toHaveProperty('authorization');
      expect(url).not.toContain('tab-secret');
      expect(init?.body).not.toBe('tab-secret');
    }
    expect(fetchSpy.mock.calls[1]![1]!.body).toBe(form);
    expect(fetchSpy.mock.calls[1]![1]!.headers).not.toHaveProperty('Content-Type');
  });

  it('clears current credentials and aborts their lifetime on 401', async () => {
    setAccessToken('expired-token');
    const session = sessionSnapshot();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ error: { code: 'unauthorized', message: 'Войдите снова' } }, { status: 401 }));
    await expect(request('/auth/me')).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    expect(session.signal.aborted).toBe(true);
    expect(sessionSnapshot().accessToken).toBeNull();
    expect(window.sessionStorage.getItem(SESSION_TOKEN_KEY)).toBeNull();
  });

  it('does not let a delayed 401 clear a newer session', async () => {
    setAccessToken('old-token');
    let resolve!: (response: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockReturnValue(new Promise<Response>((done) => { resolve = done; }));
    const result = expect(request('/auth/me')).rejects.toMatchObject({ status: 401 });
    setAccessToken('new-token');
    resolve(Response.json({ error: { code: 'unauthorized', message: 'Expired' } }, { status: 401 }));
    await result;
    expect(sessionSnapshot().accessToken).toBe('new-token');
  });

  it('rejects a stale JSON body even if the response ignores abort', async () => {
    setAccessToken('old-token');
    let resolve!: (body: unknown) => void;
    const response = Response.json({});
    const readJson = vi.spyOn(response, 'json').mockReturnValue(new Promise((done) => { resolve = done; }));
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(response);
    const result = expect(request('/datasets')).rejects.toMatchObject({ name: 'AbortError' });
    await vi.waitFor(() => { expect(readJson).toHaveBeenCalled(); });
    setAccessToken('new-token');
    resolve([{ dataset_id: 'old-user-data' }]);
    await result;
  });

  it('reads our error shape, FastAPI detail and bare statuses', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    fetchSpy.mockResolvedValueOnce(Response.json({ error: { code: 'bad_csv', message: 'Строка 3', suggestion: 'Проверьте файл' } }, { status: 422 }));
    await expect(request('/x')).rejects.toMatchObject({ status: 422, code: 'bad_csv', message: 'Строка 3', suggestion: 'Проверьте файл' });
    fetchSpy.mockResolvedValueOnce(Response.json({ detail: 'Not Found' }, { status: 404 }));
    await expect(request('/x')).rejects.toMatchObject({ code: 'http_404', message: 'Not Found', suggestion: null });
    fetchSpy.mockResolvedValueOnce(new Response('oops', { status: 502 }));
    await expect(request('/x')).rejects.toMatchObject({ message: 'Сервер ответил ошибкой 502' });
  });

  it('reports network failures and keeps aborts as aborts', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    fetchSpy.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const error = await request('/x').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
    fetchSpy.mockRejectedValueOnce(new DOMException('aborted', 'AbortError'));
    await expect(request('/x')).rejects.toMatchObject({ name: 'AbortError' });
  });
});
