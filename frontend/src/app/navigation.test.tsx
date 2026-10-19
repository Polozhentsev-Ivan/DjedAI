import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderRoute } from '@/test/renderRoute';

async function selectRetail() {
  await userEvent.click(screen.getByRole('button', { name: 'Активный датасет' }));
  await userEvent.click(screen.getByRole('menuitem', { name: /retail_crm_prod/ }));
}

describe('dataset context and explicit routes', () => {
  it('uses a directly opened catalog dataset in the following chat', async () => {
    const { router } = await renderRoute('/catalog/ds_retail');
    await screen.findByRole('heading', { name: 'Каталог датасета: retail_crm_prod' });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Активный датасет' })).toHaveTextContent('retail_crm_prod');
    });
    expect(window.localStorage.getItem('djedai.activeDataset')).toBe('ds_retail');
    await userEvent.click(screen.getByRole('button', { name: /Задать вопрос в чате/ }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/chat'); });
    expect(await screen.findByText(/Задайте вопрос к датасету retail_crm_prod/)).toBeInTheDocument();
  });

  it('changes an explicit catalog URL when the dataset picker changes', async () => {
    const { router } = await renderRoute('/catalog/ds_orders');
    await screen.findByRole('heading', { name: 'Каталог датасета: orders_2026' });
    await selectRetail();
    await waitFor(() => { expect(router.state.location.pathname).toBe('/catalog/ds_retail'); });
    expect(await screen.findByRole('heading', { name: 'Каталог датасета: retail_crm_prod' })).toBeInTheDocument();
  });

  it('opens a dashboard outside the remembered dataset and restores its context', async () => {
    window.localStorage.setItem('djedai.activeDataset', 'ds_retail');
    const { router } = await renderRoute('/dashboards/d_q');
    expect(await screen.findByRole('heading', { name: 'Дашборд: Квартальная статистика продаж' })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Активный датасет' })).toHaveTextContent('orders_2026');
    });
    expect(router.state.location.pathname).toBe('/dashboards/d_q');
    expect(window.localStorage.getItem('djedai.activeDataset')).toBe('ds_orders');
    expect(screen.queryByText('У вас ещё нет дашбордов')).not.toBeInTheDocument();
  });

  it('reports a missing explicit dashboard instead of opening the first one', async () => {
    const { router } = await renderRoute('/dashboards/missing');
    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось открыть дашборд');
    expect(screen.queryByRole('heading', { name: 'Дашборд: Квартальная статистика продаж' })).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/dashboards/missing');
  });

  it('leaves an explicit dashboard when switching to a dataset without dashboards', async () => {
    const { router } = await renderRoute('/dashboards/d_q');
    await screen.findByRole('heading', { name: 'Дашборд: Квартальная статистика продаж' });
    await selectRetail();
    await waitFor(() => { expect(router.state.location.pathname).toBe('/dashboards'); });
    expect(await screen.findByText('У вас ещё нет дашбордов')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Активный датасет' })).toHaveTextContent('retail_crm_prod');
  });

  it('leaves the previous dataset chat when the dataset picker changes', async () => {
    const { router } = await renderRoute('/chat/c_q1');
    await screen.findByRole('heading', { name: 'Продажи Q1' });
    await selectRetail();
    await waitFor(() => { expect(router.state.location.pathname).toBe('/chat'); });
    expect(await screen.findByRole('heading', { name: 'Новый чат' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Активный датасет' })).toHaveTextContent('retail_crm_prod');
  });
});
