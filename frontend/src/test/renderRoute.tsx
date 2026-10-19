import { render, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { routes } from '@/app/routes';

/**
 * Renders the real route tree at a URL, the way a direct visit or a reload would.
 * Waits for lazy routes so tests can query the page synchronously.
 */
export async function renderRoute(url: string) {
  const router = createMemoryRouter(routes, { initialEntries: [url] });
  await waitFor(() => { expect(router.state.initialized).toBe(true); });
  const view = render(<RouterProvider router={router} />);
  return { router, ...view };
}
