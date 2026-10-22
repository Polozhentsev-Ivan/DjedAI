import { test, expect, type Page } from '@playwright/test';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { mkdtemp } from 'node:fs/promises';
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
test.beforeEach(async ({ page }) => { await authenticate(page); });

async function upload(page: Page, name: string, body: string, status = 'ready', extra = false) {
  await page.goto('/sources');
  const response = page.waitForResponse((r) => r.url().endsWith('/api/datasets/upload') && r.request().method() === 'POST');
  const files = [{ name, mimeType: 'text/csv', buffer: Buffer.from(body) }];
  if (extra) files.push({ name: 'customers.csv', mimeType: 'text/csv', buffer: Buffer.from('id,name\n1,Alice\n2,Bob\n') });
  await page.getByTestId('csv-input').setInputFiles(files);
  const accepted = await response;
  expect(accepted.status()).toBe(202);
  const { dataset_id: datasetId } = await accepted.json() as { dataset_id: string };
  await expect.poll(async () => {
    const value = await (await apiGet(page, `/api/datasets/${datasetId}`)).json() as { status: string };
    return value.status;
  }).toBe(status);
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

test('bad CSV and model failure terminate visibly; cancellation never retries POST', async ({ page }) => {
  const bad = await upload(page, 'empty.csv', '', 'error');
  expect(await (await apiGet(page, `/api/datasets/${bad}`)).json()).toMatchObject({ status: 'error' });
  await expect(page.getByText(/пуст/i).first()).toBeVisible();
  const dataset = await upload(page, 'failure.csv', 'id,amount\n1,20\n');
  await page.goto(`/catalog/${dataset}`);
  await page.getByRole('button', { name: /чат/i }).first().click();
  await page.getByRole('textbox', { name: 'Вопрос к данным' }).fill('Ошибка модели');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Не удалось получить ответ агента', { exact: true })).toBeVisible();
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
