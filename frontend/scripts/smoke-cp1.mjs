/** Browser acceptance against an already running production UI/API and real model.
 * Run, restart the backend with the same storage, then run --verify.
 */
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { parseArgs } from 'node:util';
import { chromium } from '@playwright/test';

const { values } = parseArgs({ options: {
  url: { type: 'string', default: 'http://localhost:8080' },
  output: { type: 'string' }, verify: { type: 'boolean', default: false },
} });
assert(values.output, 'Provide --output with an evidence directory outside the checkout');
const output = path.resolve(values.output);
await mkdir(output, { recursive: true });
const reportPath = path.join(output, 'browser-report.json');
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, baseURL: values.url });
page.setDefaultTimeout(180_000);
const username = process.env.DJEDAI_SMOKE_USERNAME;
const password = process.env.DJEDAI_SMOKE_PASSWORD;
assert(username && password, 'Set DJEDAI_SMOKE_USERNAME and DJEDAI_SMOKE_PASSWORD for the smoke account');
const registration = await page.request.post('/api/auth/register', { data: { username, password } });
assert([201, 409].includes(registration.status()), 'Unable to register the smoke account');
const login = await page.request.post('/api/auth/login', { data: { username, password } });
assert.equal(login.status(), 200, 'Unable to authenticate the smoke account');
const { access_token: accessToken } = await login.json();
await page.addInitScript((token) => { window.sessionStorage.setItem('djedai.access_token', token); }, accessToken);
const authHeaders = { Authorization: `Bearer ${accessToken}` };
await page.addInitScript(() => {
  window.__cp1Streams = [];
  const original = window.fetch;
  window.fetch = async (...args) => {
    const [input, options] = args;
    const url = typeof input === 'string' || input instanceof URL ? new URL(input, window.location.href).href : input.url;
    const method = options?.method ?? (input instanceof Request ? input.method : 'GET');
    const question = method.toUpperCase() === 'POST' && new URL(url).pathname.endsWith('/messages');
    const stream = { url, chunks: [], raw: '', finished: false, interrupted: false };
    if (question) window.__cp1Streams.push(stream);
    let response;
    try {
      response = await original(...args);
    } catch (error) {
      if (question) stream.interrupted = true;
      throw error;
    }
    if (response.headers.get('content-type')?.includes('text/event-stream')) {
      stream.url = response.url;
      if (!question) window.__cp1Streams.push(stream);
      const clone = response.clone();
      const started = performance.now();
      void (async () => {
        const reader = clone.body.getReader();
        const decoder = new TextDecoder();
        try {
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            const text = decoder.decode(value, { stream: true });
            stream.raw += text;
            stream.chunks.push({ ms: Math.round(performance.now() - started), text });
          }
          stream.raw += decoder.decode();
          stream.finished = true;
        } catch { stream.interrupted = true; }
      })();
    } else if (question) stream.finished = true;
    return response;
  };
});

async function json(url) {
  const response = await page.request.get(url, { headers: authHeaders });
  assert.equal(response.status(), 200, `${url}: ${await response.text()}`);
  return response.json();
}

async function verifyRun(run, index) {
  const detail = await json(`/api/datasets/${run.dataset_id}`);
  assert.deepEqual(detail, run.dataset);
  await page.goto(run.chat_url);
  await page.getByText(run.answer, { exact: true }).waitFor();
  assert.equal(await page.getByText(run.answer, { exact: true }).count(), 1);
  const chat = await json(`/api/chats/${run.chat_id}`);
  assert.deepEqual(chat, run.chat);
  await page.locator('details[data-result-id]').first().locator('summary').click();
  await page.locator('details[data-result-id] pre').first().waitFor();
  for (const result of run.results) {
    assert.deepEqual(await json(`/api/results/${result.metadata.result_id}`), result.metadata);
    assert.deepEqual(await json(`/api/results/${result.metadata.result_id}/data`), result.data);
  }
  await page.screenshot({ path: path.join(output, `restart-${index}.png`), fullPage: true });
}

try {
  if (values.verify) {
    const report = JSON.parse(await readFile(reportPath, 'utf8'));
    for (const [index, run] of report.runs.entries()) await verifyRun(run, index + 1);
    report.restart = { passed: true, verified_at: new Date().toISOString() };
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
    console.log('PASS: datasets, catalogs, browser history, SQL and result data survived restart');
  } else {
    const report = {
      started_at: new Date().toISOString(), origin: values.url,
      commit: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
      working_tree: execFileSync('git', ['status', '--short'], { encoding: 'utf8' }).trim(),
      runs: [],
    };
    for (const [index, amounts] of [[12, 30, 8], [401, 500]].entries()) {
      const name = `cp1_orders_${index + 1}`;
      const body = `id,amount\n${amounts.map((amount, i) => `${i + 1},${amount}`).join('\n')}\n`;
      const oracle = { n: amounts.length, total: amounts.reduce((sum, amount) => sum + amount, 0) };
      await writeFile(path.join(output, `${name}.csv`), body);
      await page.goto('/sources');
      const acceptedPromise = page.waitForResponse((r) => r.url().endsWith('/api/datasets/upload') && r.request().method() === 'POST');
      const files = [{ name: `${name}.csv`, mimeType: 'text/csv', buffer: Buffer.from(body) }];
      if (index === 0) files.push({ name: 'cp1_customers.csv', mimeType: 'text/csv', buffer: Buffer.from('id,name\n1,Alice\n2,Bob\n') });
      await page.getByTestId('csv-input').setInputFiles(files);
      const accepted = await acceptedPromise;
      assert.equal(accepted.status(), 202);
      const { dataset_id: datasetId } = await accepted.json();
      const eventResponse = await page.request.get(`/api/datasets/${datasetId}/events`, { headers: authHeaders });
      assert.match(await eventResponse.text(), /event: done/);
      const dataset = await json(`/api/datasets/${datasetId}`);
      assert.equal(dataset.status, 'ready');
      assert.equal(dataset.table_count, index === 0 ? 2 : 1);
      await page.goto(`/catalog/${datasetId}`);
      await page.getByText('amount', { exact: true }).first().waitFor();
      await page.screenshot({ path: path.join(output, `catalog-${index + 1}.png`), fullPage: true });
      await page.getByRole('button', { name: /чат/i }).click();
      const question = `По таблице ${name} одним SELECT вычисли COUNT(*) AS n и SUM(amount) AS total без фильтров. Ответь только строкой COUNT=<целое>;SUM=<число>, без других слов и без графиков.`;
      await page.getByRole('textbox', { name: 'Вопрос к данным' }).fill(question);
      await page.getByRole('button', { name: 'Отправить', exact: true }).click();
      await page.waitForFunction(() => window.__cp1Streams.some((s) => s.url.endsWith('/messages') && (s.finished || s.interrupted)), undefined, { timeout: 180_000 });
      await page.getByRole('button', { name: 'Отправить', exact: true }).waitFor();
      const chatUrl = page.url();
      const chatId = new URL(chatUrl).pathname.split('/').at(-1);
      const chat = await json(`/api/chats/${chatId}`);
      const assistant = chat.messages.findLast((message) => message.role === 'assistant');
      assert(assistant, 'The model must produce a persisted assistant message');
      const asked = chat.messages.findLast((message) => message.role === 'user');
      assert.equal(asked?.text, question);
      assert.equal(asked?.turn?.status, 'success');
      assert.equal(asked.turn.assistant_message_id, assistant.message_id);
      const answer = assistant.blocks.filter((b) => b.type === 'text').map((b) => b.markdown).join('\n');
      const match = answer.match(/COUNT\s*=\s*(\d+)\s*;\s*SUM\s*=\s*(-?\d+(?:\.\d+)?)/i);
      assert(match, `Unexpected answer format: ${answer}`);
      assert.deepEqual({ n: Number(match[1]), total: Number(match[2]) }, oracle);
      await page.getByText(answer, { exact: true }).waitFor();
      assert.equal(await page.getByText(answer, { exact: true }).count(), 1);
      const results = [];
      for (const id of assistant.result_ids) results.push({ metadata: await json(`/api/results/${id}`), data: await json(`/api/results/${id}/data`) });
      assert(results.some((result) => {
        if (result.metadata.tool !== 'run_sql' || !result.metadata.sql?.includes(name) || result.data.rows.length !== 1) return false;
        const columns = result.data.columns.map((column) => typeof column === 'string' ? column : column.name);
        const row = result.data.rows[0];
        return columns.indexOf('n') >= 0 && columns.indexOf('total') >= 0
          && Number(row[columns.indexOf('n')]) === oracle.n
          && Number(row[columns.indexOf('total')]) === oracle.total;
      }), 'Persisted SQL must calculate COUNT AS n and SUM AS total exactly');
      const streams = await page.evaluate(() => window.__cp1Streams);
      const chatStream = streams.find((s) => s.url.endsWith('/messages'));
      if (chatStream.finished) {
        assert.match(chatStream.raw, /event: status/);
        assert.match(chatStream.raw, /event: done/);
      } else {
        // GET-confirmed success closes the stale stream, even before its headers arrive.
        assert.equal(chatStream.interrupted, true);
      }
      assert.doesNotMatch(chatStream.raw, /event: error/);
      assert.equal(await page.evaluate(() => navigator.serviceWorker.getRegistrations().then((items) => items.length)), 0);
      await page.locator('details[data-result-id]').first().locator('summary').click();
      await page.locator('details[data-result-id] pre').first().waitFor();
      await page.screenshot({ path: path.join(output, `answer-${index + 1}.png`), fullPage: true });
      report.runs.push({ dataset_id: datasetId, dataset, chat_id: chatId, chat_url: chatUrl, chat, question, oracle, answer, results, streams });
      await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
      console.log(`PASS: ${name}: COUNT=${oracle.n};SUM=${oracle.total}, ${assistant.result_ids.join(', ')}`);
    }
    report.completed_at = new Date().toISOString();
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  }
} finally {
  await browser.close();
}
