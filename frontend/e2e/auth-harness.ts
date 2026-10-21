// Dev-only browser entry; it imports the exact transport used by the React application.
import { login, logout, me, register } from '../src/api/auth';
import { request } from '../src/api/client';
import { sessionSnapshot } from '../src/api/session';
import { streamEvents } from '../src/api/sse';

export const harness = {
  register, login, logout, me,
  hasToken: () => sessionSnapshot().accessToken !== null,
  json: () => request('/_auth-test/json', { method: 'POST', json: { marker: 'json-✓' } }),
  upload: () => {
    const form = new FormData();
    form.set('file', new File(['id,name\n1,Москва\n'], 'transport.csv', { type: 'text/csv' }));
    return request('/_auth-test/upload', { method: 'POST', form });
  },
  stream: async (method: 'GET' | 'POST') => {
    const events: unknown[] = [];
    const outcome = await streamEvents('/_auth-test/events', {
      method, ...(method === 'POST' ? { json: { marker: 'post-sse' } } : {}),
      onEvent: (event) => { events.push(event); },
    });
    return { outcome, events };
  },
  expire: () => request('/_auth-test/expire', { method: 'POST' }),
};

declare global { interface Window { authHarness: typeof harness } }
window.authHarness = harness;
