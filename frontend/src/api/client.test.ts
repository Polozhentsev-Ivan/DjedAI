import { ApiError, apiUrl, request, send } from './client';

describe('api client', () => {
  afterEach(() => { vi.restoreAllMocks(); });

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
