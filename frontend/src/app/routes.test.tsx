import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderRoute } from '@/test/renderRoute';

describe('routing', () => {
  it('redirects the root to sources', async () => {
    const { router } = await renderRoute('/');
    expect(await screen.findByRole('heading', { name: 'Источники данных' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/sources');
  });

  it('opens a nested route directly, as after a reload', async () => {
    await renderRoute('/catalog/ds_orders');
    expect(await screen.findByRole('heading', { name: 'Каталог датасета: orders_2026' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Каталог' })).toHaveAttribute('aria-current', 'page');
  });

  it('moves between sections from the sidebar', async () => {
    const { router } = await renderRoute('/sources');
    await userEvent.click(screen.getByRole('link', { name: 'Дашборды' }));
    expect(await screen.findByRole('heading', { name: 'Дашборд: Квартальная статистика продаж' })).toBeInTheDocument();
    expect(within(screen.getByRole('main')).getByRole('link', { name: /Регионы/ })).toHaveAttribute('href', '/dashboards/d_r');
    await userEvent.click(screen.getByRole('link', { name: 'Чат' }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/chat'); });
    // Router state can update before React replaces the previous page's links.
    expect(await screen.findByRole('heading', { name: 'Новый чат' })).toBeInTheDocument();
    const chatLink = await within(screen.getByRole('complementary')).findByRole('link', { name: /Регионы/ });
    expect(chatLink).toHaveAttribute('href', '/chat/c_reg');
    await userEvent.click(chatLink);
    await waitFor(() => { expect(router.state.location.pathname).toBe('/chat/c_reg'); });
    expect(await screen.findByRole('heading', { name: 'Регионы' })).toBeInTheDocument();
  });

  it('shows not found for unknown pages', async () => {
    const { router } = await renderRoute('/nope');
    expect(await screen.findByText('Страница не найдена')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'К источникам' }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/sources'); });
  });

  it('logs out to the login page', async () => {
    const { router } = await renderRoute('/sources');
    await userEvent.click(screen.getByRole('button', { name: /Выйти/ }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/login'); });
  });

  it('changes and remembers the active dataset', async () => {
    const { router } = await renderRoute('/catalog');
    const picker = screen.getByRole('button', { name: 'Активный датасет' });
    await waitFor(() => { expect(picker).toHaveTextContent('orders_2026'); });
    await userEvent.click(picker);
    expect(screen.getByRole('menuitem', { name: /marketing_q3/ })).toHaveAttribute('data-disabled');
    await userEvent.click(screen.getByRole('menuitem', { name: /retail_crm_prod/ }));
    expect(picker).toHaveTextContent('retail_crm_prod');
    expect(await screen.findByRole('heading', { name: 'Каталог датасета: retail_crm_prod' })).toBeInTheDocument();
    expect(window.localStorage.getItem('djedai.activeDataset')).toBe('ds_retail');
    await userEvent.click(picker);
    await userEvent.click(screen.getByRole('menuitem', { name: /Добавить источник/ }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/sources'); });
  });

  it('collapses the sidebar on tablets', async () => {
    const spy = vi.spyOn(window, 'matchMedia').mockImplementation((query) => ({
      matches: query === '(max-width: 1023px)', media: query, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
    await renderRoute('/sources');
    expect(await screen.findByRole('link', { name: 'Источники' })).not.toHaveTextContent('Источники');
    spy.mockRestore();
  });
});
