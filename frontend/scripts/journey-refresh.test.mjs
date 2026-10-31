import assert from 'node:assert/strict';
import test from 'node:test';
import { verifyRefreshResults } from './journey-refresh.mjs';

const previous = { widgets: ['orders', 'targets'].map((name) => ({
  widget_id: `w_${name}`, result_id: `r_old_${name}`,
  recipe: null, tool_call: { tool: 'run_sql', params: { sql: `SELECT * FROM ${name}` } },
})) };
const refreshed = previous.widgets.map((widget) => ({
  ...widget, status: 'ok', result_id: widget.result_id.replace('old', 'new'),
}));
const expectedRows = [[['North', 30], ['South', 30]], [['North', 100], ['South', 200]]];

for (const [name, widgets] of [
  ['empty', []],
  ['missing', refreshed.slice(0, 1)],
  ['substituted', [refreshed[0], { ...refreshed[1], widget_id: 'w_foreign' }]],
  ['reordered', [...refreshed].reverse()],
  ['duplicated', [refreshed[0], refreshed[0]]],
]) {
  test(`verify phase rejects ${name} widgets before reading results`, async () => {
    let reads = 0;
    await assert.rejects(
      verifyRefreshResults({ widgets }, previous, expectedRows, async () => { reads++; }),
      /Refresh must preserve every saved widget/,
    );
    assert.equal(reads, 0);
  });
}

test('verify phase checks both fresh results of the intact dashboard', async () => {
  const reads = [];
  await verifyRefreshResults({ widgets: refreshed }, previous, expectedRows, async (id, rows) => {
    reads.push([id, rows]);
  });
  assert.deepEqual(reads, refreshed.map((widget, index) => [widget.result_id, expectedRows[index]]));
});
