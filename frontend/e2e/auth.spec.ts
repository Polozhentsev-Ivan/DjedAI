import { test, expect, type Page } from '@playwright/test';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { harness } from './auth-harness';

declare global { interface Window { authHarness: typeof harness } }
const root = fileURLToPath(new URL('../..', import.meta.url));
let backend: ChildProcess | undefined;
let directory: string;
let python: string;
// Cold Python imports and database initialization are test-server preparation,
// separate from the application behavior asserted below.
const BACKEND_STARTUP_TIMEOUT = 90_000;

async function startBackend() {
  backend = spawn(python, ['tests/e2e/serve_auth.py', '--data-dir', directory], { cwd: root, stdio: 'pipe' });
  const process = backend;
  let output = '';
  process.stderr?.on('data', (chunk: Buffer) => { output += chunk.toString(); });
  await expect.poll(async () => {
    if (process.exitCode !== null || process.signalCode !== null) throw new Error(`Backend failed: ${output}`);
    try { return (await fetch('http://127.0.0.1:18082/health')).status; } catch { return 0; }
  }, { timeout: BACKEND_STARTUP_TIMEOUT }).toBe(200);
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
  test.setTimeout(BACKEND_STARTUP_TIMEOUT + 30_000);
  directory = await mkdtemp(path.join(tmpdir(), 'djedai-auth-browser-'));
  python = execFileSync('uv', ['run', '--locked', 'python', '-c', 'import sys; print(sys.executable)'], { cwd: root, encoding: 'utf8' }).trim();
  await startBackend();
});
test.afterAll(async () => { await stopBackend(); });

async function openHarness(page: Page) {
  await page.goto('/e2e/auth-harness.html');
  await page.waitForFunction(() => Boolean(window.authHarness));
}

test('real session survives reload and backend restart; one Bearer transport covers JSON, multipart and GET/POST SSE', async ({ page }) => {
  await openHarness(page);
  const user = await page.evaluate(() => window.authHarness.register({ username: 'browser-user', password: 'browser-password-102' }));
  expect(await page.evaluate(() => window.authHarness.hasToken())).toBe(false);
  await page.evaluate(async () => { await window.authHarness.login({ username: 'browser-user', password: 'browser-password-102' }); });
  expect(await page.evaluate(() => window.authHarness.me())).toMatchObject({ user_id: user.user_id });
  expect(await page.evaluate(() => window.authHarness.json())).toEqual({ user_id: user.user_id, body: { marker: 'json-✓' } });
  expect(await page.evaluate(() => window.authHarness.upload())).toEqual({ user_id: user.user_id, name: 'transport.csv', body: 'id,name\n1,Москва\n' });
  for (const method of ['GET', 'POST'] as const) {
    expect(await page.evaluate((value) => window.authHarness.stream(value), method)).toEqual({ outcome: 'done', events: [{ event: 'done', data: { user_id: user.user_id } }] });
  }
  expect(await page.evaluate(() => window.localStorage.getItem('djedai.access_token'))).toBeNull();
  expect(new URL(page.url()).search).toBe('');
  await page.reload();
  await page.waitForFunction(() => Boolean(window.authHarness));
  expect(await page.evaluate(() => window.authHarness.me())).toMatchObject({ user_id: user.user_id });
  await stopBackend();
  await startBackend();
  expect(await page.evaluate(() => window.authHarness.me())).toMatchObject({ user_id: user.user_id });
  const token = await page.evaluate(() => window.sessionStorage.getItem('djedai.access_token'));
  await page.evaluate(() => window.authHarness.logout());
  expect(await page.evaluate(() => window.authHarness.hasToken())).toBe(false);
  expect((await page.request.get('/api/auth/me', { headers: { Authorization: `Bearer ${token}` } })).status()).toBe(401);
});

test('invalid login never installs a token; expiry clears the tab session', async ({ page }) => {
  await openHarness(page);
  const failure = await page.evaluate(async () => {
    try { await window.authHarness.login({ username: 'missing', password: 'incorrect-password' }); }
    catch (error) { return (error as { status: number }).status; }
    return 0;
  });
  expect(failure).toBe(401);
  expect(await page.evaluate(() => window.authHarness.hasToken())).toBe(false);
  await page.evaluate(() => window.authHarness.register({ username: 'expiry-user', password: 'browser-password-102' }));
  await page.evaluate(async () => { await window.authHarness.login({ username: 'expiry-user', password: 'browser-password-102' }); });
  await page.evaluate(() => window.authHarness.expire());
  const expired = await page.evaluate(async () => {
    try { await window.authHarness.me(); }
    catch (error) { return (error as { status: number }).status; }
    return 0;
  });
  expect(expired).toBe(401);
  expect(await page.evaluate(() => window.authHarness.hasToken())).toBe(false);
  expect(await page.evaluate(() => window.sessionStorage.getItem('djedai.access_token'))).toBeNull();
});
