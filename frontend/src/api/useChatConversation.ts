import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { ApiError, request, type Schemas } from './client';
import { keys, useChat } from './queries';
import { streamEvents, type AgentEventMap } from './sse';

interface Turn {
  chatId: string | null;
  datasetId: string;
  history: Schemas['ChatMessage'][];
  question: string;
  blocks: Schemas['Block'][];
  resultIds: string[];
  status: string;
  running: boolean;
  error: ApiError | null;
}

interface Run {
  controller: AbortController;
  chatId: string | null;
  datasetId: string;
}

const failure = (error: unknown) => error instanceof ApiError ? error : new ApiError(0, { code: 'unknown', message: 'Не удалось получить ответ сервера' });

/** One explicitly started POST at a time; navigation cancels it, GET restores persisted history. */
export function useChatConversation(chatId: string | null, datasetId: string | null, onCreated: (id: string) => void) {
  const client = useQueryClient();
  const history = useChat(chatId);
  const run = useRef<Run | null>(null);
  const [turn, setTurn] = useState<Turn | null>(null);

  useEffect(() => {
    const current = run.current;
    if (current && (current.chatId !== chatId || current.datasetId !== datasetId)) {
      current.controller.abort();
      run.current = null;
    }
    setTurn((value) => value && (value.chatId !== chatId || value.datasetId !== datasetId) ? null : value);
  }, [chatId, datasetId]);
  useEffect(() => () => { run.current?.controller.abort(); run.current = null; }, []);

  const visible = turn && turn.chatId === chatId && turn.datasetId === datasetId ? turn : null;

  const send = async (question: string) => {
    const text = question.trim();
    if (!text || !datasetId || run.current || (chatId && !history.isSuccess)) return false;
    const current: Run = { controller: new AbortController(), chatId, datasetId };
    run.current = current;
    setTurn({ chatId, datasetId, history: history.data?.messages ?? [], question: text, blocks: [], resultIds: [], status: 'Отправляем вопрос…', running: true, error: null });
    const update = (change: (value: Turn) => Turn) => {
      if (run.current === current && !current.controller.signal.aborted) setTurn((value) => value && change(value));
    };
    try {
      if (current.chatId === null) {
        const created = await request<Schemas['ChatCreated']>('/chats', {
          method: 'POST', json: { dataset_id: datasetId } satisfies Schemas['ChatCreate'], signal: current.controller.signal,
        });
        if (run.current !== current || current.controller.signal.aborted) return false;
        current.chatId = created.chat_id;
        update((value) => ({ ...value, chatId: created.chat_id }));
        onCreated(created.chat_id);
        void client.invalidateQueries({ queryKey: keys.chats(datasetId) });
      }
      await streamEvents<AgentEventMap>(`/chats/${encodeURIComponent(current.chatId)}/messages`, {
        method: 'POST', json: { text } satisfies Schemas['SendMessageRequest'], signal: current.controller.signal,
        onEvent: (event) => {
          if (event.event === 'status') update((value) => ({ ...value, status: event.data.text }));
          else if (event.event === 'block') update((value) => ({ ...value, blocks: [...value.blocks, event.data] }));
          else if (event.event === 'tool_result') update((value) => ({ ...value, resultIds: [...new Set([...value.resultIds, event.data.result_id])] }));
          else if (event.event === 'error') update((value) => ({ ...value, error: new ApiError(200, { code: 'agent_error', message: event.data.message, suggestion: event.data.detail ?? null }) }));
        },
      });
    } catch (error) {
      update((value) => ({ ...value, error: failure(error) }));
    } finally {
      if (run.current === current) {
        // Keep the streamed snapshot until the saved message replaces it. Never append
        // the GET messages to it: the same blocks would appear twice after `done`.
        if (current.chatId) {
          try {
            await client.cancelQueries({ queryKey: keys.chat(current.chatId) });
            const saved = await client.query({
              queryKey: keys.chat(current.chatId),
              queryFn: ({ signal }) => request<Schemas['ChatDetail']>(`/chats/${encodeURIComponent(current.chatId ?? '')}`, { signal }),
              staleTime: 0,
            });
            if (run.current === current) setTurn((value) => value && { ...value, history: saved.messages, question: '', blocks: [], resultIds: [] });
          } catch {
            // Preserve the answer already received if recovering history also fails.
          }
          void client.invalidateQueries({ queryKey: keys.chats(datasetId) });
        }
        if (run.current === current) {
          setTurn((value) => value && { ...value, running: false });
          run.current = null;
        }
      }
    }
    return true;
  };

  const stop = () => {
    const current = run.current;
    if (!current) return;
    current.controller.abort();
    setTurn((value) => value && { ...value, status: 'Восстанавливаем сохранённую историю…', error: new ApiError(0, {
      code: 'stopped', message: 'Получение ответа остановлено',
      suggestion: 'История перечитывается с сервера. Повторный запрос запускается только кнопкой «Отправить».',
    }) });
  };

  return { history, turn: visible, send, stop };
}
