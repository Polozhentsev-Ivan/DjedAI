import { test, expect, type Page } from '@playwright/test';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

const root = fileURLToPath(new URL('../..', import.meta.url));
let backend: ChildProcess | undefined;
let directory: string;
let python: string;
const tokens = new WeakMap<Page, string>();

async function authenticate(page: Page) {
  const username = `browser-${randomUUID()}`;
  const password = 'browser-ownership-regression';
  expect((await page.request.post('/api/auth/register', { data: { username, password } })).status()).toBe(201);
  const login = await page.request.post('/api/auth/login', { data: { username, password } });
  expect(login.status()).toBe(200);
  const token = (await login.json() as { access_token: string }).access_token;
  tokens.set(page, token);
  await page.addInitScript((value) => { window.sessionStorage.setItem('djedai.access_token', value); }, token);
}

function apiGet(page: Page, url: string) {
  return page.request.get(url, { headers: { Authorization: `Bearer ${tokens.get(page) ?? ''}` } });
}

async function startBackend() {
  backend = spawn(python, ['tests/e2e/serve_cp1.py', '--data-dir', directory], { cwd: root, stdio: 'pipe' });
  const process = backend;
  let output = '';
  backend.stderr?.on('data', (chunk: Buffer) => { output += chunk.toString(); });
  await expect.poll(async () => {
    if (process.exitCode !== null || process.signalCode !== null) throw new Error(`Backend failed: ${output}`);
    try { return (await fetch('http://127.0.0.1:18080/health')).status; } catch { return 0; }
  }, { timeout: 20_000 }).toBe(200);
}

async function stopBackend() {
  const process = backend;
  if (!process || process.exitCode !== null || process.signalCode !== null) return;
  const exited = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => { process.kill('SIGKILL'); reject(new Error('Backend did not stop')); }, 10_000);
    process.once('exit', () => { clearTimeout(timer); resolve(); });
  });
  process.kill();
  await exited;
}

test.beforeAll(async () => {
  directory = await mkdtemp(path.join(tmpdir(), 'djedai-cp1-browser-'));
  python = execFileSync('uv', ['run', '--locked', 'python', '-c', 'import sys; print(sys.executable)'], { cwd: root, encoding: 'utf8' }).trim();
  await startBackend();
});
test.afterAll(async () => { await stopBackend(); });
test.beforeEach(async ({ page }, info) => { if (!info.title.startsWith('UI auth:')) await authenticate(page); });

async function upload(page: Page, name: string, body: string, status = 'ready', extra = false) {
  const files = { [name]: body };
  if (extra) files['customers.csv'] = 'id,name\n1,Alice\n2,Bob\n';
  return uploadFiles(page, files, status);
}

async function uploadFiles(page: Page, csv: Record<string, string>, status = 'ready') {
  await page.goto('/sources');
  const response = page.waitForResponse((r) => r.url().endsWith('/api/datasets/upload') && r.request().method() === 'POST');
  const files = Object.entries(csv).map(([name, body]) => ({ name, mimeType: 'text/csv', buffer: Buffer.from(body) }));
  await page.getByTestId('csv-input').setInputFiles(files);
  const accepted = await response;
  expect(accepted.status()).toBe(202);
  const { dataset_id: datasetId } = await accepted.json() as { dataset_id: string };
  await expect.poll(async () => {
    const value = await (await apiGet(page, `/api/datasets/${datasetId}`)).json() as { status: string };
    return value.status;
  }, { timeout: 15000 }).toBe(status);
  return datasetId;
}

test('CSV → real HTTP model/SQL → result and history survive backend restart', async ({ page }) => {
  // Deliberately independent CSV oracle, two datasets with different counts/sums.
  const first = await upload(page, 'orders.csv', 'id,amount\n1,12\n2,30\n3,8\n', 'ready', true);
  const detail = await (await apiGet(page, `/api/datasets/${first}`)).json() as { catalog: { tables: { table_id: string }[] } };
  expect(detail.catalog.tables).toHaveLength(2);
  await page.goto(`/catalog/${first}`);
  await expect(page.getByText('amount', { exact: true }).first()).toBeVisible();
  await page.goto('/chat');
  await page.getByRole('textbox', { name: 'Вопрос к данным' }).fill('Сколько строк и какова сумма amount?');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Строк: 3; сумма: 50.', { exact: true })).toHaveCount(1);
  const chatUrl = page.url();
  const saved = page.locator('details[data-result-id]').first();
  await expect(saved).toBeVisible();
  const resultId = await saved.getAttribute('data-result-id');
  expect(resultId).toBeTruthy();
  const metadata = await (await apiGet(page, `/api/results/${resultId}`)).json() as { sql: string; tool: string };
  expect(metadata.tool).toBe('run_sql');
  expect(metadata.sql).toContain(detail.catalog.tables[0]?.table_id);
  expect(await (await apiGet(page, `/api/results/${resultId}/data`)).json()).toMatchObject({ columns: ['n', 'total'], rows: [[3, 50]] });

  await stopBackend();
  await startBackend();
  await page.goto(chatUrl);
  await expect(page.getByText('Строк: 3; сумма: 50.', { exact: true })).toHaveCount(1);
  await expect(page.locator(`details[data-result-id="${resultId}"]`)).toBeVisible();
  expect(await (await apiGet(page, `/api/datasets/${first}`)).json()).toMatchObject({ status: 'ready' });
  expect(await (await apiGet(page, `/api/results/${resultId}/data`)).json()).toMatchObject({ rows: [[3, 50]] });

  const second = await upload(page, 'other.csv', 'id,amount\n1,7\n2,11\n');
  expect(second).not.toBe(first);
  // Sources' catalog action explicitly selects the new dataset.
  await page.goto(`/catalog/${second}`);
  await page.getByRole('button', { name: /чат/i }).first().click();
  await page.getByRole('textbox', { name: 'Вопрос к данным' }).fill('Сколько строк и какова сумма amount?');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Строк: 2; сумма: 18.', { exact: true })).toHaveCount(1);
  expect(await (await apiGet(page, '/api/results/missing')).json()).toMatchObject({ error: { code: 'not_found' } });
  expect(await page.evaluate<number>('navigator.serviceWorker.getRegistrations().then(items => items.length)')).toBe(0);
});

interface CatalogColumn { name: string; original_name: string; semantic_type: string; null_ratio: number; description: string }
/** The browser backend describes every catalog, so descriptions are always filled here. */
interface Catalog {
  summary: string;
  tables: { table_id: string; description: string; columns: CatalogColumn[] }[];
  relations: { from: { table: string; column: string }; to: { table: string; column: string }; overlap: number; cardinality: string }[];
  warnings: string[];
  transformations: { table: string; column: string | null; action: string; detail?: Record<string, unknown> }[];
}

const percent = (ratio: number) => `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(Math.round(ratio * 1000) / 10)}%`;

test('catalog screen matches the catalog API: fields, descriptions, journal and relations graph', async ({ page }) => {
  const id = await uploadFiles(page, {
    'orders.csv': 'order_id,customer_id,amount,Комментарий клиента\n1,1,12,\n2,2,30,позвонить\n3,1,8,\n4,3,5,\n',
    'customers.csv': 'id,name\n1,Alice\n2,Bob\n3,Eve\n',
  });
  const catalog = await (await apiGet(page, `/api/datasets/${id}/catalog`)).json() as Catalog;
  expect(catalog.transformations.map((t) => t.action)).toEqual(expect.arrayContaining(['rename_column', 'keep_text']));
  expect(catalog.relations.map((r) => `${r.from.table}.${r.from.column}->${r.to.table}.${r.to.column}`)).toContain('orders.customer_id->customers.id');
  await page.goto(`/catalog/${id}`);
  await expect(page.getByText(catalog.summary, { exact: true })).toBeVisible();

  for (const table of catalog.tables) {
    await page.getByRole('tab', { name: new RegExp(`^${table.table_id}`) }).click();
    await expect(page.getByText(table.description, { exact: true })).toBeVisible();
    const rows = page.getByRole('table', { name: `Структура таблицы ${table.table_id}` }).getByRole('row');
    await expect(rows).toHaveCount(table.columns.length + 1);
    for (const [index, column] of table.columns.entries()) {
      const cells = rows.nth(index + 1).getByRole('cell');
      await expect(cells.nth(0)).toHaveText(column.original_name);
      await expect(cells.nth(1)).toHaveText(column.name);
      await expect(cells.nth(2)).toContainText(column.semantic_type);
      await expect(cells.nth(3)).toHaveText(column.description);
      await expect(cells.nth(4)).toHaveText(percent(column.null_ratio));
    }
  }

  for (const warning of catalog.warnings) await expect(page.getByText(warning, { exact: true })).toBeVisible();
  // Every logged step is on screen in words: renames per table, the rest per column; ordinary text is not a step.
  const log = page.locator('section', { has: page.getByRole('heading', { name: 'Журнал обработки данных' }) });
  for (const t of catalog.transformations) {
    if (t.action === 'keep_text' && t.detail?.reason === 'text') continue;
    const prefix = t.action === 'rename_column' ? `${t.table}: ` : `${t.table}.${t.column ?? ''}: `;
    await expect(log.getByText(prefix).first()).toBeVisible();
    await expect(log).not.toContainText(`: ${t.action}`);
  }

  const graph = page.getByRole('group', { name: 'Граф связей таблиц' });
  await expect(graph.getByRole('button', { name: /^Таблица / })).toHaveCount(catalog.tables.length);
  await expect(graph.getByRole('img')).toHaveCount(catalog.relations.length);
  const bars = page.getByRole('progressbar', { name: 'Совпадение ключей' });
  await expect(bars).toHaveCount(catalog.relations.length);
  for (const [index, r] of catalog.relations.entries()) {
    const name = `${r.from.table}.${r.from.column} → ${r.to.table}.${r.to.column}: `;
    await expect(graph.getByRole('img', { name })).toContainText(`${r.cardinality} · ${percent(r.overlap)}`);
    await expect(bars.nth(index)).toHaveAttribute('aria-valuenow', String(Math.round(r.overlap * 1000) / 10));
  }
  await graph.getByRole('button', { name: /^Таблица customers/ }).click();
  await expect(page.getByRole('tab', { name: /^customers/ })).toHaveAttribute('aria-selected', 'true');
});

test('relations graph of the synthetic dataset matches the independent references', async ({ page }) => {
  // eval/synthetic_expectations.json is recomputed from the CSV by scripts/verify_synthetic_data.py, not by the app.
  type Ref = { from: { table: string; column: string }; to: { table: string; column: string } };
  const { relations } = JSON.parse(await readFile(path.join(root, 'eval/synthetic_expectations.json'), 'utf8')) as { relations: { expected: Ref[]; not_expected: Ref[] } };
  const names = ['sales', 'stores', 'products', 'campaigns'];
  const files = await Promise.all(names.map(async (n): Promise<[string, string]> => [`${n}.csv`, await readFile(path.join(root, `tests/fixtures/synthetic/${n}.csv`), 'utf8')]));
  const id = await uploadFiles(page, Object.fromEntries(files));
  await page.goto(`/catalog/${id}`);
  const graph = page.getByRole('group', { name: 'Граф связей таблиц' });
  // campaigns has no relations and still is a node of the graph.
  await expect(graph.getByRole('button', { name: /^Таблица / })).toHaveCount(names.length);
  await expect(graph.getByRole('img')).toHaveCount(relations.expected.length);
  const edge = (r: Ref) => graph.getByRole('img', { name: `${r.from.table}.${r.from.column} → ${r.to.table}.${r.to.column}: ` });
  for (const r of relations.expected) await expect(edge(r)).toContainText('N:1 · 100%');
  for (const r of relations.not_expected) await expect(edge(r)).toHaveCount(0);
});

test('catalog of a dataset without relations has no graph', async ({ page }) => {
  const id = await uploadFiles(page, { 'orders.csv': 'id,amount\n1,12\n2,30\n' });
  const catalog = await (await apiGet(page, `/api/datasets/${id}/catalog`)).json() as Catalog;
  expect(catalog.relations).toEqual([]);
  await page.goto(`/catalog/${id}`);
  await expect(page.getByText('Связи между таблицами не найдены')).toBeVisible();
  await expect(page.getByRole('group', { name: 'Граф связей таблиц' })).toHaveCount(0);
  await expect(page.getByRole('progressbar')).toHaveCount(0);
});

test('bad CSV and model failure terminate visibly; cancellation never retries POST', async ({ page }) => {
  const bad = await upload(page, 'empty.csv', '', 'error');
  expect(await (await apiGet(page, `/api/datasets/${bad}`)).json()).toMatchObject({ status: 'error' });
  await expect(page.getByText(/пуст/i).first()).toBeVisible();
  const dataset = await upload(page, 'failure.csv', 'id,amount\n1,20\n');
  await page.goto(`/catalog/${dataset}`);
  await page.getByRole('button', { name: /чат/i }).first().click();
  await page.getByRole('textbox', { name: 'Вопрос к данным' }).fill('Ошибка модели');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Не удалось проверить ответ модели', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeVisible();
  let posts = 0;
  page.on('request', (r) => { if (r.url().endsWith('/messages') && r.method() === 'POST') posts++; });
  await page.getByRole('textbox', { name: 'Вопрос к данным' }).fill('Медленно посчитай строки и сумму amount');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await page.getByRole('button', { name: 'Остановить', exact: true }).click();
  await expect(page.getByText('Получение ответа остановлено', { exact: true })).toBeVisible();
  // The controlled server pauses for one second; allow the pending call to finish.
  await new Promise((resolve) => setTimeout(resolve, 1500));
  expect(posts).toBe(1);
  await expect(page.getByText('Строк: 1; сумма: 20.', { exact: true })).toHaveCount(0);
});

async function controlModel(marker: string, stage: string, mode: string, release = false) {
  const response = await fetch('http://127.0.0.1:18081/control', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ marker, stage, mode, release }),
  });
  expect(response.status).toBe(200);
}

async function submitQuestion(page: Page, text: string) {
  const input = page.getByRole('textbox', { name: 'Вопрос к данным' });
  await expect(input).toBeEnabled();
  await input.fill(text);
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
}

test('turn status precedes controlled stage completion; retry pause resumes the same stage', async ({ page }, info) => {
  const marker = `stage-${randomUUID()}`;
  const stages = [
    ['Understanding', 'Разбираю вопрос…'], ['PlanResponse', 'Строю план анализа…'], ['ComposeResponse', 'Формулирую ответ…'], ['VerifyResponse', 'Проверяю числа в ответе…'],
  ] as const;
  // Verify intentionally receives a fixed prompt without the original question.
  const markerFor = (name: string) => name === 'VerifyResponse' ? '*' : marker;
  for (const [name] of stages) await controlModel(markerFor(name), name, 'gate');
  const dataset = await upload(page, 'stages.csv', 'id,amount\n1,12\n2,30\n');
  await page.goto(`/catalog/${dataset}`);
  await page.getByRole('button', { name: /чат/i }).first().click();
  await submitQuestion(page, `${marker}: строки и сумма amount`);
  for (const [name, text] of stages) {
    await expect(page.getByRole('status')).toHaveText(text);
    await expect.poll(async () => {
      const observations = await (await fetch('http://127.0.0.1:18081/control')).json() as Record<string, { started: number; finished: number }>;
      return observations[`${markerFor(name)}:${name}`];
    }).toEqual({ started: 1, finished: 0 });
    await expect(page.locator('[data-role="assistant"]')).toHaveCount(0);
    if (name === 'Understanding') await page.screenshot({ path: info.outputPath('turn-waiting.png') });
    await controlModel(markerFor(name), name, 'gate', true);
  }
  await expect(page.getByText('Строк: 2; сумма: 42.', { exact: true })).toHaveCount(1);
  const retryMarker = `retry-${randomUUID()}`;
  await controlModel(retryMarker, 'Understanding', 'retry');
  await submitQuestion(page, `${retryMarker}: строки и сумма amount`);
  await expect(page.getByRole('status')).toHaveText('Ожидаю повторного запроса к модели…');
  await expect(page.getByText('Строк: 2; сумма: 42.', { exact: true })).toHaveCount(2);
  const observations = await (await fetch('http://127.0.0.1:18081/control')).json() as Record<string, { started: number }>;
  expect(observations[`${retryMarker}:Understanding`].started).toBe(2);
});

test('separate identical questions retain timeout, unavailable and stop across reload, then a new question works', async ({ page }, info) => {
  const marker = `outcome-${randomUUID()}`;
  const question = `${marker}: строки и сумма amount`;
  const dataset = await upload(page, 'outcomes.csv', 'id,amount\n1,9\n2,11\n');
  await page.goto(`/catalog/${dataset}`);
  await page.getByRole('button', { name: /чат/i }).first().click();
  let posts = 0;
  page.on('request', (request) => { if (request.method() === 'POST' && request.url().endsWith('/messages')) posts++; });
  await controlModel(marker, 'Understanding', 'timeout');
  await submitQuestion(page, question);
  await expect(page.getByText('Модель не ответила за отведённое время', { exact: true })).toBeVisible({ timeout: 15000 });
  await controlModel(marker, 'Understanding', 'unavailable');
  await submitQuestion(page, question);
  await expect(page.getByText('Сервис модели временно недоступен', { exact: true })).toBeVisible();
  await controlModel(marker, 'Understanding', 'gate');
  await submitQuestion(page, question);
  await expect(page.getByRole('status')).toHaveText('Разбираю вопрос…');
  await page.getByRole('button', { name: 'Остановить', exact: true }).click();
  await expect(page.getByText('Получение ответа остановлено', { exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Вопрос к данным' })).toBeEnabled();
  await controlModel(marker, 'Understanding', 'gate', true);
  const chatId = new URL(page.url()).pathname.split('/').at(-1) ?? '';
  await page.reload();
  await expect(page.locator('[data-role="user"]')).toHaveCount(3);
  await expect(page.locator('[data-role="assistant"]')).toHaveCount(0);
  await expect(page.getByRole('alert')).toHaveCount(3);
  const saved = await (await apiGet(page, `/api/chats/${chatId}`)).json() as { messages: { message_id: string; turn: { code: string; run_id: string } }[] };
  expect(saved.messages.map((message) => message.turn.code)).toEqual(['model_timeout', 'model_unavailable', 'cancelled']);
  expect(new Set(saved.messages.map((message) => message.message_id)).size).toBe(3);
  expect(new Set(saved.messages.map((message) => message.turn.run_id)).size).toBe(3);
  expect(posts).toBe(3);
  await page.setViewportSize({ width: 1280, height: 1100 });
  await page.screenshot({ path: info.outputPath('turn-outcomes-light.png'), fullPage: true });
  await page.evaluate(() => { document.documentElement.classList.add('dark'); });
  await page.screenshot({ path: info.outputPath('turn-outcomes-dark.png'), fullPage: true });
  await submitQuestion(page, 'Новый вопрос: строки и сумма amount');
  await expect(page.getByText('Строк: 2; сумма: 20.', { exact: true })).toHaveCount(1);
  await expect(page.locator('[data-role="user"]')).toHaveCount(4);
  expect(posts).toBe(4);
});

test('reload disconnects the old turn and restores its reason without replaying POST', async ({ page }) => {
  const marker = `disconnect-${randomUUID()}`;
  await controlModel(marker, 'Understanding', 'gate');
  const dataset = await upload(page, 'disconnect.csv', 'id,amount\n1,8\n');
  await page.goto(`/catalog/${dataset}`);
  await page.getByRole('button', { name: /чат/i }).first().click();
  let posts = 0;
  page.on('request', (request) => { if (request.method() === 'POST' && request.url().endsWith('/messages')) posts++; });
  await submitQuestion(page, `${marker}: строки и сумма amount`);
  await expect(page.getByRole('status')).toHaveText('Разбираю вопрос…');
  await page.reload();
  await expect(page.getByText('Соединение прервано до конца ответа', { exact: true })).toBeVisible();
  await expect(page.locator('[data-role="user"]')).toHaveCount(1);
  await expect(page.locator('[data-role="assistant"]')).toHaveCount(0);
  expect(posts).toBe(1);
  await controlModel(marker, 'Understanding', 'gate', true);
  await submitQuestion(page, 'Посчитай строки и сумму amount');
  await expect(page.getByText('Строк: 1; сумма: 8.', { exact: true })).toHaveCount(1);
  expect(posts).toBe(2);
});

test('confirmed restored cancellation releases sending while history GET is held', async ({ page }) => {
  const firstMarker = `restored-${randomUUID()}`;
  const nextMarker = `after-cancel-${randomUUID()}`;
  await controlModel(firstMarker, 'Understanding', 'gate');
  await controlModel(nextMarker, 'Understanding', 'gate');
  const dataset = await upload(page, 'restored.csv', 'id,amount\n1,8\n');
  const headers = { Authorization: `Bearer ${tokens.get(page) ?? ''}` };
  const created = await page.request.post('/api/chats', { headers, data: { dataset_id: dataset } });
  expect(created.status()).toBe(201);
  const { chat_id: chatId } = await created.json() as { chat_id: string };
  // Another client owns the open stream; this page restores the running question by GET.
  const external = await fetch(`http://127.0.0.1:18080/api/chats/${chatId}/messages`, {
    method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: `${firstMarker}: строки и сумма amount`, request_id: randomUUID() }),
  });
  expect(external.status).toBe(200);
  let holdHistory = false;
  let heldReads = 0;
  let releaseHistory = () => { /* set below */ };
  const historyGate = new Promise<void>((resolve) => { releaseHistory = resolve; });
  await page.route(`**/api/chats/${chatId}`, async (route) => {
    if (holdHistory) { heldReads++; await historyGate; }
    await route.continue();
  });
  try {
    await page.goto(`/chat/${chatId}`);
    await expect(page.getByRole('button', { name: 'Активный датасет' })).toContainText('restored.csv');
    await expect(page.getByRole('status').filter({ hasText: 'Разбираю вопрос…' })).toHaveCount(1);
    holdHistory = true;
    const stopped = page.waitForResponse((response) => response.url().endsWith('/cancel'));
    await page.getByRole('button', { name: 'Остановить', exact: true }).click();
    expect(await (await stopped).json()).toMatchObject({ status: 'cancelled' });
    await expect.poll(() => heldReads).toBeGreaterThan(0);
    await expect(page.getByRole('textbox', { name: 'Вопрос к данным' })).toBeEnabled();
    const nextQuestion = `${nextMarker}: строки и сумма amount`;
    await submitQuestion(page, nextQuestion);
    await expect(page.getByRole('status')).toHaveText('Разбираю вопрос…');
    await expect(page.getByText(nextQuestion, { exact: true })).toHaveCount(1);
    releaseHistory();
    await page.unrouteAll({ behavior: 'wait' });
    await expect(page.getByRole('textbox', { name: 'Вопрос к данным' })).toBeDisabled();
    await controlModel(nextMarker, 'Understanding', 'gate', true);
    await expect(page.getByText('Строк: 1; сумма: 8.', { exact: true })).toHaveCount(1);
    await expect(page.locator('[data-role="user"]')).toHaveCount(2);
  } finally {
    releaseHistory();
    await controlModel(firstMarker, 'Understanding', 'gate', true);
    await controlModel(nextMarker, 'Understanding', 'gate', true);
    await external.body?.cancel();
    await page.unrouteAll({ behavior: 'wait' });
  }
});

test('persisted success closes a held SSE and accepts the next question exactly once', async ({ page }) => {
  const firstMarker = `early-success-${randomUUID()}`;
  const nextMarker = `after-success-${randomUUID()}`;
  await controlModel(firstMarker, 'Understanding', 'gate');
  await controlModel(nextMarker, 'Understanding', 'gate');
  const dataset = await upload(page, 'early-success.csv', 'id,amount\n1,12\n2,30\n');
  await page.goto(`/catalog/${dataset}`);
  await page.getByRole('button', { name: /чат/i }).first().click();
  let releaseSse = () => { /* set below */ };
  const sseGate = new Promise<void>((resolve) => { releaseSse = resolve; });
  let posts = 0;
  let firstBody: string | null = null;
  let firstFailed = false;
  let firstRouteFinished = false;
  let initialHistory = true;
  await page.route('**/api/chats/c_*', async (route) => {
    if (initialHistory && route.request().method() === 'GET') {
      initialHistory = false;
      const chatId = new URL(route.request().url()).pathname.split('/').at(-1) ?? '';
      await expect.poll(async () => {
        const saved = await (await apiGet(page, `/api/chats/${chatId}`)).json() as { messages: { turn?: { status: string } }[] };
        return saved.messages[0]?.turn?.status;
      }).toBe('running');
    }
    await route.continue();
  });
  const runningHistory = page.waitForResponse(async (response) => {
    if (response.request().method() !== 'GET' || !/\/api\/chats\/c_[^/]+$/.test(new URL(response.url()).pathname)) return false;
    const saved = await response.json() as { messages: { turn?: { status: string } }[] };
    return saved.messages[0]?.turn?.status === 'running';
  });
  page.on('requestfailed', (request) => {
    if (firstBody !== null && request.url().endsWith('/messages') && request.postData() === firstBody) firstFailed = true;
  });
  await page.route('**/api/chats/*/messages', async (route) => {
    if (++posts !== 1) { await route.continue(); return; }
    firstBody = route.request().postData();
    const response = await route.fetch(); // Real backend persists the answer before delivery.
    await sseGate;
    if (firstFailed) await route.abort();
    else await route.fulfill({ response });
    firstRouteFinished = true;
  });
  try {
    await submitQuestion(page, `${firstMarker}: строки и сумма amount`);
    await expect(page).toHaveURL(/\/chat\/c_/);
    const chatId = new URL(page.url()).pathname.split('/').at(-1) ?? '';
    await expect.poll(async () => {
      const saved = await (await apiGet(page, `/api/chats/${chatId}`)).json() as { messages: { turn?: { status: string } }[] };
      return saved.messages[0]?.turn?.status;
    }).toBe('running');
    await runningHistory; // Normal GET polling is active while the SSE is held.
    await controlModel(firstMarker, 'Understanding', 'gate', true);
    await expect(page.getByText('Строк: 2; сумма: 42.', { exact: true })).toHaveCount(1);
    await expect.poll(() => firstFailed).toBe(true);
    const nextQuestion = `${nextMarker}: строки и сумма amount`;
    await submitQuestion(page, nextQuestion);
    await expect.poll(() => posts).toBe(2);
    await expect(page.getByRole('status')).toHaveText('Разбираю вопрос…');
    releaseSse();
    await expect.poll(() => firstRouteFinished).toBe(true);
    await expect(page.getByRole('textbox', { name: 'Вопрос к данным' })).toBeDisabled();
    await expect(page.getByText(nextQuestion, { exact: true })).toHaveCount(1);
    await controlModel(nextMarker, 'Understanding', 'gate', true);
    await expect(page.getByText('Строк: 2; сумма: 42.', { exact: true })).toHaveCount(2);
    await expect(page.locator('[data-role="user"]')).toHaveCount(2);
    await expect(page.getByRole('alert')).toHaveCount(0);
    expect(posts).toBe(2);
  } finally {
    releaseSse();
    await controlModel(firstMarker, 'Understanding', 'gate', true);
    await controlModel(nextMarker, 'Understanding', 'gate', true);
    await page.unrouteAll({ behavior: 'wait' });
  }
});

test('production smoke observes completed streams and persisted question outcomes', ({ baseURL }, info) => {
  const output = execFileSync(process.execPath, ['frontend/scripts/smoke-cp1.mjs', '--url', baseURL ?? 'http://127.0.0.1:4173', '--output', info.outputPath('smoke')], {
    cwd: root, encoding: 'utf8', windowsHide: true, timeout: 45000,
    env: { ...process.env, DJEDAI_SMOKE_USERNAME: `smoke-${randomUUID()}`, DJEDAI_SMOKE_PASSWORD: 'browser-smoke-regression' },
  });
  expect(output).toContain('PASS:');
});

test('two browser sessions isolate same-named CSVs, chats, results and SSE', async ({ page, browser }) => {
  const context = await browser.newContext({ baseURL: 'http://127.0.0.1:4173' });
  const other = await context.newPage();
  try {
    await authenticate(other);
    const first = await upload(page, 'orders.csv', 'id,amount\n1,10\n2,15\n');
    const second = await upload(other, 'orders.csv', 'id,amount\n1,200\n2,300\n3,400\n');
    expect(second).not.toBe(first);
    const list = await (await apiGet(other, '/api/datasets')).json() as { dataset_id: string }[];
    expect(list.map((item) => item.dataset_id)).toEqual([second]);
    expect((await apiGet(other, `/api/datasets/${first}`)).status()).toBe(404);
    expect((await apiGet(other, `/api/datasets/${first}/catalog`)).status()).toBe(404);
    expect((await apiGet(other, `/api/datasets/${first}/events`)).status()).toBe(404);
    const runs: { page: Page; dataset: string; answer: string }[] = [
      { page, dataset: first, answer: 'Строк: 2; сумма: 25.' },
      { page: other, dataset: second, answer: 'Строк: 3; сумма: 900.' },
    ];
    const privateIds: { chat: string; result: string }[] = [];
    for (const run of runs) {
      await run.page.goto(`/catalog/${run.dataset}`);
      await run.page.getByRole('button', { name: /чат/i }).first().click();
      await run.page.getByRole('textbox', { name: 'Вопрос к данным' }).fill('Сколько строк и какова сумма amount?');
      await run.page.getByRole('button', { name: 'Отправить', exact: true }).click();
      await expect(run.page.getByText(run.answer, { exact: true })).toHaveCount(1);
      const result = await run.page.locator('details[data-result-id]').first().getAttribute('data-result-id');
      expect(result).toBeTruthy();
      privateIds.push({ chat: new URL(run.page.url()).pathname.split('/').at(-1) ?? '', result: result ?? '' });
    }
    const firstIds = privateIds[0];
    expect(firstIds).toBeDefined();
    for (const path of [`/api/chats/${firstIds.chat}`, `/api/results/${firstIds.result}`, `/api/results/${firstIds.result}/data`]) {
      const response = await apiGet(other, path);
      expect(response.status()).toBe(404);
      expect(response.headers()['cache-control']).toBe('no-store');
    }
    const forbiddenStream = await other.request.post(`/api/chats/${firstIds.chat}/messages`, {
      headers: { Authorization: `Bearer ${tokens.get(other) ?? ''}` }, data: { text: 'Чужой вопрос' },
    });
    expect(forbiddenStream.status()).toBe(404);
    expect(forbiddenStream.headers()['content-type']).not.toContain('text/event-stream');
  } finally { await context.close(); }
});

test('UI auth: registration, logout, account switching, reload and revoked session', async ({ page }) => {
  const firstUser = `ui-a-${randomUUID()}`;
  const secondUser = `ui-b-${randomUUID()}`;
  const password = 'browser-auth-check';
  async function fill(name: string, secret = password) {
    await page.getByLabel('Имя пользователя', { exact: true }).fill(name);
    await page.getByLabel('Пароль', { exact: true }).fill(secret);
  }
  async function registerUser(name: string) {
    await page.getByRole('link', { name: 'Зарегистрироваться', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Зарегистрироваться', exact: true })).toBeVisible();
    await fill(name);
    const loginResponse = page.waitForResponse((r) => r.url().endsWith('/api/auth/login'));
    await page.getByRole('button', { name: 'Зарегистрироваться', exact: true }).click();
    const token = (await (await loginResponse).json() as { access_token: string }).access_token;
    tokens.set(page, token);
    await expect(page.getByText(name, { exact: true })).toBeVisible();
    return token;
  }
  async function signOut() {
    const response = page.waitForResponse((r) => r.url().endsWith('/api/auth/logout'));
    await page.getByRole('button', { name: 'Выйти', exact: true }).click();
    await expect(page).toHaveURL(/\/login$/);
    expect((await response).status()).toBe(204);
    expect(await page.evaluate(() => window.sessionStorage.getItem('djedai.access_token'))).toBeNull();
  }
  await page.goto('/sources');
  await expect(page).toHaveURL(/\/login$/);
  const firstToken = await registerUser(firstUser);
  const datasetId = await upload(page, 'private-a.csv', 'id,amount\n1,15\n2,35\n');
  await page.goto(`/catalog/${datasetId}`);
  await page.getByRole('button', { name: /чат/i }).first().click();
  await page.getByRole('textbox', { name: 'Вопрос к данным' }).fill('Медленно посчитай строки и сумму amount');
  const sending = page.waitForRequest((r) => r.url().endsWith('/messages') && r.method() === 'POST');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await sending;
  await signOut();
  expect((await page.request.get('/api/auth/me', { headers: { Authorization: `Bearer ${firstToken}` } })).status()).toBe(401);
  await registerUser(secondUser);
  await expect(page.getByText('У вас пока нет загруженных данных', { exact: true })).toBeVisible();
  await expect(page.getByText('private-a', { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText(secondUser, { exact: true })).toBeVisible();
  await expect(page.getByText('У вас пока нет загруженных данных', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.localStorage.getItem('djedai.access_token'))).toBeNull();
  await signOut();
  await fill(firstUser, 'wrong-password');
  await page.getByRole('button', { name: 'Войти в систему', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Неверный логин или пароль');
  await fill(firstUser);
  const loginResponse = page.waitForResponse((r) => r.url().endsWith('/api/auth/login'));
  await page.getByRole('button', { name: 'Войти в систему', exact: true }).click();
  const token = (await (await loginResponse).json() as { access_token: string }).access_token;
  await expect(page.getByText(firstUser, { exact: true })).toBeVisible();
  await expect(page.getByRole('table', { name: 'Ваши датасеты' })).toContainText('private-a');
  expect((await page.request.post('/api/auth/logout', { headers: { Authorization: `Bearer ${token}` } })).status()).toBe(204);
  await page.reload();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('status')).toContainText('Сессия истекла');
});
