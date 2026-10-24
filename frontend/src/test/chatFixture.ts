import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Schemas } from '../api/client';

// Read from disk: the fixture lives outside the Vite root, and jsdom's URL is not accepted by node:fs.
const path = resolve(dirname(fileURLToPath(import.meta.url)), '../../../tests/fixtures/chat/blocks.json');

interface ChatFixture {
  blocks: Schemas['Block'][];
  events: { event: string; data: Record<string, unknown> }[];
}

/** The agreed answer blocks and stream events shared with the backend tests (tests/fixtures/chat/blocks.json). */
export const chatFixture = JSON.parse(readFileSync(path, 'utf8')) as ChatFixture;

export const fixtureEvent = (name: string) => {
  const found = chatFixture.events.find((value) => value.event === name);
  if (!found) throw new Error(`No ${name} event in the chat fixture`);
  return found.data;
};

/** Rows for the fixture's r_fixture result, which its chart and table blocks reference. */
export const fixtureResult: Schemas['ResultData'] = {
  columns: ['month', 'revenue'],
  rows: [['2026-01-01', 1_800_000], ['2026-02-01', 2_100_000], ['2026-03-01', 2_450_000]],
  row_count: 3,
  truncated: false,
};

export const fixtureResultMeta: Schemas['ToolResultMeta'] = {
  result_id: 'r_fixture', status: 'ok', tool: 'time_aggregate', params: { table: 'orders', freq: 'month' }, data: fixtureResult,
  sql: "SELECT date_trunc('month', payment_date) AS month, SUM(amount) AS revenue FROM orders GROUP BY month ORDER BY month;",
};
