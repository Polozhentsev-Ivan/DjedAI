import assert from 'node:assert/strict';

export function assertWidgetIdentities(updated, previous) {
  assert.deepEqual(
    updated.widgets.map((widget) => widget.widget_id),
    previous.widgets.map((widget) => widget.widget_id),
    'Refresh must preserve every saved widget and its order',
  );
}

export async function verifyRefreshResults(updated, previous, expectedRows, result) {
  // Validate the complete collection before checking individual results.
  assertWidgetIdentities(updated, previous);
  for (const [i, widget] of updated.widgets.entries()) {
    assert.equal(widget.status, 'ok');
    assert.notEqual(widget.result_id, previous.widgets[i].result_id);
    assert.deepEqual(widget.recipe, previous.widgets[i].recipe);
    assert.deepEqual(widget.tool_call, previous.widgets[i].tool_call);
    await result(widget.result_id, expectedRows[i]);
  }
}
