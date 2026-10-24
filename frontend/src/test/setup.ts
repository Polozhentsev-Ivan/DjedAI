import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, beforeEach, vi } from 'vitest';
import { File as NodeFile } from 'node:buffer';
import { resetServer, server } from './server';
import { clearSession } from '@/api/session';

// React constructs FormData from DOM forms, while Node fetch/MSW require native
// multipart classes (including global File when decoding incoming multipart).
const BrowserFormData = globalThis.FormData;
const NodeFormData = (await new Response('', { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }).formData()).constructor as typeof FormData;
class TestFormData extends NodeFormData {
  constructor(form?: HTMLFormElement, submitter?: HTMLElement | null) {
    super();
    if (form !== undefined) return new BrowserFormData(form, submitter);
  }
}
Object.defineProperty(globalThis, 'FormData', { configurable: true, writable: true, value: TestFormData });
Object.defineProperty(globalThis, 'File', { configurable: true, writable: true, value: NodeFile });
function fileBytes(file: File): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => { resolve(reader.result as ArrayBuffer); };
    reader.onerror = () => { reject(reader.error ?? new Error('Unable to read multipart file')); };
    reader.readAsArrayBuffer(file);
  });
}

let interceptedFetch: typeof fetch;

beforeAll(() => {
  server.listen({ onUnhandledFrame: 'error' });
  interceptedFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    if (!(init?.body instanceof BrowserFormData)) return interceptedFetch(input, init);
    const body = new NodeFormData();
    for (const [name, value] of init.body.entries()) {
      if (typeof value === 'string') body.append(name, value);
      else {
        const file = new NodeFile([new Uint8Array(await fileBytes(value))], value.name, { type: value.type, lastModified: value.lastModified });
        // The native constructor accepts Node Blob; DOM and Node declarations
        // differ in their bytes() buffer type despite sharing the Blob API.
        body.append(name, file as unknown as Blob);
      }
    }
    return interceptedFetch(input, { ...init, body });
  };
});
beforeEach(() => {
  resetServer();
  window.localStorage.clear();
  window.sessionStorage.clear();
  clearSession();
});
afterEach(() => {
  cleanup();
});
afterAll(() => {
  globalThis.fetch = interceptedFetch;
  server.close();
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
