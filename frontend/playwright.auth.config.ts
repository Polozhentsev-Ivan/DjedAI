import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: 'auth.spec.ts',
  workers: 1,
  timeout: 60_000,
  outputDir: process.env.DJEDAI_E2E_OUTPUT ?? '../../artifacts/issue-102/playwright-auth',
  use: { baseURL: 'http://127.0.0.1:4174', trace: 'retain-on-failure' },
  webServer: {
    // A dev-only entry imports production client modules; no harness is shipped in dist.
    command: 'npm run dev -- --host 127.0.0.1 --port 4174 --strictPort',
    url: 'http://127.0.0.1:4174/e2e/auth-harness.html',
    env: { VITE_API_MOCKS: 'false', DJEDAI_API_PROXY: 'http://127.0.0.1:18082' },
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
