import { expect, test, type Page } from '@playwright/test';
import type { Schemas } from '../src/api/client';
import { chatFixture, fixtureResult, fixtureResultMeta } from '../src/test/chatFixture';

const created = '2026-10-25T12:00:00Z';
function fixtureChart(): Schemas['ChartBlock'] {
  const block = chatFixture.blocks.find((value) => value.type === 'chart');
  if (block?.type !== 'chart') throw new Error('The shared chat fixture has no chart block');
  return block;
}
const chart = fixtureChart();

/**
 * A saved answer with one chart against a faked API that keeps saved dashboards for the whole test,
 * so a reload reads the dashboard back the way the server would return it.
 */
async function dashboardApi(page: Page) {
  const dashboards: Schemas['Dashboard'][] = [];
  const saves: Schemas['DashboardCreate'][] = [];
  await page.addInitScript(() => {
    sessionStorage.setItem('djedai.access_token', 'dashboard-save-session');
    localStorage.setItem('djedai.theme', 'light');
  });
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    expect(request.headers().authorization).toBe('Bearer dashboard-save-session');
    const chat: Schemas['ChatDetail'] = { chat_id: 'c_save', dataset_id: 'ds_save', title: 'Выручка', created_at: created,
      messages: [{ message_id: 'm_chart', role: 'assistant', created_at: created, blocks: [{ type: 'text', markdown: 'Выручка росла весь квартал.' }, chart], result_ids: [] }] };
    let body: unknown;
    if (path === '/api/auth/me') body = { user_id: 'u_save', username: 'Проверка дашборда', created_at: created } satisfies Schemas['UserRead'];
    else if (path === '/api/datasets') body = [{ dataset_id: 'ds_save', name: 'Продажи', status: 'ready', source: { kind: 'csv_upload' }, table_count: 1, row_count: 120_000, created_at: created }] satisfies Schemas['DatasetSummary'][];
    else if (path === '/api/chats') body = [chat];
    else if (path === '/api/chats/c_save') body = chat;
    else if (path === '/api/results/r_fixture') body = fixtureResultMeta;
    else if (path === '/api/results/r_fixture/data') body = fixtureResult;
    else if (path === '/api/dashboards' && request.method() === 'GET') {
      body = dashboards.map(({ dashboard_id, dataset_id, title, refreshed_at }) => ({ dashboard_id, dataset_id, title, refreshed_at: refreshed_at ?? null }));
    } else if (path === '/api/dashboards' && request.method() === 'POST') {
      const save = request.postDataJSON() as Schemas['DashboardCreate'];
      saves.push(save);
      const dashboard: Schemas['Dashboard'] = { dashboard_id: 'd_saved', dataset_id: save.dataset_id, title: save.chart.title, created_at: created, refreshed_at: null, widgets: [{
        widget_id: 'w_saved', tool_call: { tool: fixtureResultMeta.tool ?? '', params: fixtureResultMeta.params ?? {} }, chart: save.chart, caption: save.chart.subtitle ?? null,
        layout: { x: 0, y: 0, w: 6, h: 4 }, result_id: save.result_id, status: 'ok', error: null,
      }] };
      dashboards.push(dashboard);
      body = dashboard;
    } else if (path === '/api/dashboards/d_saved' && dashboards[0]) body = dashboards[0];
    else throw new Error(`Unexpected fixture request: ${request.method()} ${path}`);
    await route.fulfill({ json: body });
  });
  return saves;
}

test('explains a chart answer, saves it to a new dashboard and reopens it after a reload', async ({ page }, info) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const saves = await dashboardApi(page);
  await page.goto('/chat/c_save');
  const card = page.locator('section').filter({ has: page.getByRole('img', { name: /Выручка по месяцам/ }) });
  await expect.poll(() => card.locator('svg path').count()).toBeGreaterThan(0);

  await card.getByRole('button', { name: 'Как посчитано' }).click();
  await expect(card.getByText(fixtureResultMeta.sql ?? '')).toBeVisible();
  await expect(card.getByRole('list', { name: 'Параметры' })).toContainText("freq = 'month'");
  await card.screenshot({ path: info.outputPath('chart-how-calculated.png') });

  await card.getByRole('button', { name: 'В дашборд' }).click();
  await page.getByRole('menuitem', { name: /Новый дашборд/ }).click();
  await expect(page.getByRole('status').filter({ hasText: 'График добавлен в «Выручка по месяцам»' })).toBeVisible();
  expect(saves).toEqual([{ dataset_id: 'ds_save', result_id: 'r_fixture', chart: chart.chart }]);
  await page.screenshot({ path: info.outputPath('chart-saved-notice.png') });

  await page.getByRole('button', { name: 'Открыть →' }).click();
  await expect(page).toHaveURL(/\/dashboards\/d_saved$/);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Дашборд: Выручка по месяцам' })).toBeVisible();
  await page.getByRole('button', { name: 'Как посчитано' }).click();
  await expect(page.getByRole('list', { name: 'Рецепт' })).toHaveText("time_aggregate(table='orders', freq='month')");
  await page.screenshot({ path: info.outputPath('dashboard-reopened.png') });
});
