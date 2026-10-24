import { http, HttpResponse } from 'msw/http';
import { server } from './server';

const url = 'http://localhost:3000/api/after-abort';

describe('mock backend', () => {
  // With socket-level interception about one request in a few hundred escaped to the real
  // localhost here: Undici replaces the socket of an aborted request, and a late first write
  // on the new socket made MSW pass it through (B4-11).
  it('serves every request that follows an aborted one', async () => {
    server.use(http.get(url, async ({ request }) => {
      if (request.headers.has('X-Hang')) await new Promise(() => undefined);
      return HttpResponse.json({ ok: true });
    }));
    const failures: string[] = [];
    for (let i = 0; i < 200; i++) {
      const controller = new AbortController();
      const hung = fetch(url, { headers: { 'X-Hang': '1' }, signal: controller.signal }).catch((error: unknown) => error);
      await new Promise((resolve) => setTimeout(resolve, 1));
      controller.abort();
      expect(await hung).toMatchObject({ name: 'AbortError' });
      try {
        expect(await (await fetch(url)).json()).toEqual({ ok: true });
      } catch (error) {
        failures.push(`#${i}: ${String(error)}`);
      }
    }
    expect(failures).toEqual([]);
  });
});
