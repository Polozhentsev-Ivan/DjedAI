import { expect, test, type Page } from '@playwright/test';
import type { Schemas } from '../src/api/client';
import { chartExamples } from '../src/fixtures/charts';

const created = '2026-10-24T12:00:00Z';
const chat: Schemas['ChatDetail'] = {
  chat_id: 'c_charts', dataset_id: 'ds_charts', title: 'Проверка визуализаций', created_at: created,
  messages: [{ message_id: 'm_charts', role: 'assistant', created_at: created, result_ids: [], blocks: chartExamples.map(({ spec }) => ({ type: 'chart', chart: spec })) }],
};

async function examplesApi(page: Page) {
  await page.addInitScript(() => {
    sessionStorage.setItem('djedai.access_token', 'chart-renderer-session');
    if (!localStorage.getItem('djedai.theme')) localStorage.setItem('djedai.theme', 'light');
  });
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    expect(route.request().headers().authorization).toBe('Bearer chart-renderer-session');
    let body: unknown;
    if (path === '/api/auth/me') body = { user_id: 'u_charts', username: 'Проверка графиков', created_at: created } satisfies Schemas['UserRead'];
    else if (path === '/api/datasets') body = [{ dataset_id: 'ds_charts', name: 'Визуализации', status: 'ready', source: { kind: 'csv_upload' }, table_count: 1, row_count: 20, created_at: created }] satisfies Schemas['DatasetSummary'][];
    else if (path === '/api/chats') body = [chat];
    else if (path === '/api/chats/c_charts') body = chat;
    else if (path === '/api/dashboards') body = [];
    else {
      const example = chartExamples.find(({ spec }) => path === `/api/results/${spec.result_id}/data`);
      if (!example) throw new Error(`Unexpected fixture request: ${path}`);
      body = example.data;
    }
    await route.fulfill({ json: body });
  });
}

test.beforeEach(async ({ page }) => { await examplesApi(page); });

test('scatter legend toggles missing and literal categories independently', async ({ page }, info) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.route('**/api/results/r_scatter/data', (route) => route.fulfill({ json: {
    columns: ['orders', 'average', 'region'], rows: [[1, 10, null], [2, 20, 'Нет значения']], row_count: 2, truncated: false,
  } satisfies Schemas['ResultData'] }));
  await page.goto('/chat/c_charts');
  const scatter = page.getByRole('img', { name: /Заказы и средний чек/ });
  await scatter.scrollIntoViewIfNeeded();
  const missing = scatter.getByText('Нет значения (пропуск) · Средний чек, ₽', { exact: true });
  const literal = scatter.getByText('"Нет значения" (строка) · Средний чек, ₽', { exact: true });
  await expect(missing).toBeVisible();
  await expect(literal).toBeVisible();
  // Count scatter's unit-circle markers, excluding axes and larger legend icons.
  const points = scatter.locator('svg path[d^="M1 0A1 1"]');
  await expect(points).toHaveCount(2);
  await missing.click();
  await expect(points).toHaveCount(1);
  await literal.click();
  await expect(points).toHaveCount(0);
  await missing.click();
  await expect(points).toHaveCount(1);
  await literal.click();
  await expect(points).toHaveCount(2);
  await page.mouse.move(0, 0);
  let previousFrame = '';
  await expect.poll(async () => {
    const frame = await scatter.locator('svg').innerHTML();
    const settled = frame === previousFrame;
    previousFrame = frame;
    return settled;
  }, { intervals: [100] }).toBe(true);
  await scatter.screenshot({ path: info.outputPath('scatter-distinct-categories.png') });
});

test('all ten manual specs render through the production chat and result data API', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/chat/c_charts');
  for (const { spec } of chartExamples) {
    const card = page.getByRole('heading', { name: spec.title, exact: true }).locator('..').locator('..').locator('..');
    await card.scrollIntoViewIfNeeded();
    await expect(card.getByRole('alert')).toHaveCount(0);
    if (spec.type === 'table') await expect(card.getByRole('cell', { name: '60%', exact: true })).toBeVisible();
    else if (spec.type === 'kpi') await expect(card.getByText('4,0 млн ₽', { exact: true })).toBeVisible();
    else {
      const chart = card.getByRole('img');
      await expect(chart.locator('svg')).toBeVisible();
      await expect.poll(() => chart.locator('svg path').count()).toBeGreaterThan(0);
      // Browser layout and ECharts' SVG renderer are real, including the newly registered modules.
      await expect(chart.locator('svg')).toHaveAttribute('width', /[1-9]\d*/);
      const clipped = await chart.locator('svg').evaluate((svg) => {
        const frame = svg.getBoundingClientRect();
        return [...svg.querySelectorAll('text')].filter((text) => {
          const box = text.getBoundingClientRect();
          return box.left < frame.left - 1 || box.right > frame.right + 1 || box.top < frame.top - 1 || box.bottom > frame.bottom + 1;
        }).map((text) => text.textContent);
      });
      expect(clipped, `Axis/legend text outside ${spec.type}`).toEqual([]);
    }
    await card.screenshot({ path: info.outputPath(`${spec.type}-light.png`) });
  }
  await page.getByRole('button', { name: 'Включить тёмную тему' }).click();
  const heatmap = page.getByRole('img', { name: /Продажи по регионам и кварталам/ });
  await heatmap.scrollIntoViewIfNeeded();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await heatmap.screenshot({ path: info.outputPath('heatmap-dark.png') });
  await page.reload();
  await expect(page.getByRole('img', { name: /Заказы и средний чек/ })).toBeAttached();
  await expect(page.locator('html')).toHaveClass(/dark/);
  expect(errors).toEqual([]);
});

test('charts resize on a narrow screen and display recoverable data errors', async ({ page }, info) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto('/chat/c_charts');
  const scatter = page.getByRole('img', { name: /Заказы и средний чек/ });
  await scatter.scrollIntoViewIfNeeded();
  const width = await scatter.evaluate((element) => element.getBoundingClientRect().width);
  expect(width).toBeGreaterThan(200);
  expect(width).toBeLessThan(768);
  await scatter.screenshot({ path: info.outputPath('scatter-tablet.png') });
  await page.setViewportSize({ width: 1024, height: 768 });
  await expect.poll(() => scatter.evaluate((element) => Number(element.querySelector('svg')?.getAttribute('width')))).toBeGreaterThan(width);

  await page.route('**/api/results/r_histogram/data', (route) => route.fulfill({ status: 404, json: { error: { code: 'not_found', message: 'Результат не найден' } } }));
  await page.reload();
  const error = page.getByRole('alert').filter({ hasText: 'Не удалось загрузить данные графика' });
  await expect(error).toHaveText(/Результат не найден/);
  await error.scrollIntoViewIfNeeded();
  await error.screenshot({ path: info.outputPath('result-error.png') });
  await page.unroute('**/api/results/r_histogram/data');
  await error.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByRole('img', { name: /Распределение сумм заказа/ })).toBeVisible();
});
