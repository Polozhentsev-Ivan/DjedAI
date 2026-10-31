import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createDb } from '@/mocks/db';
import { createHandlers } from '@/mocks/handlers';
import { server, useScenario } from '@/test/server';
import { ApiError } from './client';
import {
  createQueryClient, useChats, useConnectDatabase, useDashboard, useDashboards, useDataset, useDatasets, useDeleteDataset,
  useProfilingProgress, useRefreshDashboard, useRemoveWidget, useResult, useResultData, useSaveToDashboard, useUploadCsv,
} from './queries';

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { client, wrapper };
}

const csv = (name: string) => new File(['a,b\n1,2'], name, { type: 'text/csv' });

describe('query client', () => {
  it('retries server errors once and client errors never', () => {
    const retry = createQueryClient().getDefaultOptions().queries?.retry as (n: number, e: unknown) => boolean;
    expect(retry(0, new ApiError(503, { code: 'x', message: 'x' }))).toBe(true);
    expect(retry(1, new ApiError(503, { code: 'x', message: 'x' }))).toBe(false);
    expect(retry(0, new ApiError(404, { code: 'x', message: 'x' }))).toBe(false);
  });
});

describe('dataset queries', () => {
  it('lists datasets and reads a catalog', async () => {
    const { wrapper } = setup();
    const list = renderHook(() => useDatasets(), { wrapper });
    await waitFor(() => { expect(list.result.current.data?.map((d) => d.name)).toEqual(['orders_2026', 'retail_crm_prod', 'marketing_q3']); });
    const one = renderHook(() => useDataset('ds_orders'), { wrapper });
    await waitFor(() => { expect(one.result.current.data?.catalog?.tables).toHaveLength(2); });
    expect(renderHook(() => useDataset(null), { wrapper }).result.current.fetchStatus).toBe('idle');
  });

  it('reports a missing dataset as a 404 ApiError', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => useDataset('nope'), { wrapper });
    await waitFor(() => { expect(result.current.error?.status).toBe(404); });
  });

  it('deletes a dataset and refreshes the list', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => ({ list: useDatasets(), remove: useDeleteDataset() }), { wrapper });
    await waitFor(() => { expect(result.current.list.data).toHaveLength(3); });
    act(() => { result.current.remove.mutate('ds_marketing'); });
    await waitFor(() => { expect(result.current.list.data).toHaveLength(2); });
  });

  it('uploads files and follows profiling until the dataset is ready', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => {
      const upload = useUploadCsv();
      return { upload, progress: useProfilingProgress(upload.data?.dataset_id ?? null), list: useDatasets() };
    }, { wrapper });
    expect(result.current.progress.phase).toBe('idle');
    act(() => { result.current.upload.mutate([csv('sales.csv'), csv('b.csv')]); });
    await waitFor(() => { expect(result.current.progress.phase).toBe('done'); });
    expect(result.current.progress.steps.profile?.status).toBe('done');
    expect(result.current.progress.message).toBe('Профилирование завершено');
    await waitFor(() => { expect(result.current.list.data?.find((d) => d.name === 'sales')?.status).toBe('ready'); });
  });

  it('rejects an upload without files', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => useUploadCsv(), { wrapper });
    act(() => { result.current.mutate([]); });
    await waitFor(() => { expect(result.current.error?.code).toBe('no_files'); });
  });

  it('surfaces a profiling error from the stream', async () => {
    useScenario('error');
    const { wrapper } = setup();
    const { result } = renderHook(() => {
      const upload = useUploadCsv();
      return { upload, progress: useProfilingProgress(upload.data?.dataset_id ?? null) };
    }, { wrapper });
    act(() => { result.current.upload.mutate([csv('sales.csv')]); });
    await waitFor(() => { expect(result.current.progress.phase).toBe('error'); });
    expect(result.current.progress.error?.message).toContain('не удалось разобрать строку 3');
  });

  it('connects a database and validates the DSN', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => useConnectDatabase(), { wrapper });
    act(() => { result.current.mutate({ kind: 'postgres', dsn: 'oops' }); });
    await waitFor(() => { expect(result.current.error?.code).toBe('bad_dsn'); });
    act(() => { result.current.mutate({ kind: 'postgres', dsn: 'postgresql://u:p@h:5432/db', tables: ['orders'] }); });
    await waitFor(() => { expect(result.current.data?.status).toBe('processing'); });
  });

  it('serves empty lists in the empty scenario', async () => {
    useScenario('empty');
    const { wrapper } = setup();
    const { result } = renderHook(() => ({ datasets: useDatasets(), dashboards: useDashboards('ds_orders'), chats: useChats('ds_orders') }), { wrapper });
    await waitFor(() => { expect(result.current.datasets.data).toEqual([]); });
    await waitFor(() => { expect(result.current.dashboards.data).toEqual([]); });
    await waitFor(() => { expect(result.current.chats.data).toEqual([]); });
  });
});

describe('chat, result and dashboard queries', () => {
  it('lists chats of a dataset', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => useChats('ds_orders'), { wrapper });
    await waitFor(() => { expect(result.current.data?.map((c) => c.title)).toEqual(['Продажи Q1', 'Регионы', 'Сегменты']); });
  });

  it('reads result metadata and rows', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => ({ meta: useResult('r_rev'), data: useResultData('r_rev'), missing: useResultData('r_x') }), { wrapper });
    await waitFor(() => { expect(result.current.meta.data?.tool).toBe('time_aggregate'); });
    await waitFor(() => { expect(result.current.data.data?.rows).toHaveLength(10); });
    await waitFor(() => { expect(result.current.missing.error?.status).toBe(404); });
  });

  it('opens, refreshes and edits a dashboard', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => ({
      list: useDashboards('ds_orders'), dashboard: useDashboard('d_q'), refresh: useRefreshDashboard('d_q'), remove: useRemoveWidget('d_q'),
    }), { wrapper });
    await waitFor(() => { expect(result.current.list.data).toHaveLength(3); });
    await waitFor(() => { expect(result.current.dashboard.data?.widgets).toHaveLength(4); });
    act(() => { result.current.refresh.mutate(); });
    await waitFor(() => { expect(result.current.refresh.isSuccess).toBe(true); });
    act(() => { result.current.remove.mutate('w_kpi'); });
    await waitFor(() => { expect(result.current.dashboard.data?.widgets).toHaveLength(3); });
  });

  it('marks one failing widget in the error scenario', async () => {
    useScenario('error');
    const { wrapper } = setup();
    const { result } = renderHook(() => useDashboard('d_q'), { wrapper });
    await waitFor(() => { expect(result.current.data?.widgets.find((w) => w.widget_id === 'w_cat')?.status).toBe('error'); });
  });

  it('saves a chart to an existing or a new dashboard', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => useSaveToDashboard(), { wrapper });
    const chart = { chart_id: 'chart_revenue', result_id: 'r_rev', type: 'line', title: 'Выручка' } as const;
    act(() => { result.current.mutate({ dataset_id: 'ds_orders', dashboard_id: 'd_q', result_id: 'r_rev', chart }); });
    await waitFor(() => { expect(result.current.data?.widgets).toHaveLength(5); });
    act(() => { result.current.mutate({ dataset_id: 'ds_orders', result_id: 'r_rev', chart, title: 'Новый' }); });
    await waitFor(() => { expect(result.current.data?.title).toBe('Новый'); });
  });

  it('keeps a chained result as a recipe and a single call as a tool call, without temporary ids', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => useSaveToDashboard(), { wrapper });
    const chart = { chart_id: 'chart_top', result_id: 'r_top', type: 'bar', title: 'Лучшие месяцы', subtitle: 'Топ-3 по выручке' } as const;
    act(() => { result.current.mutate({ dataset_id: 'ds_orders', result_id: 'r_top', chart }); });
    await waitFor(() => { expect(result.current.data?.title).toBe('Лучшие месяцы'); });
    const [chained] = result.current.data!.widgets;
    expect(chained).toMatchObject({ caption: 'Топ-3 по выручке', result_id: 'r_top', layout: { x: 0, y: 0, w: 6, h: 4 } });
    expect(chained?.tool_call).toBeUndefined();
    expect(chained?.recipe).toEqual({ schema_version: 1, output_step_id: 'step_2', steps: [
      { step_id: 'step_1', source_step_id: null, call: { tool: 'time_aggregate', params: { table: 'orders', freq: 'month' } } },
      { step_id: 'step_2', source_step_id: 'step_1', call: { tool: 'top_n', params: { by: 'revenue', n: 3 } } },
    ] });
    const id = result.current.data!.dashboard_id;
    act(() => { result.current.mutate({ dataset_id: 'ds_orders', dashboard_id: id, result_id: 'r_rev', chart: { ...chart, result_id: 'r_rev' } }); });
    await waitFor(() => { expect(result.current.data?.widgets).toHaveLength(2); });
    expect(result.current.data?.widgets[1]).toMatchObject({ tool_call: { tool: 'time_aggregate' }, layout: { y: 4 } });
  });

  it('refuses a missing result, a broken chain and a dashboard of another dataset', async () => {
    const db = createDb();
    db.results.r_top = { ...db.results.r_top!, source_result_id: 'r_gone' };
    server.resetHandlers(...createHandlers({ db, tick: 0 }));
    const { wrapper } = setup();
    const { result } = renderHook(() => useSaveToDashboard(), { wrapper });
    const chart = { chart_id: 'c', result_id: 'r_none', type: 'bar', title: 'График' } as const;
    act(() => { result.current.mutate({ dataset_id: 'ds_orders', result_id: 'r_none', chart }); });
    await waitFor(() => { expect(result.current.error?.status).toBe(404); });
    act(() => { result.current.mutate({ dataset_id: 'ds_orders', result_id: 'r_top', chart: { ...chart, result_id: 'r_top' } }); });
    await waitFor(() => { expect(result.current.error?.status).toBe(422); });
    act(() => { result.current.mutate({ dataset_id: 'ds_other', dashboard_id: 'd_q', result_id: 'r_rev', chart: { ...chart, result_id: 'r_rev' } }); });
    await waitFor(() => { expect(result.current.error?.message).toBe('Дашборд не найден'); });
  });

  it('reports a missing dashboard', async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => ({ one: useDashboard('d_none'), refresh: useRefreshDashboard('d_none'), remove: useRemoveWidget('d_none') }), { wrapper });
    await waitFor(() => { expect(result.current.one.error?.status).toBe(404); });
    act(() => { result.current.refresh.mutate(); result.current.remove.mutate('w'); });
    await waitFor(() => { expect(result.current.refresh.error?.status).toBe(404); });
    await waitFor(() => { expect(result.current.remove.error?.status).toBe(404); });
  });
});
