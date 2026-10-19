import type { components } from './generated';

export type Schemas = components['schemas'];

/** Public backend base: /api by default (Vite proxy in dev, nginx in production). */
export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api').replace(/\/+$/, '');

/** Absolute URL of an API path; relative bases resolve against the page origin. */
export function apiUrl(path: string): string {
  return new URL(`${API_BASE}${path}`, window.location.origin).toString();
}

/** One error type for every failed request, so screens show errors the same way. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly suggestion: string | null;

  constructor(status: number, body: Schemas['ErrorBody']) {
    super(body.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code;
    this.suggestion = body.suggestion ?? null;
  }
}

const NETWORK_ERROR: Schemas['ErrorBody'] = {
  code: 'network',
  message: 'Нет связи с сервером',
  suggestion: 'Проверьте, что backend запущен, и повторите.',
};

function isErrorBody(value: unknown): value is Schemas['ErrorBody'] {
  return typeof value === 'object' && value !== null && typeof (value as { message?: unknown }).message === 'string';
}

/** Reads the error of a failed response: our { error } shape, FastAPI { detail } or plain status. */
export async function errorFrom(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Not JSON: fall back to the HTTP status below.
  }
  const payload = (body ?? {}) as { error?: unknown; detail?: unknown };
  if (isErrorBody(payload.error)) return new ApiError(response.status, payload.error);
  if (typeof payload.detail === 'string') return new ApiError(response.status, { code: `http_${response.status}`, message: payload.detail });
  return new ApiError(response.status, { code: `http_${response.status}`, message: `Сервер ответил ошибкой ${response.status}` });
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  /** JSON body; use `form` for multipart uploads. */
  json?: unknown;
  form?: FormData;
  signal?: AbortSignal | undefined;
  headers?: Record<string, string>;
}

/** Sends a request and returns the raw response; network failures and non-2xx become ApiError. */
export async function send(path: string, { method = 'GET', json, form, signal, headers = {} }: RequestOptions = {}): Promise<Response> {
  const allHeaders: Record<string, string> = { Accept: 'application/json', ...headers };
  const init: RequestInit = { method, headers: allHeaders };
  if (json !== undefined) {
    init.body = JSON.stringify(json);
    allHeaders['Content-Type'] = 'application/json';
  } else if (form) {
    init.body = form;
  }
  if (signal) init.signal = signal;
  let response: Response;
  try {
    response = await fetch(apiUrl(path), init);
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, NETWORK_ERROR);
  }
  if (!response.ok) throw await errorFrom(response);
  return response;
}

/** JSON request; 204 resolves to undefined. */
export async function request<T>(path: string, options?: RequestOptions): Promise<T> {
  const response = await send(path, options);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
