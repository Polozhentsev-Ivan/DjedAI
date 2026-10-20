import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw/http';
import type { Schemas } from '@/api/client';
import { createDb } from '@/mocks/db';
import { renderRoute } from '@/test/renderRoute';
import { server, useScenario } from '@/test/server';

const api = (path: string) => `http://localhost:3000/api${path}`;
const answer = 'Выручка стабильно растёт: пик в июле — **3,4 млн ₽**.';
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
  const realFetch = globalThis.fetch;
  vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (url.endsWith('/messages')) signal = init?.signal ?? undefined;
    return realFetch(input, init);
  });
  server.use(http.post(api('/chats/:id/messages'), () => {
    calls += 1;
    return new HttpResponse(new ReadableStream<Uint8Array>({ start(controller) { stream = controller; } }), { headers: { 'Content-Type': 'text/event-stream' } });
  }));
  return {
    ready: () => waitFor(() => { expect(stream).toBeDefined(); }),
    emit: (event: string, data: unknown) => { act(() => { stream?.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)); }); },
    close: () => { act(() => { stream?.close(); }); },
    aborted: () => signal?.aborted,
    calls: () => calls,
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
    expect(await screen.findByText(answer)).toBeInTheDocument();
    await waitFor(() => { expect(screen.queryByRole('button', { name: 'Остановить' })).not.toBeInTheDocument(); });
    expect(screen.getAllByText(answer)).toHaveLength(1);
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
      { message_id: 'u_42', role: 'user', text: question, created_at: saved.created_at },
      { message_id: 'm_42', role: 'assistant', blocks: [{ type: 'text', markdown: 'Всего 42 строки' }], created_at: saved.created_at },
    ];
    stream.emit('done', { message_id: 'm_42' });
    await waitFor(() => { expect(screen.queryByRole('button', { name: 'Остановить' })).not.toBeInTheDocument(); });
    expect(screen.getAllByText('Всего 42 строки')).toHaveLength(1);
    expect(document.querySelectorAll('[data-role="user"]')).toHaveLength(1);
  });

  it('stops the POST stream without repeating it', async () => {
    const stream = liveStream();
    await renderRoute('/chat/c_q1');
    await ask();
    await stream.ready();
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
    expect(await screen.findByRole('alert')).toHaveTextContent('Запрос превысил таймаут 30 сек.');
    expect(screen.getByRole('alert')).toHaveTextContent('Попробуйте уточнить период.');
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
    expect(screen.getByRole('link', { name: 'Сохранённый дашборд' })).toHaveAttribute('href', '/dashboards/d_q');
  });
});
