import { fileURLToPath } from 'node:url';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { msw } from 'msw/vite';
import { defineConfig } from 'vitest/config';

// Backend address for the dev proxy; the browser itself always talks to /api.
const apiTarget = process.env.DJEDAI_API_PROXY ?? 'http://localhost:8000';

export default defineConfig({
  // msw() serves the mock worker in dev only; production builds never import virtual:msw.
  plugins: [react(), tailwindcss(), msw()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    // ECharts is one ~560 kB chunk loaded only by the chat and dashboard screens.
    chunkSizeWarningLimit: 600,
    rolldownOptions: {
      output: {
        codeSplitting: { groups: [{ name: 'echarts', test: /node_modules[\\/](echarts|zrender)[\\/]/ }] },
      },
    },
  },
  server: {
    proxy: { '/api': { target: apiTarget, changeOrigin: true } },
  },
  preview: {
    proxy: { '/api': { target: apiTarget, changeOrigin: true } },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.test.{ts,tsx}', 'src/test/**', 'src/fixtures/**', 'src/api/generated.ts', 'src/main.tsx', 'src/vite-env.d.ts',
        // Dev-only bootstrap of the mock API; it needs a real Service Worker, which jsdom lacks.
        'src/mocks/browser.ts'],
      reporter: ['text', 'html', 'lcov'],
      thresholds: { lines: 80 },
    },
  },
});
