import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, beforeEach, vi } from 'vitest';
import { File as NodeFile } from 'node:buffer';
import { resetServer, server } from './server';

// jsdom's FormData/File are not understood by Node's fetch, so multipart uploads would arrive
// broken. Tests use Node's own classes; the browser build is untouched.
const nodeFormData = (await new Response('', { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }).formData()).constructor;
Object.defineProperty(globalThis, 'FormData', { configurable: true, writable: true, value: nodeFormData });
Object.defineProperty(globalThis, 'File', { configurable: true, writable: true, value: NodeFile });

beforeAll(() => { server.listen({ onUnhandledFrame: 'error' }); });
beforeEach(() => {
  resetServer();
  window.localStorage.clear();
  window.sessionStorage.clear();
});
afterEach(() => {
  cleanup();
});
afterAll(() => { server.close(); });

// jsdom has no layout APIs that charts and the theme rely on.
if (typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches: false, media: query, onchange: null,
      addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
    }),
  });
}

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (!('ResizeObserver' in window)) Object.defineProperty(window, 'ResizeObserver', { value: ResizeObserverStub });

// jsdom cannot lay out SVG charts; pages get a no-op ECharts instance. charts.test.tsx mocks it on its own.
vi.mock('@/charts/echarts', () => {
  const instance = { setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() };
  return { echarts: { init: vi.fn(() => instance), getInstanceByDom: vi.fn(() => instance) } };
});
