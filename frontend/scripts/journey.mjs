/** #175: production UI/API/storage/source, with only the model controlled.
 * Invoked by scripts/test-journey.sh; no route interception or runtime injection.
 */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { chromium, expect as baseExpect } from '@playwright/test';

const expect = baseExpect.configure({ timeout: 30_000 });

const { values } = parseArgs({ options: {
  url: { type: 'string' }, 'model-url': { type: 'string' }, output: { type: 'string' },
  phase: { type: 'string' },
} });
assert(values.url && values['model-url'] && values.output);
assert(['prepare', 'verify', 'partial'].includes(values.phase));
const root = fileURLToPath(new URL('../..', import.meta.url));
const output = path.resolve(values.output);
await mkdir(output, { recursive: true });
const reportPath = path.join(output, 'journey-report.json');
const sha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
const dirty = execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' }).trim();
const password = process.env.JOURNEY_PASSWORD;
assert(password, 'JOURNEY_PASSWORD is required; do not put it into the report');
const report = values.phase === 'prepare' ? {
  application_sha: sha, dirty_worktree: Boolean(dirty), started_at: new Date().toISOString(),
  scope: 'production_transport_with_controlled_model_not_live_model_quality',
  status: 'running', checks: [], sources: [], username: `journey-${Date.now()}`,
} : JSON.parse(await readFile(reportPath, 'utf8'));
assert.equal(report.application_sha, sha, 'Cannot combine phases from different revisions');
if (values.phase === 'verify') assert.equal(report.status, 'prepared');
if (values.phase === 'partial') assert.equal(report.status, 'restart_verified');
const browser = await chromium.launch();
const context = await browser.newContext({ baseURL: values.url, viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(30_000);
let token;
let posts = 0;
page.on('request', (request) => {
  if (request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/messages')) posts++;
});

async function api(url, options = {}) {
  const response = await page.request.fetch(url, { ...options, headers: { Authorization: `Bearer ${token}` } });
  assert.equal(response.status(), options.expectedStatus ?? 200, `${options.method ?? 'GET'} ${url}: ${await response.text()}`);
  return response;
}
async function json(url) { return (await api(url)).json(); }
async function model(pathname, data) {
  const response = await page.request.fetch(`${values['model-url']}${pathname}`, data ? { method: 'POST', data } : {});
  assert.equal(response.status(), 200);
  return response.json();
}
async function login(username, register = false) {
  await page.goto('/login');
  if (register) await page.getByRole('link', { name: 'Зарегистрироваться', exact: true }).click();
  await expect(page).toHaveURL(register ? /\/register$/ : /\/login$/);
  const submit = page.getByRole('button', { name: register ? 'Зарегистрироваться' : 'Войти в систему', exact: true });
  await expect(submit).toBeVisible();
  await page.getByLabel('Имя пользователя', { exact: true }).fill(username);
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  const response = page.waitForResponse((r) => r.url().endsWith('/api/auth/login'));
  await submit.click();
  const received = await response;
  assert.equal(received.status(), 200);
  token = (await received.json()).access_token;
  await expect(page.getByText(username, { exact: true })).toBeVisible();
}
async function logout() {
  const response = page.waitForResponse((r) => r.url().endsWith('/api/auth/logout'));
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  assert.equal((await response).status(), 204);
  await expect(page).toHaveURL(/\/login$/);
  assert.equal(await page.evaluate(() => sessionStorage.getItem('djedai.access_token')), null);
}
async function source(kind, otherOwner = false) {
  await page.goto('/sources');
  const accepted = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().endsWith(kind === 'csv' ? '/datasets/upload' : '/datasets/connect'));
  if (kind === 'csv') {
    const orders = await readFile(path.join(root, 'tests/fixtures/journey/orders.csv'), 'utf8');
    await page.getByTestId('csv-input').setInputFiles([
      { name: 'orders.csv', mimeType: 'text/csv', buffer: Buffer.from(otherOwner ? orders.replace(',12\n', ',112\n') : orders) },
      { name: 'targets.csv', mimeType: 'text/csv', buffer: await readFile(path.join(root, 'tests/fixtures/journey/targets.csv')) },
    ]);
  } else {
    await page.getByRole('tab', { name: 'Подключить БД', exact: true }).click();
    await page.getByLabel('Строка подключения (DSN)', { exact: true }).fill('postgresql://journey_reader:journey-test-only@journey-postgres:5432/journey');
    await page.getByRole('button', { name: 'Проверить соединение', exact: true }).click();
    await expect(page.getByText(/Соединение установлено:/)).toBeVisible();
    await page.getByRole('button', { name: /^public\.orders\b/ }).click();
    await page.getByRole('button', { name: /^public\.targets\b/ }).click();
    await page.getByRole('button', { name: 'Подключить', exact: true }).click();
  }
  const response = await accepted;
  assert.equal(response.status(), 202, await response.text());
  const { dataset_id: id } = await response.json();
  await expect.poll(async () => (await json(`/api/datasets/${id}`)).status, { timeout: 120_000 }).toBe('ready');
  await page.goto(`/catalog/${id}`);
  await page.getByRole('button', { name: /чат/i }).first().click();
  return id;
}
const categoryQuestion = 'Покажи столбчатый график суммы amount по category за январь 2025.';
async function ask(question) {
  const before = await page.locator('[data-role="assistant"]').count();
  const verifiesBefore = (await model('/observations')).calls.filter((c) => c.stage === 'VerifyResponse' && c.finished).length;
  await page.getByRole('textbox', { name: 'Вопрос к данным' }).fill(question);
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.locator('[data-role="assistant"]')).toHaveCount(before + 1, { timeout: 90_000 });
  await expect(page.getByRole('textbox', { name: 'Вопрос к данным' })).toBeEnabled();
  const chatId = new URL(page.url()).pathname.split('/').at(-1);
  const chat = await json(`/api/chats/${chatId}`);
  const message = chat.messages.filter((m) => m.role === 'assistant').at(-1);
  assert(!message.blocks.some((b) => b.type === 'error'), JSON.stringify(message.blocks));
  const chart = message.blocks.find((b) => b.type === 'chart')?.chart;
  assert(chart, JSON.stringify(message.blocks));
  await expect(page.locator('[data-role="assistant"]').last().locator('[role="img"]')).toBeVisible();
  assert.equal(chart.y[0].field, 'total');
  assert((await model('/observations')).calls.filter((c) => c.stage === 'VerifyResponse' && c.finished).length > verifiesBefore);
  return { chatId, chart };
}
async function result(id, rows) {
  const data = await json(`/api/results/${id}/data`);
  assert.deepEqual(data.rows.map((r) => [r[0], Number(r[1])]), rows);
  const meta = await json(`/api/results/${id}`);
  assert.equal(meta.tool, 'run_sql');
  assert(meta.sql && meta.read, 'SQL and read provenance are required');
  return meta;
}
async function save(chart, dashboardId) {
  const card = page.locator('[data-role="assistant"]').last().locator('section').filter({ has: page.getByRole('button', { name: 'В дашборд', exact: true }) });
  await card.getByRole('button', { name: 'Как посчитано', exact: true }).click();
  await expect(card.locator('pre')).toContainText('SELECT');
  await card.getByRole('button', { name: 'В дашборд', exact: true }).click();
  const response = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().endsWith('/api/dashboards'));
  await page.getByRole('menuitem', { name: dashboardId ? 'Сумма по группам' : /Новый дашборд/ }).click();
  const saved = await response;
  assert.equal(saved.status(), 200, await saved.text());
  const dashboard = await saved.json();
  assert(dashboard.widgets.some((w) => w.result_id === chart.result_id));
  if (dashboardId) assert.equal(dashboard.dashboard_id, dashboardId);
  return dashboard;
}
async function refresh(id) {
  const response = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().endsWith(`/dashboards/${id}/refresh`));
  await page.getByRole('button', { name: 'Обновить данные', exact: true }).click();
  const received = await response;
  assert.equal(received.status(), 200, await received.text());
  await expect(page.getByRole('button', { name: 'Обновить данные', exact: true })).toBeEnabled();
  return received.json();
}
function check(name) {
  report.checks.push({ name, phase: values.phase, passed: true });
  console.log(`PASS ${name}`);
}

try {
  await login(report.username, values.phase === 'prepare');
  assert.equal(await page.evaluate(() => navigator.serviceWorker.getRegistrations().then((r) => r.length)), 0);
  if (values.phase === 'prepare') {
    for (const kind of ['csv', 'postgres']) {
      const datasetId = await source(kind);
      const first = await ask(categoryQuestion);
      const meta = await result(first.chart.result_id, [['A', 42], ['B', 8]]);
      assert.equal(meta.read.dataset_id, datasetId);
      check(`${kind}: source UI, independent January totals, SQL and provenance`);
      const second = await ask('А теперь то же самое по регионам.');
      assert.equal(second.chatId, first.chatId);
      assert.notEqual(second.chart.result_id, first.chart.result_id);
      const regionMeta = await result(second.chart.result_id, [['North', 20], ['South', 30]]);
      assert.match(regionMeta.sql, /2025-01-01/);
      assert.match(regionMeta.sql, /2025-02-01/);
      assert.equal(second.chart.x.field, 'region');
      check(`${kind}: follow-up retains January and groups by region`);
      const toolsBefore = (await model('/observations')).calls.reduce((sum, c) => sum + (c.tool_calls ?? 0), 0);
      const third = await ask('Покажи предыдущий результат линейным графиком.');
      assert.equal(third.chart.result_id, second.chart.result_id);
      assert.equal(third.chart.type, 'line');
      assert.equal((await model('/observations')).calls.reduce((sum, c) => sum + (c.tool_calls ?? 0), 0), toolsBefore);
      // An empty plan may still be requested; unchanged persisted result ID proves reuse.
      let dashboard = await save(third.chart);
      const target = await ask('Покажи сумму target по region из targets столбчатым графиком.');
      await result(target.chart.result_id, [['North', 100], ['South', 200]]);
      dashboard = await save(target.chart, dashboard.dashboard_id);
      assert.equal(dashboard.widgets.length, 2);
      await page.goto(`/dashboards/${dashboard.dashboard_id}`);
      await page.reload();
      await expect(page.getByRole('heading', { name: 'Дашборд: Сумма по группам' })).toBeVisible();
      await expect(page.locator('[role="img"]')).toHaveCount(2);
      assert.deepEqual(await json(`/api/dashboards/${dashboard.dashboard_id}`), dashboard);
      await page.screenshot({ path: path.join(output, `${kind}-saved.png`), fullPage: true });
      report.sources.push({ kind, dataset_id: datasetId, chat_id: first.chatId, dashboard });
      check(`${kind}: chart reuse, two saved recipes, direct URL and reload`);
    }
    const sample = report.sources[0];
    await page.goto(`/chat/${sample.chat_id}`);
    const restored = await json(`/api/chats/${sample.chat_id}`);
    const assistantsBefore = restored.messages.filter((m) => m.role === 'assistant').length;
    await expect(page.locator('[data-role="assistant"]')).toHaveCount(assistantsBefore);
    const postsBefore = posts;
    const callsBefore = (await model('/observations')).calls.length;
    await model('/control', { hold: true });
    await page.getByRole('textbox', { name: 'Вопрос к данным' }).fill('Покажи сумму target по region из targets.');
    await page.getByRole('button', { name: 'Отправить', exact: true }).click();
    await expect.poll(async () => (await model('/observations')).calls.length).toBeGreaterThan(callsBefore);
    await page.reload();
    await expect(page.getByText('Соединение прервано до конца ответа', { exact: true })).toBeVisible();
    await model('/control', { hold: false });
    assert.equal(posts, postsBefore + 1, 'Reload must not replay the question');
    await expect(page.locator('[data-role="assistant"]')).toHaveCount(assistantsBefore);
    await ask('Покажи сумму target по region из targets столбчатым графиком.');
    check('SSE interruption persists, reload does not replay POST, next turn succeeds');
    const firstToken = token;
    await logout();
    await login(`${report.username}-other`, true);
    assert.equal((await page.request.get('/api/auth/me', { headers: { Authorization: `Bearer ${firstToken}` } })).status(), 401);
    assert.deepEqual(await json('/api/datasets'), []);
    for (const s of report.sources) {
      for (const url of [`/api/datasets/${s.dataset_id}`, `/api/chats/${s.chat_id}`, `/api/dashboards/${s.dashboard.dashboard_id}`, `/api/results/${s.dashboard.widgets[0].result_id}/data`]) {
        await api(url, { expectedStatus: 404 });
      }
      await api(`/api/dashboards/${s.dashboard.dashboard_id}/refresh`, { method: 'POST', expectedStatus: 404 });
    }
    await source('csv', true);
    const own = await ask(categoryQuestion);
    await result(own.chart.result_id, [['A', 142], ['B', 8]]);
    await page.goto(`/dashboards/${sample.dashboard.dashboard_id}`);
    await expect(page.getByText('Не удалось открыть дашборд', { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Сумма по группам', exact: true })).toHaveCount(0);
    check('UI account switch clears state; foreign IDs/refresh denied; own calculation isolated');
    const calls = (await model('/observations')).calls;
    assert(calls.some((c) => c.stage === 'VerifyResponse' && c.finished));
    report.model_stages = calls.map((c) => ({ ...c }));
    report.status = 'prepared';
  } else if (values.phase === 'verify') {
    const callsBefore = (await model('/observations')).calls.length;
    for (const s of report.sources) {
      await page.goto(`/dashboards/${s.dashboard.dashboard_id}`);
      await expect(page.locator('[role="img"]')).toHaveCount(2);
      assert.deepEqual(await json(`/api/dashboards/${s.dashboard.dashboard_id}`), s.dashboard);
      const updated = await refresh(s.dashboard.dashboard_id);
      for (const [i, widget] of updated.widgets.entries()) {
        assert.equal(widget.status, 'ok');
        assert.notEqual(widget.result_id, s.dashboard.widgets[i].result_id);
        assert.deepEqual(widget.recipe, s.dashboard.widgets[i].recipe);
        assert.deepEqual(widget.tool_call, s.dashboard.widgets[i].tool_call);
        await result(widget.result_id, i === 1 ? [['North', 100], ['South', 200]] : [['North', s.kind === 'postgres' ? 30 : 20], ['South', 30]]);
      }
      s.refreshed = updated;
      await page.screenshot({ path: path.join(output, `${s.kind}-restart-refresh.png`), fullPage: true });
      check(`${s.kind}: restart preserves definitions and refresh creates fresh correct results`);
    }
    assert.equal((await model('/observations')).calls.length, callsBefore, 'Refresh must work without the LLM');
    report.status = 'restart_verified';
  } else {
    const s = report.sources.find((s) => s.kind === 'postgres');
    await page.goto(`/dashboards/${s.dashboard.dashboard_id}`);
    const callsBefore = (await model('/observations')).calls.length;
    const updated = await refresh(s.dashboard.dashboard_id);
    assert.equal(updated.widgets[0].status, 'error');
    assert.equal(updated.widgets[0].result_id, null);
    assert(updated.widgets[0].error);
    assert.equal(updated.widgets[1].status, 'ok');
    assert.notEqual(updated.widgets[1].result_id, s.refreshed.widgets[1].result_id);
    await result(updated.widgets[1].result_id, [['North', 100], ['South', 200]]);
    const failedCard = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Сумма по группам', exact: true }) });
    await expect(failedCard.locator('[role="img"]')).toHaveCount(0);
    await expect(failedCard.getByText(updated.widgets[0].error.message, { exact: true })).toBeVisible();
    await expect(page.locator('[role="img"]')).toHaveCount(1);
    assert.equal((await model('/observations')).calls.length, callsBefore);
    report.partial_refresh = updated;
    await page.screenshot({ path: path.join(output, 'postgres-partial-failure.png'), fullPage: true });
    check('Real PostgreSQL view failure affects one widget, stale chart hidden, neighbour fresh');
    report.status = 'passed';
  }
} catch (error) {
  report.status = 'failed';
  report.failed_phase = values.phase;
  report.error = String(error);
  report.page_text = await page.locator('body').innerText().catch(() => 'Page unavailable');
  await page.screenshot({ path: path.join(output, `${values.phase}-failure.png`), fullPage: true }).catch(() => {});
  throw error;
} finally {
  report.updated_at = new Date().toISOString();
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  await browser.close();
}
console.log(JSON.stringify({ status: report.status, checks: report.checks.length, application_sha: sha }));
