import { QueryClient, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { ApiError, request, type Schemas } from './client';
import { streamEvents, type ProfilingEventMap } from './sse';

declare module '@tanstack/react-query' {
  // Every query and mutation goes through request(), which only throws ApiError.
  interface Register {
    defaultError: ApiError;
  }
}

/** Query keys in one place, so mutations invalidate exactly what they change. */
export const keys = {
  datasets: ['datasets'] as const,
  dataset: (id: string) => ['datasets', id] as const,
  chats: (datasetId: string) => ['chats', datasetId] as const,
  dashboards: (datasetId: string) => ['dashboards', datasetId] as const,
  dashboard: (id: string) => ['dashboard', id] as const,
  result: (id: string) => ['result', id] as const,
  resultData: (id: string) => ['result', id, 'data'] as const,
};

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Client errors (404, 422) will not fix themselves; network and 5xx get one more try.
        retry: (failures, error) => failures < 1 && !(error instanceof ApiError && error.status >= 400 && error.status < 500),
        refetchOnWindowFocus: false,
        staleTime: 30_000,
      },
      mutations: { retry: false },
    },
  });
}

const enc = encodeURIComponent;

export function useDatasets() {
  return useQuery({ queryKey: keys.datasets, queryFn: ({ signal }) => request<Schemas['DatasetSummary'][]>('/datasets', { signal }) });
}

export function useDataset(id: string | null) {
  return useQuery({
    queryKey: keys.dataset(id ?? ''),
    queryFn: ({ signal }) => request<Schemas['DatasetDetail']>(`/datasets/${enc(id ?? '')}`, { signal }),
    enabled: id !== null,
  });
}

export function useDeleteDataset() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<undefined>(`/datasets/${enc(id)}`, { method: 'DELETE' }),
    onSuccess: (_data, id) => {
      client.removeQueries({ queryKey: keys.dataset(id) });
      return client.invalidateQueries({ queryKey: keys.datasets });
    },
  });
}

export function useUploadCsv() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (files: readonly File[]) => {
      const form = new FormData();
      for (const file of files) form.append('files', file);
      return request<Schemas['DatasetAccepted']>('/datasets/upload', { method: 'POST', form });
    },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.datasets }),
  });
}

export function useConnectDatabase() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Schemas['ConnectRequest']) => request<Schemas['DatasetAccepted']>('/datasets/connect', { method: 'POST', json: body }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.datasets }),
  });
}

export type ProfilingStep = Schemas['ProfilingStep'];

export interface ProfilingState {
  phase: 'idle' | 'running' | 'done' | 'error';
  file: string | null;
  steps: Partial<Record<ProfilingStep, { status: 'active' | 'done'; pct?: number | undefined }>>;
  error: ApiError | null;
}

const IDLE: ProfilingState = { phase: 'idle', file: null, steps: {}, error: null };
const RUNNING: ProfilingState = { ...IDLE, phase: 'running' };

/**
 * Follows /datasets/{id}/events while `id` is set. Leaving the page or switching the id
 * aborts the stream; when profiling ends the dataset list and catalog are refetched.
 */
export function useProfilingProgress(id: string | null): ProfilingState {
  const client = useQueryClient();
  const [state, setState] = useState<ProfilingState & { id: string | null }>({ ...IDLE, id: null });

  useEffect(() => {
    if (id === null) return;
    const controller = new AbortController();
    // State of a previous id is dropped on the first event of the new stream.
    const update = (change: (s: ProfilingState) => Partial<ProfilingState>) => {
      setState((s) => {
        const current = s.id === id ? s : { ...RUNNING, id };
        return { ...current, ...change(current) };
      });
    };
    const fail = (error: ApiError) => { update(() => ({ phase: 'error', error })); };
    streamEvents<ProfilingEventMap>(`/datasets/${enc(id)}/events`, {
      signal: controller.signal,
      onEvent: (message) => {
        if (message.event === 'progress') {
          const { step, status, pct, file } = message.data;
          update((s) => ({ file: file ?? s.file, steps: { ...s.steps, [step]: { status, pct } } }));
        } else if (message.event === 'done') {
          update(() => ({ phase: 'done' }));
        } else {
          fail(new ApiError(200, message.data));
        }
      },
    })
      .then(() => client.invalidateQueries({ queryKey: keys.datasets }))
      .catch((error: unknown) => { fail(error instanceof ApiError ? error : new ApiError(0, { code: 'unknown', message: String(error) })); });
    return () => { controller.abort(); };
  }, [id, client]);

  if (id === null) return IDLE;
  return state.id === id ? state : RUNNING;
}

export function useChats(datasetId: string | null) {
  return useQuery({
    queryKey: keys.chats(datasetId ?? ''),
    queryFn: ({ signal }) => request<Schemas['ChatSummary'][]>(`/chats?dataset_id=${enc(datasetId ?? '')}`, { signal }),
    enabled: datasetId !== null,
  });
}

export function useDashboards(datasetId: string | null) {
  return useQuery({
    queryKey: keys.dashboards(datasetId ?? ''),
    queryFn: ({ signal }) => request<Schemas['DashboardSummary'][]>(`/dashboards?dataset_id=${enc(datasetId ?? '')}`, { signal }),
    enabled: datasetId !== null,
  });
}

export function useDashboard(id: string | null) {
  return useQuery({
    queryKey: keys.dashboard(id ?? ''),
    queryFn: ({ signal }) => request<Schemas['Dashboard']>(`/dashboards/${enc(id ?? '')}`, { signal }),
    enabled: id !== null,
  });
}

export function useRefreshDashboard(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => request<Schemas['Dashboard']>(`/dashboards/${enc(id)}/refresh`, { method: 'POST' }),
    onSuccess: (dashboard) => {
      client.setQueryData(keys.dashboard(id), dashboard);
      return client.invalidateQueries({ queryKey: ['result'] });
    },
  });
}

export function useRemoveWidget(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (widgetId: string) => request<Schemas['Dashboard']>(`/dashboards/${enc(id)}`, { method: 'PATCH', json: { remove_widget_ids: [widgetId] } }),
    onSuccess: (dashboard) => { client.setQueryData(keys.dashboard(id), dashboard); },
  });
}

export function useSaveToDashboard() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Schemas['DashboardCreate']) => request<Schemas['Dashboard']>('/dashboards', { method: 'POST', json: body }),
    onSuccess: (dashboard) => {
      client.setQueryData(keys.dashboard(dashboard.dashboard_id), dashboard);
      return client.invalidateQueries({ queryKey: keys.dashboards(dashboard.dataset_id) });
    },
  });
}

export function useResult(id: string | null) {
  return useQuery({
    queryKey: keys.result(id ?? ''),
    queryFn: ({ signal }) => request<Schemas['ToolResultMeta']>(`/results/${enc(id ?? '')}`, { signal }),
    enabled: id !== null,
    staleTime: Infinity,
  });
}

export function useResultData(id: string | null) {
  return useQuery({
    queryKey: keys.resultData(id ?? ''),
    queryFn: ({ signal }) => request<Schemas['ResultData']>(`/results/${enc(id ?? '')}/data`, { signal }),
    enabled: id !== null,
    staleTime: Infinity,
  });
}
