import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

afterEach(() => {
  cleanup();
});

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
