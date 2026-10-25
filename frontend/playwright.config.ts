import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: ['cp1.spec.ts', 'charts.spec.ts', 'chat-blocks.spec.ts'],
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  outputDir: process.env.DJEDAI_E2E_OUTPUT ?? '../artifacts/playwright',
  use: { baseURL: 'http://127.0.0.1:4173', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    env: { VITE_API_MOCKS: 'false', DJEDAI_API_PROXY: 'http://127.0.0.1:18080' },
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
