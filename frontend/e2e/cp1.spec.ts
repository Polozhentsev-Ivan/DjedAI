import { test, expect, type Page } from '@playwright/test';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../..', import.meta.url));
let backend: ChildProcess | undefined;
let directory: string;
let python: string;

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
    const value = await (await page.request.get(`/api/datasets/${datasetId}`)).json() as { status: string };
    return value.status;
  }).toBe(status);
  return datasetId;
}

test('CSV → real HTTP model/SQL → result and history survive backend restart', async ({ page }) => {
  // Deliberately independent CSV oracle, two datasets with different counts/sums.
  const first = await upload(page, 'orders.csv', 'id,amount\n1,12\n2,30\n3,8\n', 'ready', true);
  const detail = await (await page.request.get(`/api/datasets/${first}`)).json() as { catalog: { tables: { table_id: string }[] } };
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
  const metadata = await (await page.request.get(`/api/results/${resultId}`)).json() as { sql: string; tool: string };
  expect(metadata.tool).toBe('run_sql');
  expect(metadata.sql).toContain(detail.catalog.tables[0]?.table_id);
  expect(await (await page.request.get(`/api/results/${resultId}/data`)).json()).toMatchObject({ columns: ['n', 'total'], rows: [[3, 50]] });

  await stopBackend();
  await startBackend();
  await page.goto(chatUrl);
  await expect(page.getByText('Строк: 3; сумма: 50.', { exact: true })).toHaveCount(1);
  await expect(page.locator(`details[data-result-id="${resultId}"]`)).toBeVisible();
  expect(await (await page.request.get(`/api/datasets/${first}`)).json()).toMatchObject({ status: 'ready' });
  expect(await (await page.request.get(`/api/results/${resultId}/data`)).json()).toMatchObject({ rows: [[3, 50]] });

  const second = await upload(page, 'other.csv', 'id,amount\n1,7\n2,11\n');
  expect(second).not.toBe(first);
  // Sources' catalog action explicitly selects the new dataset.
  await page.goto(`/catalog/${second}`);
  await page.getByRole('button', { name: /чат/i }).first().click();
  await page.getByRole('textbox', { name: 'Вопрос к данным' }).fill('Сколько строк и какова сумма amount?');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Строк: 2; сумма: 18.', { exact: true })).toHaveCount(1);
  expect(await (await page.request.get('/api/results/missing')).json()).toMatchObject({ error: { code: 'not_found' } });
  expect(await page.evaluate<number>('navigator.serviceWorker.getRegistrations().then(items => items.length)')).toBe(0);
});

test('bad CSV and model failure terminate visibly; cancellation never retries POST', async ({ page }) => {
  const bad = await upload(page, 'empty.csv', '', 'error');
  expect(await (await page.request.get(`/api/datasets/${bad}`)).json()).toMatchObject({ status: 'error' });
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
