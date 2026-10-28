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
  chat: (id: string) => ['chat', id] as const,
  dashboards: (datasetId: string) => ['dashboards', datasetId] as const,
  dashboard: (id: string) => ['dashboard', id] as const,
  result: (id: string) => ['result', id] as const,
  resultData: (id: string) => ['result', id, 'data'] as const,
  modelSettings: ['settings', 'model'] as const,
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
  return useQuery({
    queryKey: keys.datasets,
    queryFn: ({ signal }) => request<Schemas['DatasetSummary'][]>('/datasets', { signal }),
    refetchInterval: (query) => query.state.data?.some((dataset) => dataset.status === 'processing') ? 2000 : false,
  });
}

export function useDataset(id: string | null) {
  return useQuery({
    queryKey: keys.dataset(id ?? ''),
    queryFn: ({ signal }) => request<Schemas['DatasetDetail']>(`/datasets/${enc(id ?? '')}`, { signal }),
    enabled: id !== null,
    // Re-profiling runs in the background while the dataset stays ready.
    refetchInterval: (query) => query.state.data?.refresh?.status === 'processing' ? 2000 : false,
  });
}

/** Re-profile a ready dataset into the next catalog revision (B1-11). */
export function useRefreshDataset() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<Schemas['DatasetDetail']>(`/datasets/${enc(id)}/refresh`, { method: 'POST' }),
    onSuccess: (detail) => {
      client.setQueryData(keys.dataset(detail.dataset_id), detail);
      return client.invalidateQueries({ queryKey: keys.datasets });
    },
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

// The DSN carries a password: its mutations leave the cache as soon as nothing observes
// them (reset or unmount), so the secret lives no longer than the form shows it.
export function useConnectDatabase() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Schemas['ConnectRequest']) => request<Schemas['DatasetAccepted']>('/datasets/connect', { method: 'POST', json: body }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.datasets }),
    gcTime: 0,
  });
}

/** Lists tables of a read-only source before connecting; the server stores nothing. */
export function useCheckDatabase() {
  return useMutation({
    mutationFn: (body: Schemas['SourceCheckRequest']) => request<Schemas['SourceCheckResult']>('/datasets/connect/check', { method: 'POST', json: body }),
    gcTime: 0,
  });
}

/** Effective model settings (#209); the API key itself never comes back. */
export function useModelSettings() {
  return useQuery({
    queryKey: keys.modelSettings,
    queryFn: ({ signal }) => request<Schemas['ModelSettingsRead']>('/settings/model', { signal }),
  });
}

// Both mutations may carry the API key: like the DSN, it leaves the cache with the form.
export function useUpdateModelSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Schemas['ModelSettingsUpdate']) => request<Schemas['ModelSettingsRead']>('/settings/model', { method: 'PUT', json: body }),
    onSuccess: (settings) => { client.setQueryData(keys.modelSettings, settings); },
    gcTime: 0,
  });
}

/** One short request to the model with the entered values; the server stores nothing. */
export function useCheckModelSettings() {
  return useMutation({
    mutationFn: (body: Schemas['ModelSettingsUpdate']) => request<Schemas['ModelCheckResult']>('/settings/model/check', { method: 'POST', json: body }),
    gcTime: 0,
  });
}

export type ProfilingStep = Schemas['ProfilingStep'];

export interface ProfilingState {
  phase: 'idle' | 'running' | 'done' | 'error';
  message: string | null;
  steps: Partial<Record<string, { status: 'active' | 'done'; pct?: number | undefined }>>;
  error: ApiError | null;
}

const IDLE: ProfilingState = { phase: 'idle', message: null, steps: {}, error: null };
const RUNNING: ProfilingState = { ...IDLE, phase: 'running' };

/**
 * Follows /datasets/{id}/events while `id` is set. Leaving the page or switching the id
 * aborts the stream; when profiling ends the dataset list and catalog are refetched.
 */
export function useProfilingProgress(id: string | null, attempt: string | number = 0): ProfilingState {
  const client = useQueryClient();
  const [state, setState] = useState<ProfilingState & { id: string | null; attempt: string | number }>({ ...IDLE, id: null, attempt: 0 });

  useEffect(() => {
    if (id === null) return;
    const controller = new AbortController();
    // A new observation starts fresh, including retrying the same dataset after a disconnect.
    const update = (change: (s: ProfilingState) => Partial<ProfilingState>) => {
      if (controller.signal.aborted) return;
      setState((s) => {
        const current = s.id === id && s.attempt === attempt ? s : { ...RUNNING, id, attempt };
        return { ...current, ...change(current) };
      });
    };
    const fail = (error: ApiError) => { update(() => ({ phase: 'error', error })); };
    streamEvents<ProfilingEventMap>(`/datasets/${enc(id)}/events`, {
      signal: controller.signal,
      onEvent: (message) => {
        if (message.event === 'progress') {
          const { stage, progress, message: text } = message.data;
          update((s) => ({
            phase: 'running', error: null,
            message: text ?? null,
            steps: {
              ...Object.fromEntries(Object.entries(s.steps).map(([key, step]) => [key, key === stage ? step : { status: 'done' as const, pct: 100 }])),
              [stage]: { status: progress === 1 ? 'done' : 'active', pct: Math.round(progress * 100) },
            },
          }));
        } else if (message.event === 'done') {
          update((s) => ({ phase: 'done', error: null, steps: Object.fromEntries(Object.keys(s.steps).map((key) => [key, { status: 'done' as const, pct: 100 }])) }));
        } else {
          fail(new ApiError(200, { code: 'profiling_failed', message: message.data.message, suggestion: message.data.detail ?? null }));
        }
      },
    })
      .then(async (outcome) => {
        if (outcome !== 'aborted') await Promise.all([
          client.invalidateQueries({ queryKey: keys.datasets }),
          client.invalidateQueries({ queryKey: keys.dataset(id) }),
        ]);
      })
      .catch((error: unknown) => { fail(error instanceof ApiError ? error : new ApiError(0, { code: 'unknown', message: String(error) })); });
    return () => { controller.abort(); };
  }, [id, attempt, client]);

  if (id === null) return IDLE;
  return state.id === id && state.attempt === attempt ? state : RUNNING;
}

export function useChats(datasetId: string | null) {
  return useQuery({
    queryKey: keys.chats(datasetId ?? ''),
    queryFn: ({ signal }) => request<Schemas['ChatSummary'][]>(`/chats?dataset_id=${enc(datasetId ?? '')}`, { signal }),
    enabled: datasetId !== null,
  });
}

export function useChat(id: string | null) {
  return useQuery({
    queryKey: keys.chat(id ?? ''),
    queryFn: ({ signal }) => request<Schemas['ChatDetail']>(`/chats/${enc(id ?? '')}`, { signal }),
    enabled: id !== null,
    // A revisit recovers the saved history after a stopped or disconnected POST.
    staleTime: 0,
    refetchInterval: (query) => query.state.data?.messages.some((message) => message.turn?.status === 'running') ? 1000 : false,
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
