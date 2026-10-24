import { ApiError, errorFrom, apiUrl, type Schemas } from './client';
import { clearSessionIfCurrent, sessionSignal, sessionSnapshot } from './session';

export interface SseMessage {
  event: string;
  data: string;
  id?: string;
}

/**
 * Incremental text/event-stream parser. Chunks may split an event, a line or a UTF-8
 * character anywhere; complete events come out as soon as their blank line arrives.
 */
export class SseParser {
  private readonly decoder = new TextDecoder();
  private buffer = '';
  /** A trailing CR waits for the next chunk: it may be the first half of CRLF. */
  private pendingCR = false;

  push(chunk: Uint8Array | string): SseMessage[] {
    let text = (this.pendingCR ? '\r' : '') + (typeof chunk === 'string' ? chunk : this.decoder.decode(chunk, { stream: true }));
    this.pendingCR = text.endsWith('\r');
    if (this.pendingCR) text = text.slice(0, -1);
    this.buffer += text.replace(/\r\n?/g, '\n');
    const messages: SseMessage[] = [];
    let boundary = this.buffer.indexOf('\n\n');
    while (boundary !== -1) {
      const block = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const message = parseBlock(block);
      if (message) messages.push(message);
      boundary = this.buffer.indexOf('\n\n');
    }
    return messages;
  }
}

function parseBlock(block: string): SseMessage | null {
  let event = 'message';
  let id: string | undefined;
  const data: string[] = [];
  for (const line of block.split('\n')) {
    if (line === '' || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
    else if (field === 'id') id = value;
  }
  if (data.length === 0) return null;
  return id === undefined ? { event, data: data.join('\n') } : { event, data: data.join('\n'), id };
}

/** Event name → JSON payload of a stream (an interface without an index signature). */
export type EventMap = object;
export type StreamEvent<M extends EventMap> = { [K in keyof M & string]: { event: K; data: M[K] } }[keyof M & string];

/** POST /chats/{id}/messages (specification §7.3). */
export interface AgentEventMap {
  status: Schemas['AgentStatus'];
  tool_call: Schemas['AgentToolCall'];
  tool_result: Schemas['AgentToolResult'];
  block: Schemas['Block'];
  done: Schemas['AgentDone'];
  error: Schemas['StreamError'];
}

/** GET /datasets/{id}/events. */
export interface ProfilingEventMap {
  progress: Schemas['ProfilingProgress'];
  done: Schemas['ProfilingDone'];
  error: Schemas['StreamError'];
}

export type StreamOutcome = 'done' | 'error' | 'aborted';

interface StreamOptions<M extends EventMap> {
  onEvent: (event: StreamEvent<M>) => void;
  method?: 'GET' | 'POST';
  json?: unknown;
  signal?: AbortSignal | undefined;
}

export const STREAM_INTERRUPTED: Schemas['ErrorBody'] = {
  code: 'stream_interrupted',
  message: 'Соединение прервано до конца ответа',
  suggestion: 'Запрос не повторяется автоматически, чтобы не запускать агента второй раз. Отправьте его снова, если нужно.',
};

/**
 * Reads an SSE stream over fetch (works for POST, unlike EventSource).
 * Resolves with `done` / `error` when the server sends that event, or `aborted` after
 * the signal fires. A stream that ends without either rejects with STREAM_INTERRUPTED;
 * the request is never retried here, so a dropped connection cannot run the agent twice.
 */
export async function streamEvents<M extends EventMap>(path: string, { onEvent, method = 'GET', json, signal: callerSignal }: StreamOptions<M>): Promise<StreamOutcome> {
  const session = sessionSnapshot();
  const signal = sessionSignal(session, callerSignal);
  // Read on each boundary: the signal can change while fetch/read/callbacks run.
  const aborted = () => signal.aborted;
  const headers: Record<string, string> = { Accept: 'text/event-stream' };
  if (session.accessToken) headers.Authorization = `Bearer ${session.accessToken}`;
  const init: RequestInit = { method, headers, signal };
  if (json !== undefined) {
    init.body = JSON.stringify(json);
    headers['Content-Type'] = 'application/json';
  }

  let response: Response;
  try {
    response = await fetch(apiUrl(path), init);
  } catch (error) {
    if (aborted()) return 'aborted';
    if (error instanceof DOMException && error.name === 'AbortError') return 'aborted';
    throw new ApiError(0, { code: 'network', message: 'Нет связи с сервером' });
  }
  if (!response.ok) {
    const error = await errorFrom(response);
    if (response.status === 401 || response.status === 403) clearSessionIfCurrent(session, response.status === 401 ? 'expired' : 'forbidden');
    throw error;
  }
  if (aborted()) return 'aborted';
  if (!response.body) throw new ApiError(response.status, STREAM_INTERRUPTED);

  const reader = response.body.getReader();
  const parser = new SseParser();
  // Leaving the page aborts the signal; cancelling the reader ends a read that is still waiting.
  const stop = () => { reader.cancel().catch(() => undefined); };
  signal.addEventListener('abort', stop, { once: true });
  try {
    for (;;) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (error) {
        if (aborted()) return 'aborted';
        throw error instanceof ApiError ? error : new ApiError(response.status, STREAM_INTERRUPTED);
      }
      if (chunk.done) break;
      for (const message of parser.push(chunk.value)) {
        if (aborted()) return 'aborted';
        let data: unknown;
        try {
          data = JSON.parse(message.data);
        } catch {
          throw new ApiError(response.status, { code: 'bad_event', message: `Сервер прислал повреждённое событие «${message.event}»` });
        }
        onEvent({ event: message.event, data } as StreamEvent<M>);
        if (message.event === 'error' && typeof data === 'object' && data !== null
            && 'code' in data && (data.code === 'unauthorized' || data.code === 'forbidden')) clearSessionIfCurrent(session, data.code === 'unauthorized' ? 'expired' : 'forbidden');
        if (message.event === 'done' || message.event === 'error') return message.event;
      }
    }
  } finally {
    signal.removeEventListener('abort', stop);
    // Stops the network read on done/error; ignored if the stream already closed.
    reader.cancel().catch(() => undefined);
  }
  if (aborted()) return 'aborted';
  throw new ApiError(response.status, STREAM_INTERRUPTED);
}
