import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw/http';
import type { Schemas } from '@/api/client';
import { keys } from '@/api/queries';
import { clearSession } from '@/api/session';
import { createDb } from '@/mocks/db';
import { chatFixture, fixtureEvent, fixtureResult, fixtureResultMeta } from '@/test/chatFixture';
import { renderRoute } from '@/test/renderRoute';
import { server, useScenario } from '@/test/server';

const api = (path: string) => `http://localhost:3000/api${path}`;
// The mocked answer's markdown emphasis is rendered, not shown as asterisks.
const answer = '3,4 млн ₽';
const question = 'Сколько строк в моём CSV?';
const encoder = new TextEncoder();

async function ask(text = question) {
  await waitFor(() => { expect(screen.getByRole('textbox', { name: 'Вопрос к данным' })).toBeEnabled(); });
  const input = screen.getByRole('textbox', { name: 'Вопрос к данным' });
  await userEvent.type(input, text);
  await userEvent.click(screen.getByRole('button', { name: 'Отправить' }));
}

function liveStream() {
  let stream: ReadableStreamDefaultController<Uint8Array> | undefined;
  let signal: AbortSignal | undefined;
  let calls = 0;
  let messageId = '';
  const realFetch = globalThis.fetch;
  vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (url.endsWith('/messages')) signal = init?.signal ?? undefined;
    return realFetch(input, init);
  });
  server.use(http.post(api('/chats/:id/messages'), async ({ request }) => {
    const body = await request.json() as Schemas['SendMessageRequest'];
    messageId = `m_${body.request_id?.replaceAll('-', '')}`;
    calls += 1;
    return new HttpResponse(new ReadableStream<Uint8Array>({ start(controller) { stream = controller; } }), { headers: { 'Content-Type': 'text/event-stream' } });
  }), http.post(api('/chats/:id/messages/:messageId/cancel'), () => HttpResponse.json({
    run_id: 'run_stopped', status: 'cancelled', node: 'understand', message: 'Получение ответа остановлено', code: 'cancelled', started_at: new Date().toISOString(), deadline_at: new Date().toISOString(), finished_at: new Date().toISOString(),
  } satisfies Schemas['ChatTurnState'])));
  return {
    ready: () => waitFor(() => { expect(stream).toBeDefined(); }),
    emit: (event: string, data: unknown) => { act(() => { stream?.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)); }); },
    raw: (bytes: Uint8Array) => { act(() => { stream?.enqueue(bytes); }); },
    close: () => { act(() => { stream?.close(); }); },
    aborted: () => signal?.aborted,
    signal: () => signal,
    calls: () => calls,
    messageId: () => messageId,
  };
}

describe('CP1 chat integration', () => {
  afterEach(() => { vi.restoreAllMocks(); });
  it('creates a chat for the selected dataset and submits the actual input once', async () => {
    const calls: unknown[] = [];
    const observe = ({ request }: { request: Request }) => { if (request.url.endsWith('/messages')) calls.push(request); };
    server.events.on('request:start', observe);
    const { router } = await renderRoute('/chat');
    await ask();
    await waitFor(() => { expect(router.state.location.pathname).toMatch(/^\/chat\/c_/); });
    expect(await screen.findByText(answer, { selector: 'strong' })).toBeInTheDocument();
    await waitFor(() => { expect(screen.queryByRole('button', { name: 'Остановить' })).not.toBeInTheDocument(); });
    expect(screen.getAllByText(answer, { selector: 'strong' })).toHaveLength(1);
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
    expect(document.querySelectorAll('[data-role="user"]')).toHaveLength(1);
    expect(document.querySelector('[data-role="user"]')).toHaveTextContent(question);
    expect(calls).toHaveLength(1);
    server.events.removeListener('request:start', observe);
  });

  it('restores saved messages and provenance after reopening the route', async () => {
    const saved = createDb().chats[0]!;
    saved.messages = [
      { message_id: 'u_saved', role: 'user', text: question, created_at: saved.created_at },
      { message_id: 'a_saved', role: 'assistant', blocks: [{ type: 'text', markdown: 'Сохранённый ответ: 10 строк' }], result_ids: ['r_rev'], created_at: saved.created_at },
    ];
    server.use(http.get(api('/chats/c_q1'), () => HttpResponse.json(saved)));
    const view = await renderRoute('/chat/c_q1');
    expect(await screen.findByText('Сохранённый ответ: 10 строк')).toBeInTheDocument();
    await userEvent.click(screen.getByText('Результат расчёта r_rev'));
    const table = await screen.findByRole('table', { name: 'Данные результата r_rev' });
    expect(within(table).getByText('1 800 000')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Метаданные JSON' })).toHaveAttribute('href', api('/results/r_rev'));
    expect(screen.getByRole('link', { name: 'Данные JSON' })).toHaveAttribute('href', api('/results/r_rev/data'));
    expect(screen.getByText("time_aggregate(table='orders', freq='month')")).toBeInTheDocument();
    view.unmount();
    await renderRoute('/chat/c_q1');
    expect(await screen.findByText('Сохранённый ответ: 10 строк')).toBeInTheDocument();
    expect(screen.getAllByText('Результат расчёта r_rev')).toHaveLength(1);
  });

  it('shows status and blocks as they arrive, then replaces them with saved history once', async () => {
    const stream = liveStream();
    const saved = createDb().chats[0]!;
    server.use(http.get(api('/chats/c_q1'), () => HttpResponse.json(saved)));
    await renderRoute('/chat/c_q1');
    await ask();
    await stream.ready();
    stream.emit('status', { node: 'execute', text: 'Считаем строки на сервере' });
    expect(await screen.findByRole('status')).toHaveTextContent('Считаем строки на сервере');
    stream.emit('block', { type: 'text', markdown: 'Всего 42 строки' });
    expect(await screen.findByText('Всего 42 строки')).toBeInTheDocument();
    saved.messages = [
      { message_id: stream.messageId(), role: 'user', text: question, created_at: saved.created_at },
      { message_id: 'm_42', role: 'assistant', blocks: [{ type: 'text', markdown: 'Всего 42 строки' }], created_at: saved.created_at },
    ];
    stream.emit('done', { message_id: 'm_42' });
    await waitFor(() => { expect(screen.queryByRole('button', { name: 'Остановить' })).not.toBeInTheDocument(); });
    expect(screen.getAllByText('Всего 42 строки')).toHaveLength(1);
    expect(document.querySelectorAll('[data-role="user"]')).toHaveLength(1);
    expect(stream.aborted()).toBe(false);
  });

  it('stops the POST stream without repeating it', async () => {
    const stream = liveStream();
    await renderRoute('/chat/c_q1');
    await ask();
    await stream.ready();
    stream.emit('status', { node: 'understand', text: 'Разбираю вопрос…', message_id: stream.messageId(), run_id: 'run_stopped' });
    expect(screen.getByRole('textbox', { name: 'Вопрос к данным' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Остановить' }));
    await waitFor(() => { expect(stream.aborted()).toBe(true); });
    expect(await screen.findByRole('alert')).toHaveTextContent('Получение ответа остановлено');
    expect(stream.calls()).toBe(1);
  });

  it('aborts on chat change and does not show the previous stream in the new chat', async () => {
    const stream = liveStream();
    await renderRoute('/chat/c_q1');
    await ask();
    await stream.ready();
    stream.emit('block', { type: 'text', markdown: 'Предыдущий незаконченный ответ' });
    expect(await screen.findByText('Предыдущий незаконченный ответ')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: /^Регионы/ }));
    await waitFor(() => { expect(stream.aborted()).toBe(true); });
    expect(await screen.findByRole('heading', { name: 'Регионы' })).toBeInTheDocument();
    expect(screen.queryByText('Предыдущий незаконченный ответ')).not.toBeInTheDocument();
    expect(stream.calls()).toBe(1);
  });

  it('waits for acceptance before confirming an early stop', async () => {
    const stream = liveStream();
    await renderRoute('/chat/c_q1');
    await ask();
    await stream.ready();
    await userEvent.click(screen.getByRole('button', { name: 'Остановить' }));
    expect(screen.getByRole('status')).toHaveTextContent('Останавливаю запрос…');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(stream.aborted()).toBe(false);
    stream.emit('status', { node: 'load_ctx', text: 'Загружаю контекст чата…', message_id: stream.messageId() });
    await waitFor(() => { expect(stream.aborted()).toBe(true); });
    expect(await screen.findByRole('alert')).toHaveTextContent('Получение ответа остановлено');
    expect(stream.calls()).toBe(1);
  });

  it('aborts a live stream on unmount', async () => {
    const stream = liveStream();
    const view = await renderRoute('/chat/c_q1');
    await ask();
    await stream.ready();
    view.unmount();
    await waitFor(() => { expect(stream.aborted()).toBe(true); });
    expect(stream.calls()).toBe(1);
  });

  it('reports disconnect and restores history with no automatic POST retry', async () => {
    const stream = liveStream();
    await renderRoute('/chat/c_q1');
    await ask();
    await stream.ready();
    stream.emit('status', { node: 'plan', text: 'Составляю план' });
    stream.close();
    expect(await screen.findByRole('alert')).toHaveTextContent('Соединение прервано до конца ответа');
    expect(stream.calls()).toBe(1);
    await waitFor(() => { expect(screen.queryByRole('button', { name: 'Остановить' })).not.toBeInTheDocument(); });
    await ask('Повторить по моему запросу');
    await waitFor(() => { expect(stream.calls()).toBe(2); });
  });

  it('shows SSE errors and keeps explicit retry under the user’s control', async () => {
    useScenario('error');
    await renderRoute('/chat/c_q1');
    await ask();
    expect(await screen.findByRole('alert')).toHaveTextContent('Модель не ответила за отведённое время');
    expect(screen.getByRole('alert')).toHaveTextContent('Отправьте вопрос снова, если нужно.');
    expect(screen.queryByRole('button', { name: 'Повторить' })).not.toBeInTheDocument();
  });

  it('shows a failed create and HTTP send error', async () => {
    server.use(http.post(api('/chats'), () => HttpResponse.json({ error: { code: 'create', message: 'Не удалось создать чат' } }, { status: 503 })));
    const view = await renderRoute('/chat');
    await ask();
    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось создать чат');
    view.unmount();
    server.use(http.post(api('/chats/:id/messages'), () => HttpResponse.json({ error: { code: 'busy', message: 'Сервер занят' } }, { status: 503 })));
    await renderRoute('/chat/c_q1');
    await ask();
    expect(await screen.findByRole('alert')).toHaveTextContent('Сервер занят');
  });

  it('shows missing history and disables sending without a dataset', async () => {
    const view = await renderRoute('/chat/missing');
    expect(await screen.findByRole('alert')).toHaveTextContent('Чат не найден');
    view.unmount();
    useScenario('empty');
    await renderRoute('/chat');
    expect(await screen.findByText('Датасет не выбран')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Вопрос к данным' })).toBeDisabled();
  });

  it('loads saved text and table/chart references using the common contracts', async () => {
    const saved = createDb().chats[0]!;
    saved.messages = [{ message_id: 'all', role: 'assistant', created_at: saved.created_at,
      blocks: [
        { type: 'table', result_id: 'r_rev' },
        { type: 'clarification', question: 'Какой период?', options: ['Месяц', 'Год'] },
        { type: 'dashboard_link', dashboard_id: 'd_q', title: 'Сохранённый дашборд' },
        { type: 'suggestions', items: ['Продолжить'] },
      ] satisfies Schemas['Block'][], result_ids: ['r_rev', 'r_rev'],
    }];
    server.use(http.get(api('/chats/c_q1'), () => HttpResponse.json(saved)));
    await renderRoute('/chat/c_q1');
    expect(await screen.findByText('Какой период?')).toBeInTheDocument();
    expect(screen.getByText('Год')).toBeInTheDocument();
    expect(screen.getAllByText('Результат расчёта r_rev')).toHaveLength(1);
    expect(screen.getByRole('link', { name: /Сохранённый дашборд/ })).toHaveAttribute('href', '/dashboards/d_q');
  });

  it('keeps three identical failed questions distinct after a reload and retries only the selected one', async () => {
    const saved = createDb().chats[0]!;
    saved.messages = [];
    const outcomes = [
      ['model_timeout', 'Модель не ответила за отведённое время', 'timeout'],
      ['model_unavailable', 'Сервис модели временно недоступен', 'error'],
      ['cancelled', 'Получение ответа остановлено', 'cancelled'],
    ] as const;
    let posts = 0;
    server.use(http.get(api('/chats/c_q1'), () => HttpResponse.json(saved)),
      http.post(api('/chats/c_q1/messages'), async ({ request }) => {
        const body = await request.json() as Schemas['SendMessageRequest'];
        expect(body.request_id).toMatch(/^[\da-f-]{36}$/);
        const id = `m_${body.request_id?.replaceAll('-', '')}`;
        const now = new Date().toISOString();
        const outcome = outcomes[posts++];
        const state: Schemas['ChatTurnState'] = { run_id: `run_${posts}`, status: outcome?.[2] ?? 'success', node: 'understand', message: outcome?.[1] ?? 'Ответ готов', code: outcome?.[0] ?? null, started_at: now, deadline_at: now, finished_at: now, assistant_message_id: outcome ? null : 'a_retry' };
        saved.messages.push({ message_id: id, role: 'user', text: body.text, created_at: now, turn: state });
        if (!outcome) saved.messages.push({ message_id: 'a_retry', role: 'assistant', blocks: [{ type: 'text', markdown: 'Ответ после явного повтора' }], created_at: now });
        const frames: [string, unknown][] = outcome ? [['error', { code: state.code, message: state.message }]] : [['block', { type: 'text', markdown: 'Ответ после явного повтора' }], ['done', { message_id: 'a_retry' }]];
        return new HttpResponse(frames.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(''), { headers: { 'Content-Type': 'text/event-stream' } });
      }));
    const view = await renderRoute('/chat/c_q1');
    for (let index = 1; index <= 3; index++) {
      await ask('Одинаковый вопрос');
      await waitFor(() => { expect(screen.getAllByRole('alert')).toHaveLength(index); });
      await waitFor(() => { expect(screen.getByRole('textbox', { name: 'Вопрос к данным' })).toBeEnabled(); });
      expect(screen.getAllByText('Одинаковый вопрос')).toHaveLength(index);
    }
    expect(new Set(saved.messages.map((message) => message.message_id)).size).toBe(3);
    view.unmount();
    await renderRoute('/chat/c_q1');
    expect(await screen.findAllByRole('alert')).toHaveLength(3);
    for (const [code, title] of outcomes) {
      const alert = screen.getByText(title).closest('[data-message-id]')!;
      expect(within(alert as HTMLElement).getByText(new RegExp(`Код: ${code}`))).toBeInTheDocument();
    }
    expect(posts).toBe(3);
    expect(document.querySelectorAll('[data-role="assistant"]')).toHaveLength(0);
    await userEvent.click(screen.getAllByRole('button', { name: 'Повторить вопрос' })[0]!);
    expect(await screen.findByText('Ответ после явного повтора')).toBeInTheDocument();
    await waitFor(() => { expect(screen.getAllByText('Одинаковый вопрос')).toHaveLength(4); });
    expect(screen.getAllByRole('alert')).toHaveLength(3);
    expect(screen.getAllByText('Ответ после явного повтора')).toHaveLength(1);
    expect(posts).toBe(4);
  });

  it('shows a retry pause and the resumed stage without inventing a completed answer', async () => {
    const stream = liveStream();
    await renderRoute('/chat/c_q1');
    await ask();
    await stream.ready();
    stream.emit('status', { node: 'understand', phase: 'stage', text: 'Разбираю вопрос…' });
    expect(await screen.findByRole('status')).toHaveTextContent('Разбираю вопрос…');
    stream.emit('status', { node: 'understand', phase: 'retry', text: 'Ожидаю повторного запроса к модели…' });
    await waitFor(() => { expect(screen.getByRole('status')).toHaveTextContent('Ожидаю повторного запроса к модели…'); });
    stream.emit('status', { node: 'understand', phase: 'stage', text: 'Разбираю вопрос…' });
    await waitFor(() => { expect(screen.getByRole('status')).toHaveTextContent('Разбираю вопрос…'); });
    stream.emit('tool_result', { result_id: 'r_rev', ok: true });
    expect(document.querySelectorAll('[data-role="assistant"]')).toHaveLength(0);
    stream.emit('error', { code: 'model_unavailable', message: 'Сервис модели временно недоступен' });
    expect(await screen.findByRole('alert')).toHaveTextContent('Сервис модели временно недоступен');
    await waitFor(() => { expect(screen.getByRole('textbox', { name: 'Вопрос к данным' })).toBeEnabled(); });
    expect(stream.calls()).toBe(1);
  });

  it('can cancel a running question recovered by GET without posting the question again', async () => {
    const saved = createDb().chats[0]!;
    const now = new Date().toISOString();
    const state: Schemas['ChatTurnState'] = { run_id: 'run_restored', status: 'running', node: 'plan', message: 'Строю план анализа…', started_at: now, deadline_at: new Date(Date.now() + 30000).toISOString() };
    saved.messages = [{ message_id: 'm_restored', role: 'user', text: question, created_at: now, turn: state }];
    let cancellations = 0;
    let sends = 0;
    server.use(http.get(api('/chats/c_q1'), () => HttpResponse.json(saved)),
      http.post(api('/chats/c_q1/messages'), () => { sends++; return HttpResponse.error(); }),
      http.post(api('/chats/c_q1/messages/m_restored/cancel'), () => {
        cancellations++;
        Object.assign(state, { status: 'cancelled', code: 'cancelled', message: 'Получение ответа остановлено', finished_at: now });
        return HttpResponse.json(state);
      }));
    await renderRoute('/chat/c_q1');
    expect(await screen.findByRole('status')).toHaveTextContent('Строю план анализа…');
    await userEvent.click(screen.getByRole('button', { name: 'Остановить' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Получение ответа остановлено');
    expect(screen.getByRole('textbox', { name: 'Вопрос к данным' })).toBeEnabled();
    expect(cancellations).toBe(1);
    expect(sends).toBe(0);
  });

  it('clears the local question and buffered blocks when the session ends', async () => {
    const stream = liveStream();
    await renderRoute('/chat/c_q1');
    await ask('Частный вопрос');
    await stream.ready();
    stream.emit('block', { type: 'text', markdown: 'Частный промежуточный ответ' });
    expect(await screen.findByText('Частный промежуточный ответ')).toBeInTheDocument();
    act(() => { clearSession('expired'); });
    await waitFor(() => { expect(stream.aborted()).toBe(true); });
    expect(await screen.findByRole('button', { name: 'Войти в систему' })).toBeInTheDocument();
    expect(screen.queryByText('Частный вопрос')).not.toBeInTheDocument();
    expect(screen.queryByText('Частный промежуточный ответ')).not.toBeInTheDocument();
    expect(stream.calls()).toBe(1);
  });

  it('allows the next question after restored cancellation while history GET remains pending', async () => {
    const stream = liveStream();
    const saved = createDb().chats[0]!;
    const now = new Date().toISOString();
    const state: Schemas['ChatTurnState'] = { run_id: 'run_restored', status: 'running', node: 'plan', message: 'Строю план анализа…', started_at: now, deadline_at: new Date(Date.now() + 30000).toISOString() };
    saved.messages = [{ message_id: 'm_restored', role: 'user', text: question, created_at: now, turn: state }];
    let releaseHistory = () => { /* set below */ };
    const historyGate = new Promise<void>((resolve) => { releaseHistory = resolve; });
    let reads = 0;
    let cancellations = 0;
    server.use(http.get(api('/chats/c_q1'), async () => {
      if (++reads > 1) await historyGate;
      return HttpResponse.json(saved);
    }), http.post(api('/chats/c_q1/messages/m_restored/cancel'), () => {
      cancellations++;
      Object.assign(state, { status: 'cancelled', code: 'cancelled', message: 'Получение ответа остановлено', finished_at: now });
      return HttpResponse.json(state);
    }));
    try {
      await renderRoute('/chat/c_q1');
      await waitFor(() => { expect(screen.getByRole('button', { name: 'Активный датасет' })).toHaveTextContent('orders_2026'); });
      await userEvent.click(await screen.findByRole('button', { name: 'Остановить' }));
      expect(await screen.findByRole('alert')).toHaveTextContent('Получение ответа остановлено');
      expect(cancellations).toBe(1);
      await waitFor(() => { expect(reads).toBeGreaterThan(1); });
      expect(screen.getByRole('textbox', { name: 'Вопрос к данным' })).toBeEnabled();
      await ask('Следующий вопрос после подтверждённой отмены');
      await waitFor(() => { expect(stream.calls()).toBe(1); });
      expect(screen.getByText('Следующий вопрос после подтверждённой отмены')).toBeInTheDocument();
      const nextSignal = stream.signal();
      releaseHistory();
      await act(async () => { await historyGate; });
      expect(nextSignal?.aborted).toBe(false);
      expect(screen.getByRole('textbox', { name: 'Вопрос к данным' })).toBeDisabled();
    } finally {
      releaseHistory();
    }
  });

  it.each(['success', 'timeout'] as const)('accepts the next question when GET confirms %s before the old SSE finishes', async (status) => {
    const stream = liveStream();
    const saved = createDb().chats[0]!;
    saved.messages = [];
    server.use(http.post(api('/chats'), () => HttpResponse.json({ chat_id: saved.chat_id }, { status: 201 })),
      http.get(api('/chats/c_q1'), () => HttpResponse.json(saved)));
    const { client } = await renderRoute('/chat');
    await ask();
    await stream.ready();
    const firstSignal = stream.signal();
    const now = new Date().toISOString();
    const state: Schemas['ChatTurnState'] = { run_id: 'run_saved_before_sse', status: 'running', node: 'verify', message: 'Проверяю числа в ответе…', started_at: now, deadline_at: new Date(Date.now() + 30000).toISOString() };
    saved.messages.push({ message_id: stream.messageId(), role: 'user', text: question, created_at: now, turn: state });
    // Seed running history through HTTP, then let normal polling observe the outcome.
    await act(async () => { await client.invalidateQueries({ queryKey: keys.chat(saved.chat_id) }); });
    expect(screen.getByRole('textbox', { name: 'Вопрос к данным' })).toBeDisabled();
    Object.assign(state, { status, code: status === 'timeout' ? 'model_timeout' : null, message: status === 'timeout' ? 'Модель не ответила за отведённое время' : 'Ответ готов', finished_at: now, assistant_message_id: status === 'success' ? 'a_early' : null });
    if (status === 'success') saved.messages.push({ message_id: 'a_early', role: 'assistant', blocks: [{ type: 'text', markdown: 'Ответ сохранён до конца SSE' }], created_at: now });
    if (status === 'success') expect(await screen.findByText('Ответ сохранён до конца SSE', {}, { timeout: 3000 })).toBeInTheDocument();
    else expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toHaveTextContent('Модель не ответила за отведённое время');
    await ask('Второй вопрос при задержанном первом SSE');
    await waitFor(() => { expect(stream.calls()).toBe(2); });
    expect(firstSignal?.aborted).toBe(true);
    expect(stream.aborted()).toBe(false);
    expect(screen.getByText('Второй вопрос при задержанном первом SSE')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Вопрос к данным' })).toBeDisabled();
    expect(screen.getAllByText(question)).toHaveLength(1);
    stream.emit('status', { node: 'understand', text: 'Разбираю второй вопрос…' });
    expect(await screen.findByRole('status')).toHaveTextContent('Разбираю второй вопрос…');
  });
});

describe('chat answer blocks', () => {
  afterEach(() => { vi.restoreAllMocks(); });
  const resultApi = () => {
    server.use(
      http.get(api('/results/r_fixture'), () => HttpResponse.json(fixtureResultMeta)),
      http.get(api('/results/r_fixture/data'), () => HttpResponse.json(fixtureResult)),
    );
  };
  const savedChat = (blocks: Schemas['Block'][]) => {
    const saved = createDb().chats[0]!;
    saved.messages = [{ message_id: 'm_blocks', role: 'assistant', created_at: saved.created_at, blocks }];
    server.use(http.get(api('/chats/c_q1'), () => HttpResponse.json(saved)));
    return saved;
  };

  it('streams every agreed block type from the shared fixture once, with status and done', async () => {
    resultApi();
    const stream = liveStream();
    const saved = createDb().chats[0]!;
    server.use(http.get(api('/chats/c_q1'), () => HttpResponse.json(saved)));
    await renderRoute('/chat/c_q1');
    await ask();
    await stream.ready();
    stream.emit('status', fixtureEvent('status'));
    expect(await screen.findByRole('status')).toHaveTextContent('Считаю выручку…');
    const [text, ...rest] = chatFixture.blocks;
    stream.emit('block', text);
    expect(await screen.findByText('12%', { selector: 'strong' })).toBeInTheDocument();
    expect(screen.queryByText('Какую метрику считать продажами?')).not.toBeInTheDocument();
    for (const block of rest) stream.emit('block', block);
    expect(await screen.findByRole('img', { name: /Выручка по месяцам/ })).toBeInTheDocument();
    expect(await screen.findByRole('table', { name: 'Таблица результата' })).toBeInTheDocument();
    expect(screen.getByText('Какую метрику считать продажами?')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Сумма заказа' })).toBeDisabled();
    expect(screen.getByRole('link', { name: /Квартальная статистика/ })).toHaveAttribute('href', '/dashboards/d_fixture');
    expect(screen.getByRole('button', { name: 'Разбить по регионам' })).toBeDisabled();
    saved.messages = [
      { message_id: stream.messageId(), role: 'user', text: question, created_at: saved.created_at },
      { message_id: 'm_fixture', role: 'assistant', blocks: chatFixture.blocks, result_ids: ['r_fixture'], created_at: saved.created_at },
    ];
    stream.emit('done', fixtureEvent('done'));
    await waitFor(() => { expect(screen.getByRole('button', { name: 'Сумма заказа' })).toBeEnabled(); });
    expect(screen.getAllByText('Какую метрику считать продажами?')).toHaveLength(1);
    expect(screen.getAllByRole('link', { name: /Квартальная статистика/ })).toHaveLength(1);
    expect(screen.getAllByText('Результат расчёта r_fixture')).toHaveLength(1);
    expect(stream.calls()).toBe(1);
  });

  it('assembles the fixture stream from network chunks cut inside events and UTF-8 characters', async () => {
    resultApi();
    const stream = liveStream();
    await renderRoute('/chat/c_q1');
    await ask();
    await stream.ready();
    const frame = (event: string, data: unknown) => `event: ${event}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
    const blocks = chatFixture.blocks.filter((block) => block.type === 'text' || block.type === 'clarification' || block.type === 'suggestions');
    const body = encoder.encode(frame('status', fixtureEvent('status')) + blocks.map((block) => frame('block', block)).join(''));
    // Odd-sized slices land inside multi-byte Cyrillic characters, field names and CRLF pairs.
    for (let start = 0; start < body.length; start += 7) stream.raw(body.slice(start, start + 7));
    expect(await screen.findByText('12%', { selector: 'strong' })).toBeInTheDocument();
    expect(screen.getByText('Какую метрику считать продажами?')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Сравнить с прошлым годом' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Считаю выручку…');
  });

  it('sends a clarification option or a suggestion as the next question exactly once', async () => {
    resultApi();
    const saved = savedChat(chatFixture.blocks.filter((block) => block.type === 'clarification' || block.type === 'suggestions'));
    const stream = liveStream();
    await renderRoute('/chat/c_q1');
    const option = await screen.findByRole('button', { name: 'Количество заказов' });
    await waitFor(() => { expect(option).toBeEnabled(); });
    await userEvent.click(option);
    await stream.ready();
    expect(stream.calls()).toBe(1);
    expect(document.querySelector('[data-role="user"]')).toHaveTextContent('Количество заказов');
    expect(screen.getByRole('button', { name: 'Разбить по регионам' })).toBeDisabled();
    saved.messages.push(
      { message_id: stream.messageId(), role: 'user', text: 'Количество заказов', created_at: saved.created_at },
      { message_id: 'm_next', role: 'assistant', blocks: [{ type: 'suggestions', items: ['Разбить по регионам'] }], created_at: saved.created_at },
    );
    stream.emit('done', { message_id: 'm_next' });
    // The older answer's buttons stay inactive; only the newest answer takes a click.
    await waitFor(() => { expect(screen.getAllByRole('button', { name: 'Разбить по регионам' })[1]).toBeEnabled(); });
    expect(screen.getAllByRole('button', { name: 'Разбить по регионам' })[0]).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Количество заказов' })).toBeDisabled();
  });

  it('explains an unknown or broken block and still shows the rest of the answer', async () => {
    savedChat([
      { type: 'text', markdown: 'Перед незнакомым блоком' },
      { type: 'heatmap_3d', data: [] } as unknown as Schemas['Block'],
      { type: 'chart' } as unknown as Schemas['Block'],
      { type: 'suggestions', items: ['Дальше'] },
    ]);
    await renderRoute('/chat/c_q1');
    expect(await screen.findByText('Перед незнакомым блоком')).toBeInTheDocument();
    const unsupported = screen.getAllByText('Этот тип ответа пока не поддерживается');
    expect(unsupported).toHaveLength(2);
    expect(screen.getByText(/Блок «heatmap_3d»/)).toBeInTheDocument();
    expect(screen.getByText(/Блок «chart»/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Дальше' })).toBeInTheDocument();
  });

  it('draws a table block by result_id with its columns and row limit', async () => {
    resultApi();
    savedChat([{ type: 'table', result_id: 'r_fixture', columns: ['revenue'], limit: 2 }]);
    await renderRoute('/chat/c_q1');
    const table = await screen.findByRole('table', { name: 'Таблица результата' });
    expect(within(table).queryByText('month')).not.toBeInTheDocument();
    expect(within(table).getByText('revenue')).toBeInTheDocument();
    expect(within(table).getAllByRole('row')).toHaveLength(3);
    expect(screen.getByText('Показаны первые 2 из 3 строк.')).toBeInTheDocument();
  });

  it('shows ISO dates of a table block in the Russian format', async () => {
    resultApi();
    savedChat([{ type: 'table', result_id: 'r_fixture' }]);
    await renderRoute('/chat/c_q1');
    const table = await screen.findByRole('table', { name: 'Таблица результата' });
    expect(within(table).getByText('01.01.2026')).toBeInTheDocument();
    expect(screen.getByText('r_fixture · 3 строки')).toBeInTheDocument();
  });
});
