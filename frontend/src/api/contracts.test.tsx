import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import type { ReactNode } from 'react';
import { TransformLogItem } from '@/components/TransformLogItem';
import { ordersCatalog } from '@/mocks/db';
import { server } from '@/test/server';
import type { Schemas } from './client';
import { useProfilingProgress } from './queries';
import { calculation, journal, kpiView } from './view';

it('renders a transformation with structured details from the shared contract', () => {
  const transformation: Schemas['Transformation'] = {
    table: 'orders', column: 'amount', action: 'parse_money',
    detail: { decimal: ',', currency: '₽', errors: { count: 0 } },
  };
  const entries = journal({ ...ordersCatalog, transformations: [transformation], warnings: [] });
  render(<>{entries.map((entry) => <TransformLogItem key={entry.text} {...entry} />)}</>);
  expect(screen.getByText(/decimal: ,/)).toHaveTextContent('currency: ₽');
  expect(screen.getByText(/decimal: ,/)).toHaveTextContent('errors: {"count":0}');
});

it('uses string columns in KPI and calculation views as returned by run_sql', () => {
  const data: Schemas['ResultData'] = { columns: ['amount'], rows: [[42]] };
  const result: Schemas['ToolResultMeta'] = { result_id: 'r', status: 'ok', data };
  expect(calculation(result).columns).toEqual([{ label: 'amount', name: 'amount' }]);
  expect(kpiView({ chart_id: 'chart_kpi', result_id: 'r', type: 'kpi', title: 'Сумма', y: [{ field: 'amount' }] }, data, result)?.value).toBe('42');
});

it('maps canonical stage/progress/message SSE payloads, including new stages and done', async () => {
  let stream: ReadableStreamDefaultController<Uint8Array> | undefined;
  server.use(http.get('/api/datasets/ds_live/events', () => new HttpResponse(new ReadableStream<Uint8Array>({
    start(controller) { stream = controller; },
  }), { headers: { 'Content-Type': 'text/event-stream' } })));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result } = renderHook(() => useProfilingProgress('ds_live'), { wrapper });
  await waitFor(() => { expect(stream).toBeDefined(); });
  const emit = (event: string, payload: unknown) => {
    act(() => { stream?.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`)); });
  };
  const progress: Schemas['ProfilingProgress'] = { stage: 'profile', progress: 0.5, message: 'Профилирование' };
  emit('progress', progress);
  await waitFor(() => { expect(result.current.steps.profile).toEqual({ status: 'active', pct: 50 }); });
  expect(result.current.message).toBe('Профилирование');
  expect(result.current.steps).not.toHaveProperty('undefined');
  emit('progress', { stage: 'relations', progress: 0.25, message: 'Поиск связей' } satisfies Schemas['ProfilingProgress']);
  await waitFor(() => { expect(result.current.steps.relations).toEqual({ status: 'active', pct: 25 }); });
  expect(result.current.steps.profile?.status).toBe('done');
  emit('done', { dataset_id: 'ds_live' } satisfies Schemas['ProfilingDone']);
  await waitFor(() => { expect(result.current.phase).toBe('done'); });
  expect(result.current.steps.relations?.status).toBe('done');
});
