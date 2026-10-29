import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw/http';
import type { Schemas } from '@/api/client';
import { testQueryClient } from '@/test/renderRoute';
import { server } from '@/test/server';
import { SavedResult } from './SavedResult';

const api = (path: string) => `http://localhost:3000/api${path}`;
const renderResult = (id = 'r_saved') => render(<QueryClientProvider client={testQueryClient()}><SavedResult id={id} /></QueryClientProvider>);
const metadata = (body: Schemas['ToolResultMeta']) => { server.use(http.get(api('/results/r_saved'), () => HttpResponse.json(body))); };
const rows = (body: Schemas['ResultData']) => { server.use(http.get(api('/results/r_saved/data'), () => HttpResponse.json(body))); };

describe('SavedResult', () => {
  it('reads string columns, shows saved SQL and limits the preview to 20 rows', async () => {
    metadata({ result_id: 'r_saved', status: 'ok', tool: 'run_sql', sql: 'SELECT id FROM orders', params: { sql: 'SELECT id FROM orders' }, source_result_id: 'r_source' });
    rows({ columns: ['id', 'value'], rows: Array.from({ length: 25 }, (_, i) => [`id-${i}`, i]), row_count: 25, truncated: false });
    renderResult();
    await userEvent.click(screen.getByText('Результат расчёта r_saved'));
    const table = await screen.findByRole('table', { name: 'Данные результата r_saved' });
    expect(within(table).getAllByRole('row')).toHaveLength(21);
    expect(screen.getByText('SELECT id FROM orders')).toBeInTheDocument();
    expect(screen.getByText('run_sql')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Параметры' })).toHaveTextContent("sql = 'SELECT id FROM orders'");
    expect(screen.getByRole('link', { name: 'r_source' })).toHaveAttribute('href', api('/results/r_source'));
    expect(screen.getByText(/Показаны первые 20 строк/)).toBeInTheDocument();
  });

  it('renders a persisted empty/error result without inventing rows', async () => {
    metadata({ result_id: 'r_saved', status: 'error', error: { code: 'sql', message: 'Ошибка сохранённого расчёта', suggestion: 'Проверьте параметры' } });
    rows({ columns: [], rows: [], row_count: 0 });
    renderResult();
    await userEvent.click(screen.getByText('Результат расчёта r_saved'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Проверьте параметры');
    expect(await screen.findByText('Результат не содержит строк')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Повторить' })).not.toBeInTheDocument();
  });

  it('reports independent metadata and data HTTP failures and allows their GET retries', async () => {
    metadata({ result_id: 'r_saved', status: 'empty' });
    rows({ columns: ['total'], rows: [[42]] });
    server.use(
      http.get(api('/results/r_saved'), () => HttpResponse.json({ error: { code: 'failed', message: 'Метаданные недоступны' } }, { status: 503 }), { once: true }),
      http.get(api('/results/r_saved/data'), () => HttpResponse.json({ error: { code: 'failed', message: 'Данные недоступны' } }, { status: 503 }), { once: true }),
    );
    renderResult();
    await userEvent.click(screen.getByText('Результат расчёта r_saved'));
    const alerts = await screen.findAllByRole('alert');
    expect(alerts).toHaveLength(2);
    for (const alert of alerts) await userEvent.click(within(alert).getByRole('button', { name: 'Повторить' }));
    await waitFor(() => { expect(screen.queryByRole('alert')).not.toBeInTheDocument(); });
    expect(await screen.findByText('42')).toBeInTheDocument();
  });
});
