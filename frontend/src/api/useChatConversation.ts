import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ApiError, request, type Schemas } from './client';
import { keys, useChat } from './queries';
import { isCurrentSession, sessionSnapshot, subscribeSession, type SessionSnapshot } from './session';
import { streamEvents, type AgentEventMap } from './sse';

interface LocalTurn {
  chatId: string | null;
  datasetId: string;
  messageId: string;
  question: string;
  createdAt: string;
  blocks: Schemas['Block'][];
  resultIds: string[];
  status: string;
  running: boolean;
  error: ApiError | null;
  assistantId: string | null;
}

interface Run {
  controller: AbortController;
  chatId: string | null;
  datasetId: string;
  messageId: string;
  session: SessionSnapshot;
  accepted: boolean;
  stopRequested: boolean;
  cancelling: boolean;
  pendingNavigation?: boolean;
  stopTimer?: ReturnType<typeof setTimeout>;
}

const failure = (error: unknown) => error instanceof ApiError ? error : new ApiError(0, { code: 'unknown', message: 'Не удалось получить ответ сервера' });
const stopped = () => new ApiError(0, { code: 'cancelled', message: 'Получение ответа остановлено', suggestion: 'Запрос не повторяется автоматически. Отправьте его снова, если нужно.' });

/** Explicit POSTs only. Question identities survive SSE loss, refetch and repeated text. */
export function useChatConversation(chatId: string | null, datasetId: string | null, onCreated: (id: string) => void) {
  const client = useQueryClient();
  const history = useChat(chatId);
  const run = useRef<Run | null>(null);
  const context = useRef({ chatId, datasetId });
  context.current = { chatId, datasetId };
  const [turns, setTurns] = useState<LocalTurn[]>([]);
  // A late stream/cancel completion must never release a newer turn.
  const finish = useCallback((current: Run, closeStream = true) => {
    if (run.current !== current) return false;
    run.current = null;
    clearTimeout(current.stopTimer);
    setTurns((values) => values.map((value) => value.messageId === current.messageId ? { ...value, running: false } : value));
    if (closeStream) current.controller.abort();
    return true;
  }, []);

  useLayoutEffect(() => {
    const current = run.current;
    if (current?.chatId === chatId) current.pendingNavigation = false;
    if (current && (current.chatId !== chatId || current.datasetId !== datasetId)) {
      current.controller.abort();
      clearTimeout(current.stopTimer);
      run.current = null;
    }
    setTurns((values) => values.filter((value) => value.chatId === chatId && value.datasetId === datasetId));
  }, [chatId, datasetId]);

  useLayoutEffect(() => {
    const current = run.current;
    if (!current || current.chatId !== chatId || current.datasetId !== datasetId || !isCurrentSession(current.session)) return;
    // Saved terminal state is authoritative even while the last SSE is delayed.
    const state = history.data?.messages.find((message) => message.message_id === current.messageId)?.turn;
    if (state && state.status !== 'running' && finish(current)) {
      void client.invalidateQueries({ queryKey: keys.chats(current.datasetId) });
    }
  }, [history.data, chatId, datasetId, client, finish]);
  useEffect(() => {
    const clear = () => {
      run.current?.controller.abort();
      clearTimeout(run.current?.stopTimer);
      run.current = null;
      setTurns([]);
    };
    const unsubscribe = subscribeSession(clear);
    return () => { unsubscribe(); run.current?.controller.abort(); clearTimeout(run.current?.stopTimer); run.current = null; };
  }, []);

  const inContext = (current: Run) => isCurrentSession(current.session)
    && (context.current.chatId === current.chatId || (current.pendingNavigation && context.current.chatId === null))
    && context.current.datasetId === current.datasetId;
  const update = (current: Run, change: (value: LocalTurn) => LocalTurn) => {
    if (inContext(current)) setTurns((values) => values.map((value) => value.messageId === current.messageId ? change(value) : value));
  };
  const recover = async (current: Run) => {
    if (!current.chatId || !inContext(current)) return;
    try {
      await client.query({
        queryKey: keys.chat(current.chatId), staleTime: 0,
        queryFn: ({ signal }) => {
          if (!inContext(current)) throw new DOMException('Request aborted', 'AbortError');
          return request<Schemas['ChatDetail']>(`/chats/${encodeURIComponent(current.chatId ?? '')}`, { signal });
        },
      });
      if (inContext(current)) void client.invalidateQueries({ queryKey: keys.chats(current.datasetId) });
    } catch {
      // Keep the local question and its outcome until a later successful GET.
    }
  };
  const cancelRun = async (current: Run) => {
    if (current.cancelling || !current.chatId || !inContext(current)) return;
    current.cancelling = true;
    clearTimeout(current.stopTimer);
    try {
      const state = await request<Schemas['ChatTurnState']>(`/chats/${encodeURIComponent(current.chatId)}/messages/${encodeURIComponent(current.messageId)}/cancel`, {
        method: 'POST', signal: AbortSignal.any([current.session.signal, AbortSignal.timeout(5000)]),
      });
      update(current, (value) => ({ ...value, error: state.status === 'success' ? null : new ApiError(200, {
        code: state.code ?? 'cancelled', message: state.message, suggestion: state.suggestion ?? null,
      }), blocks: [], resultIds: [], running: false }));
      if (inContext(current)) client.setQueryData<Schemas['ChatDetail']>(keys.chat(current.chatId), (saved) => saved && ({
        ...saved, messages: saved.messages.map((message) => message.message_id === current.messageId ? { ...message, turn: state } : message),
      }));
    } catch (error) {
      update(current, (value) => ({ ...value, error: new ApiError(0, {
        code: 'cancel_unconfirmed', message: 'Не удалось подтвердить остановку запроса', suggestion: failure(error).message,
      }), blocks: [], resultIds: [], running: false }));
    } finally {
      if (finish(current)) void recover(current);
    }
  };

  const visible = turns.filter((value) => value.chatId === chatId && value.datasetId === datasetId);
  const messages = [...(history.data?.messages ?? [])];
  for (const value of visible) {
    if (!messages.some((message) => message.message_id === value.messageId)) messages.push({
      message_id: value.messageId, role: 'user', text: value.question, created_at: value.createdAt,
    });
  }
  const savedRunning = messages.find((message) => message.turn?.status === 'running'
    && !visible.some((value) => value.messageId === message.message_id && !value.running));
  const busy = (run.current !== null && inContext(run.current)) || visible.some((value) => value.running) || !!savedRunning;

  const send = async (question: string) => {
    const text = question.trim();
    if (!text || !datasetId || run.current || busy || (chatId && (!history.isSuccess || history.data.dataset_id !== datasetId))) return false;
    const requestId = crypto.randomUUID();
    const current: Run = {
      controller: new AbortController(), chatId, datasetId, messageId: `m_${requestId.replaceAll('-', '')}`,
      session: sessionSnapshot(), accepted: false, stopRequested: false, cancelling: false,
    };
    run.current = current;
    setTurns((values) => [...values, { chatId, datasetId, messageId: current.messageId, question: text, createdAt: new Date().toISOString(), blocks: [], resultIds: [], status: 'Отправляю вопрос…', running: true, error: null, assistantId: null }]);
    try {
      if (current.chatId === null) {
        const created = await request<Schemas['ChatCreated']>('/chats', {
          method: 'POST', json: { dataset_id: datasetId } satisfies Schemas['ChatCreate'], signal: current.controller.signal,
        });
        if (run.current !== current || !inContext(current) || current.controller.signal.aborted) return false;
        setTurns((values) => values.map((value) => value.messageId === current.messageId ? { ...value, chatId: created.chat_id } : value));
        current.chatId = created.chat_id;
        current.pendingNavigation = true;
        context.current = { chatId: created.chat_id, datasetId };
        onCreated(created.chat_id);
        void client.invalidateQueries({ queryKey: keys.chats(datasetId) });
      }
      if (current.stopRequested) { update(current, (value) => ({ ...value, error: stopped() })); current.controller.abort(); return true; }
      await streamEvents<AgentEventMap>(`/chats/${encodeURIComponent(current.chatId)}/messages`, {
        method: 'POST', json: { text, request_id: requestId } satisfies Schemas['SendMessageRequest'], signal: current.controller.signal,
        onEvent: (event) => {
          if (run.current !== current || !inContext(current) || current.controller.signal.aborted) return;
          if (event.event === 'status') {
            current.accepted = true;
            update(current, (value) => ({ ...value, status: current.stopRequested ? 'Останавливаю запрос…' : event.data.text }));
            if (current.stopRequested) void cancelRun(current);
          } else if (current.stopRequested) return;
          else if (event.event === 'block') update(current, (value) => ({ ...value, blocks: [...value.blocks, event.data] }));
          else if (event.event === 'tool_result') update(current, (value) => ({ ...value, resultIds: [...new Set([...value.resultIds, event.data.result_id])] }));
          else if (event.event === 'done') update(current, (value) => ({ ...value, assistantId: event.data.message_id }));
          else if (event.event === 'error') update(current, (value) => ({ ...value, blocks: [], resultIds: [], error: new ApiError(200, { code: event.data.code ?? 'agent_error', message: event.data.message, suggestion: event.data.detail ?? null }) }));
        },
      });
    } catch (error) {
      update(current, (value) => ({ ...value, blocks: [], resultIds: [], error: failure(error) }));
    } finally {
      // streamEvents already closed its reader on done/error; preserve a clean EOF.
      if (finish(current, false)) void recover(current);
    }
    return true;
  };

  const stop = () => {
    const current = run.current;
    if (current) {
      if (current.stopRequested) return;
      current.stopRequested = true;
      update(current, (value) => ({ ...value, status: 'Останавливаю запрос…', error: null, blocks: [], resultIds: [] }));
      if (current.accepted) void cancelRun(current);
      else current.stopTimer = setTimeout(() => {
        update(current, (value) => ({ ...value, error: new ApiError(0, {
          code: 'cancel_unconfirmed', message: 'Не удалось подтвердить остановку запроса',
          suggestion: 'Восстановите историю чата, чтобы проверить сохранённый исход.',
        }) }));
        current.controller.abort();
      }, 5000);
    } else if (savedRunning && chatId && datasetId) {
      const restored: Run = { controller: new AbortController(), chatId, datasetId, messageId: savedRunning.message_id, session: sessionSnapshot(), accepted: true, stopRequested: true, cancelling: false };
      run.current = restored;
      setTurns((values) => [...values.filter((value) => value.messageId !== restored.messageId), {
        chatId, datasetId, messageId: restored.messageId, question: savedRunning.text ?? '', createdAt: savedRunning.created_at,
        blocks: [], resultIds: [], status: 'Останавливаю запрос…', running: true, error: null, assistantId: null,
      }]);
      void cancelRun(restored);
    }
  };

  return { history, messages, turns: visible, busy, send, stop };
}
