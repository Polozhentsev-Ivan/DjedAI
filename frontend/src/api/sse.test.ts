import { ApiError } from './client';
import { SseParser, streamEvents, type AgentEventMap, type StreamEvent } from './sse';
import { clearSession, sessionSnapshot, SESSION_TOKEN_KEY, setAccessToken } from './session';

const encoder = new TextEncoder();

/** Response whose body arrives in the given chunks; `hang` keeps it open afterwards. */
function streamResponse(chunks: (string | Uint8Array)[], { hang = false, status = 200 } = {}) {
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      const next = chunks.shift();
      if (next !== undefined) {
        controller.enqueue(typeof next === 'string' ? encoder.encode(next) : next);
        return;
      }
      if (hang) await new Promise(() => undefined);
      controller.close();
    },
    cancel() { cancelled = true; },
  });
  return { response: new Response(body, { status, headers: { 'Content-Type': 'text/event-stream' } }), wasCancelled: () => cancelled };
}

describe('SseParser', () => {
  it('assembles an event split across chunks', () => {
    const parser = new SseParser();
    expect(parser.push('event: sta')).toEqual([]);
    expect(parser.push('tus\ndata: {"node":"plan",')).toEqual([]);
    expect(parser.push('"text":"Считаю"}\n\n')).toEqual([{ event: 'status', data: '{"node":"plan","text":"Считаю"}' }]);
  });

  it('keeps a multi-byte character split between chunks', () => {
    const parser = new SseParser();
    const bytes = encoder.encode('data: "₽"\n\n');
    const cut = bytes.indexOf(0xe2) + 1;
    expect(parser.push(bytes.slice(0, cut))).toEqual([]);
    expect(parser.push(bytes.slice(cut))).toEqual([{ event: 'message', data: '"₽"' }]);
  });

  it('handles several events, comments, ids and multi-line data', () => {
    const parser = new SseParser();
    const out = parser.push(': ping\n\nevent: a\nid: 7\ndata: 1\n\nevent: b\ndata: [1,\ndata: 2]\n\nevent: c\n\n');
    expect(out).toEqual([{ event: 'a', data: '1', id: '7' }, { event: 'b', data: '[1,\n2]' }]);
  });

  it('accepts CRLF even when the pair is split', () => {
    const parser = new SseParser();
    expect(parser.push('event: done\r\ndata: {}\r')).toEqual([]);
    expect(parser.push('\n\r\n')).toEqual([{ event: 'done', data: '{}' }]);
  });
});

describe('streamEvents', () => {
  beforeEach(() => { clearSession(); });
  afterEach(() => { clearSession(); vi.restoreAllMocks(); });

  it('delivers events until done and closes the stream', async () => {
    const { response, wasCancelled } = streamResponse([
      'event: status\ndata: {"node":"plan","text":"Анализирую"}\n\nevent: blo',
      'ck\ndata: {"type":"text","markdown":"Выручка растёт"}\n\n',
      'event: done\ndata: {"message_id":"m_1"}\n\n',
      'event: status\ndata: {"node":"late","text":"не должно прийти"}\n\n',
    ], { hang: true });
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response);
    const events: StreamEvent<AgentEventMap>[] = [];
    const outcome = await streamEvents<AgentEventMap>('/chats/c_1/messages', { method: 'POST', json: { text: 'Покажи выручку' }, onEvent: (e) => events.push(e) });
    expect(outcome).toBe('done');
    expect(events.map((e) => e.event)).toEqual(['status', 'block', 'done']);
    expect(wasCancelled()).toBe(true);
    const [url, init] = fetchSpy.mock.calls[0]!;
    expect(url).toBe('http://localhost:3000/api/chats/c_1/messages');
    expect(init?.method).toBe('POST');
    expect(init?.body).toBe('{"text":"Покажи выручку"}');
  });

  it('stops on an error event', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamResponse(['event: error\ndata: {"code":"timeout","message":"Таймаут"}\n\n']).response);
    const onEvent = vi.fn();
    expect(await streamEvents('/x', { onEvent })).toBe('error');
    expect(onEvent).toHaveBeenCalledWith({ event: 'error', data: { code: 'timeout', message: 'Таймаут' } });
  });

  it.each(['GET', 'POST'] as const)('authorizes %s SSE with the tab Bearer', async (method) => {
    setAccessToken('stream-secret');
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamResponse(['event: done\ndata: {}\n\n']).response);
    await expect(streamEvents('/protected/events', { method, ...(method === 'POST' ? { json: { text: '?' } } : {}), onEvent: vi.fn() })).resolves.toBe('done');
    const [url, init] = fetchSpy.mock.calls[0]!;
    expect(init?.headers).toHaveProperty('Authorization', 'Bearer stream-secret');
    expect(init?.headers).toHaveProperty('Accept', 'text/event-stream');
    expect(url).not.toContain('stream-secret');
    expect(init?.body ?? '').not.toContain('stream-secret');
  });

  it.each(['clear', 'replace'] as const)('aborts active streams when credentials %s', async (change) => {
    setAccessToken('old-session');
    const { response, wasCancelled } = streamResponse(['event: progress\ndata: {}\n\n'], { hang: true });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(response);
    const onEvent = vi.fn(() => { if (change === 'clear') clearSession(); else setAccessToken('new-session'); });
    await expect(streamEvents('/datasets/d/events', { onEvent })).resolves.toBe('aborted');
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(wasCancelled()).toBe(true);
  });

  it('invalidates an expired session on SSE 401 before any event is delivered', async () => {
    setAccessToken('expired-session');
    const session = sessionSnapshot();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ error: { code: 'unauthorized', message: 'Expired' } }, { status: 401 }));
    const onEvent = vi.fn();
    await expect(streamEvents('/datasets/d/events', { onEvent })).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    expect(onEvent).not.toHaveBeenCalled();
    expect(session.signal.aborted).toBe(true);
    expect(window.sessionStorage.getItem(SESSION_TOKEN_KEY)).toBeNull();
  });

  it('reports an interrupted stream without sending the request again', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamResponse(['event: status\ndata: {"node":"plan","text":"…"}\n\n']).response);
    const error = await streamEvents('/chats/c_1/messages', { method: 'POST', json: { text: '?' }, onEvent: vi.fn() }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe('stream_interrupted');
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('clears only the current session after revocation in an already open stream', async () => {
    setAccessToken('revoked-session');
    const session = sessionSnapshot();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamResponse([
      'event: error\ndata: {"code":"unauthorized","message":"Сессия недействительна"}\n\n',
    ]).response);
    const onEvent = vi.fn();
    await expect(streamEvents('/chats/c/messages', { onEvent })).resolves.toBe('error');
    expect(onEvent).toHaveBeenCalledOnce();
    expect(session.signal.aborted).toBe(true);
    expect(window.sessionStorage.getItem(SESSION_TOKEN_KEY)).toBeNull();
  });

  it('treats a broken read as an interruption', async () => {
    const body = new ReadableStream<Uint8Array>({ pull(controller) { controller.error(new TypeError('network')); } });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(body));
    await expect(streamEvents('/x', { onEvent: vi.fn() })).rejects.toMatchObject({ code: 'stream_interrupted' });
  });

  it('resolves as aborted when the subscriber leaves', async () => {
    const { response, wasCancelled } = streamResponse(['event: progress\ndata: {"step":"read","status":"active"}\n\n'], { hang: true });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(response);
    const controller = new AbortController();
    const onEvent = vi.fn(() => { controller.abort(); });
    await expect(streamEvents('/datasets/d/events', { onEvent, signal: controller.signal })).resolves.toBe('aborted');
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(wasCancelled()).toBe(true);
  });

  it('returns aborted when fetch is aborted before the response', async () => {
    const controller = new AbortController();
    controller.abort();
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new DOMException('aborted', 'AbortError'));
    expect(await streamEvents('/x', { onEvent: vi.fn(), signal: controller.signal })).toBe('aborted');
  });

  it('turns HTTP and network failures into ApiError', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: 'not_found', message: 'Чат не найден' } }), { status: 404 }));
    await expect(streamEvents('/x', { onEvent: vi.fn() })).rejects.toMatchObject({ status: 404, message: 'Чат не найден' });
    vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await expect(streamEvents('/x', { onEvent: vi.fn() })).rejects.toMatchObject({ code: 'network' });
  });

  it('rejects malformed JSON', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamResponse(['event: block\ndata: {oops\n\n']).response);
    await expect(streamEvents('/x', { onEvent: vi.fn() })).rejects.toMatchObject({ code: 'bad_event' });
  });
});
