import { expect, test, type Page } from '@playwright/test';
import type { Schemas } from '../src/api/client';
import { chatFixture, fixtureEvent, fixtureResult, fixtureResultMeta } from '../src/test/chatFixture';

const created = '2026-10-25T12:00:00Z';
const question = 'Как менялась выручка по месяцам?';

interface StreamHarness {
  posts: number;
  requestId: string | null;
  texts: string[];
  controller: ReadableStreamDefaultController<Uint8Array> | null;
}

declare global {
  interface Window { __chatStream: StreamHarness }
}

/**
 * The production build talks to a faked API. `route.fulfill` can only send a whole body, so POST
 * /messages is answered from the page with a stream the test feeds one event at a time.
 */
async function chatApi(page: Page, messages: () => Schemas['ChatMessage'][]) {
  await page.addInitScript(() => {
    sessionStorage.setItem('djedai.access_token', 'chat-blocks-session');
    if (!localStorage.getItem('djedai.theme')) localStorage.setItem('djedai.theme', 'light');
    const harness: StreamHarness = { posts: 0, requestId: null, texts: [], controller: null };
    window.__chatStream = harness;
    const realFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
      if (init?.method !== 'POST' || !/^\/api\/chats\/[^/]+\/messages$/.test(url.pathname)) return realFetch(input, init);
      // streamEvents always sends its JSON body as a string.
      const body = JSON.parse(init.body as string) as Schemas['SendMessageRequest'];
      harness.posts += 1;
      harness.requestId = body.request_id ?? null;
      harness.texts.push(body.text);
      const stream = new ReadableStream<Uint8Array>({ start(controller) { harness.controller = controller; } });
      return Promise.resolve(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }));
    };
  });
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    expect(route.request().headers().authorization).toBe('Bearer chat-blocks-session');
    const chat: Schemas['ChatDetail'] = { chat_id: 'c_blocks', dataset_id: 'ds_blocks', title: 'Блоки ответа', created_at: created, messages: messages() };
    let body: unknown;
    if (path === '/api/auth/me') body = { user_id: 'u_blocks', username: 'Проверка чата', created_at: created } satisfies Schemas['UserRead'];
    else if (path === '/api/datasets') body = [{ dataset_id: 'ds_blocks', name: 'Продажи', status: 'ready', source: { kind: 'csv_upload' }, table_count: 1, row_count: 120_000, created_at: created }] satisfies Schemas['DatasetSummary'][];
    else if (path === '/api/chats') body = [chat];
    else if (path === '/api/chats/c_blocks') body = chat;
    else if (path === '/api/dashboards') body = [];
    else if (path === '/api/results/r_fixture') body = fixtureResultMeta;
    else if (path === '/api/results/r_fixture/data') body = fixtureResult;
    // The dashboard_link fixture points at no real dashboard; the working screen arrives with B4-7.
    else if (path.startsWith('/api/dashboards/')) { await route.fulfill({ status: 404, json: { error: { code: 'not_found', message: 'Дашборд не найден' } } }); return; }
    else throw new Error(`Unexpected fixture request: ${path}`);
    await route.fulfill({ json: body });
  });
}

async function emit(page: Page, event: string, data: unknown) {
  await page.evaluate(([name, payload]) => {
    window.__chatStream.controller?.enqueue(new TextEncoder().encode(`event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`));
  }, [event, data] as const);
}

/** The chat scrolls inside its own panel, so a full-page capture needs a window as tall as the conversation. */
async function fitConversation(page: Page) {
  const extra = await page.locator('[aria-live="polite"]').evaluate((panel) => panel.scrollHeight - panel.clientHeight);
  const viewport = page.viewportSize() ?? { width: 1280, height: 900 };
  await page.setViewportSize({ width: viewport.width, height: viewport.height + Math.max(0, extra) });
}

const harness = (page: Page) => page.evaluate(() => ({ posts: window.__chatStream.posts, requestId: window.__chatStream.requestId, texts: window.__chatStream.texts }));

async function ask(page: Page, text: string) {
  const input = page.getByRole('textbox', { name: 'Вопрос к данным' });
  await expect(input).toBeEnabled();
  await input.fill(text);
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect.poll(async () => (await harness(page)).texts.at(-1)).toBe(text);
}

const userMessage = (requestId: string | null, text: string): Schemas['ChatMessage'] => ({
  message_id: `m_${(requestId ?? '').replaceAll('-', '')}`, role: 'user', text, created_at: created,
});

test('streams every agreed block, its status and done from the shared fixture', async ({ page }, info) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  let history: Schemas['ChatMessage'][] = [];
  await chatApi(page, () => history);
  await page.goto('/chat/c_blocks');
  await ask(page, question);

  await emit(page, 'status', fixtureEvent('status'));
  await expect(page.getByRole('status')).toHaveText('Считаю выручку…');
  const [text, chart, ...rest] = chatFixture.blocks;
  await emit(page, 'block', text);
  await emit(page, 'block', chart);
  await expect(page.locator('strong', { hasText: '12%' })).toBeVisible();
  const lineChart = page.getByRole('img', { name: /Выручка по месяцам/ });
  await expect.poll(() => lineChart.locator('svg path').count()).toBeGreaterThan(0);
  await expect(page.getByRole('button', { name: 'Остановить' })).toBeVisible();
  await page.screenshot({ path: info.outputPath('chat-streaming-status.png') });

  for (const block of rest) await emit(page, 'block', block);
  await expect(page.getByRole('table', { name: 'Таблица результата' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Сумма заказа' })).toBeDisabled();
  const { requestId } = await harness(page);
  history = [userMessage(requestId, question), { message_id: 'm_fixture', role: 'assistant', blocks: chatFixture.blocks, result_ids: ['r_fixture'], created_at: created }];
  await emit(page, 'done', fixtureEvent('done'));
  await expect(page.getByRole('button', { name: 'Остановить' })).toBeHidden();
  await expect(page.getByRole('button', { name: 'Сумма заказа' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Разбить по регионам' })).toBeEnabled();
  await expect(page.getByRole('link', { name: /Квартальная статистика/ })).toHaveAttribute('href', '/dashboards/d_fixture');
  // Saved history replaces the streamed blocks without showing any of them twice.
  await expect(page.getByText('Какую метрику считать продажами?')).toHaveCount(1);
  await expect(page.getByRole('table', { name: 'Таблица результата' })).toHaveCount(1);
  expect((await harness(page)).posts).toBe(1);
  await page.mouse.move(0, 0);
  await fitConversation(page);
  await page.screenshot({ path: info.outputPath('chat-all-blocks-light.png') });

  await page.getByRole('button', { name: 'Включить тёмную тему' }).click();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await page.screenshot({ path: info.outputPath('chat-all-blocks-dark.png') });
  await page.getByRole('button', { name: 'Включить светлую тему' }).click();

  await page.getByRole('button', { name: 'Количество заказов' }).click();
  await expect.poll(async () => (await harness(page)).posts).toBe(2);
  expect((await harness(page)).texts.at(-1)).toBe('Количество заказов');
  await expect(page.getByRole('button', { name: 'Сумма заказа' })).toBeDisabled();
});

test('explains an unknown block and links the dashboard fixture', async ({ page }, info) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const dashboard = chatFixture.blocks.find((block) => block.type === 'dashboard_link');
  await chatApi(page, () => [{ message_id: 'm_unknown', role: 'assistant', created_at: created, blocks: [
    { type: 'text', markdown: 'Собрал дашборд по кварталам. Один из блоков этой версии интерфейса незнаком:' },
    { type: 'sankey', nodes: [] } as unknown as Schemas['Block'],
    ...(dashboard ? [dashboard] : []),
  ] }]);
  await page.goto('/chat/c_blocks');
  const answer = page.locator('[data-role="assistant"]');
  await expect(answer.getByText('Этот тип ответа пока не поддерживается')).toBeVisible();
  await expect(answer.getByText(/Блок «sankey»/)).toBeVisible();
  await answer.screenshot({ path: info.outputPath('chat-unknown-block.png') });
  await answer.getByRole('link', { name: /Квартальная статистика/ }).click();
  await expect(page).toHaveURL(/\/dashboards\/d_fixture$/);
});

test('shows a stream error and does not post again after a disconnect', async ({ page }, info) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await chatApi(page, () => []);
  await page.goto('/chat/c_blocks');
  await ask(page, question);
  await emit(page, 'status', { node: 'understand', text: 'Разбираю вопрос…' });
  await expect(page.getByRole('status')).toHaveText('Разбираю вопрос…');
  await emit(page, 'error', { ...fixtureEvent('error'), code: 'model_timeout' });
  const error = page.getByRole('alert').filter({ hasText: 'Не удалось получить ответ агента' });
  await expect(error).toContainText('Модель не ответила за 15 секунд');
  await expect(error.getByRole('button', { name: 'Повторить вопрос' })).toBeVisible();
  await page.mouse.move(0, 0);
  await page.screenshot({ path: info.outputPath('chat-stream-error.png') });

  await ask(page, 'А по регионам?');
  await emit(page, 'status', { node: 'plan', text: 'Составляю план' });
  await page.evaluate(() => { window.__chatStream.controller?.close(); });
  await expect(page.getByRole('alert').filter({ hasText: /соединени/i })).toBeVisible();
  await page.waitForTimeout(500);
  expect((await harness(page)).posts).toBe(2);
});
