import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { routes } from '@/app/routes';

/** Fresh cache per test; no retries so error states show at once. */
export function testQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
}

/**
 * Renders the real route tree at a URL, the way a direct visit or a reload would.
 * Waits for lazy routes so tests can query the page synchronously.
 */
export async function renderRoute(url: string) {
  const router = createMemoryRouter(routes, { initialEntries: [url] });
  await waitFor(() => { expect(router.state.initialized).toBe(true); });
  const client = testQueryClient();
  const view = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, client, ...view };
}
