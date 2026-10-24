import { Square } from 'lucide-react';
import { useEffect, useState, type SubmitEvent } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import type { Schemas } from '@/api/client';
import { useChatConversation } from '@/api/useChatConversation';
import { useWorkspace } from '@/app/workspaceContext';
import { ResultChart } from '@/charts/ResultChart';
import { Button } from '@/components/Button';
import { ChatMessage } from '@/components/ChatMessage';
import { ChartCard } from '@/components/ChartCard';
import { Icon } from '@/components/Icon';
import { SavedResult } from '@/components/SavedResult';
import { EmptyState, ErrorState, LoadingState } from '@/components/StateBlock';
import { cn } from '@/lib/cn';

function Blocks({ blocks }: { blocks: readonly Schemas['Block'][] }) {
  return blocks.map((block, index) => {
    if (block.type === 'text') return <div key={index} className="whitespace-pre-wrap">{block.markdown}</div>;
    if (block.type === 'chart') return <ChartCard key={index} title={block.chart.title} subtitle={block.chart.subtitle ?? undefined}><ResultChart spec={block.chart} /></ChartCard>;
    if (block.type === 'table') return <SavedResult key={index} id={block.result_id} />;
    if (block.type === 'clarification') return <div key={index}>{block.question}<ul>{block.options.map((option) => <li key={option}>{option}</li>)}</ul></div>;
    if (block.type === 'dashboard_link') return <Link key={index} to={`/dashboards/${encodeURIComponent(block.dashboard_id)}`}>{block.title}</Link>;
    return null;
  });
}

function Answer({ blocks = [], resultIds = [] }: { blocks?: readonly Schemas['Block'][] | undefined; resultIds?: readonly string[] | undefined }) {
  const embedded = new Set(blocks.filter((block) => block.type === 'table').map((block) => block.result_id));
  return <ChatMessage role="assistant">
    <Blocks blocks={blocks} />
    {[...new Set(resultIds)].filter((id) => !embedded.has(id)).map((id) => <SavedResult key={id} id={id} />)}
  </ChatMessage>;
}

function QuestionForm({ ready, streaming, onSend, onStop }: { ready: boolean; streaming: boolean; onSend: (text: string) => void; onStop: () => void }) {
  const [text, setText] = useState('');
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready || streaming || !text.trim()) return;
    onSend(text.trim());
    setText('');
  };
  return <form onSubmit={submit} className="flex-none border-t border-border bg-background px-8 pb-4 pt-3.5">
    <div className="mx-auto flex max-w-(--chat-max) items-center gap-2.5">
      <input name="question" aria-label="Вопрос к данным" autoComplete="off" value={text} onChange={(event) => { setText(event.target.value); }}
        disabled={streaming || !ready} placeholder={streaming ? 'Ассистент отвечает…' : 'Введите вопрос к данным'}
        className="box-border h-(--size-control) min-w-0 flex-1 rounded-full border border-input bg-card px-5 text-base tracking-body text-foreground outline-none focus-visible:outline-(--focus-ring) disabled:opacity-70" />
      {streaming ? <Button variant="inverse" className="h-(--size-control) flex-none gap-2 px-5" onClick={onStop}><Icon icon={Square} size="xs" />Остановить</Button>
        : <Button type="submit" disabled={!ready || !text.trim()} className="h-(--size-control) flex-none px-5 text-base tracking-body">Отправить</Button>}
    </div>
  </form>;
}

/** CSV/text CP1 integration; the full interactive block renderer remains #86. */
export function ChatPage() {
  const navigate = useNavigate();
  const { chatId } = useParams();
  const { user, activeDataset, setActiveDataset, datasets } = useWorkspace();
  const conversation = useChatConversation(chatId ?? null, activeDataset, (id) => { void navigate(`/chat/${encodeURIComponent(id)}`); });
  const { history, turns, messages, busy: streaming } = conversation;
  const detail = history.data;
  const chatDataset = detail?.dataset_id;
  useEffect(() => {
    if (chatDataset) setActiveDataset(chatDataset);
  }, [chatDataset, setActiveDataset]);
  const dataset = datasets.find((item) => item.dataset_id === activeDataset)?.name ?? '—';
  const ready = activeDataset !== null && (!chatId || history.isSuccess);
  const gutter = 'px-8';

  return (
    <div className="absolute inset-0 flex min-h-0 flex-col text-sm leading-normal tracking-sm text-foreground">
      <div className={cn('flex flex-none items-baseline gap-2.5 border-b border-border py-3.5', gutter)}>
        <h1 className="text-base font-semibold tracking-body">{detail?.title ?? 'Новый чат'}</h1>
        <span className="text-xs text-muted-foreground">датасет <span className="font-mono">{dataset}</span></span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto" aria-live="polite">
        <div className={cn('mx-auto box-content flex max-w-(--chat-max) flex-col gap-7 pb-10 pt-7', gutter)}>
          {!activeDataset && <EmptyState title="Датасет не выбран" text="Загрузите CSV или выберите готовый датасет, чтобы задать вопрос." actionLabel="К источникам" onAction={() => { void navigate('/sources'); }} />}
          {chatId && history.isPending && turns.length === 0 && <LoadingState size="compact" title="Загружаем историю чата…" />}
          {chatId && history.isError && <ErrorState size="inline" title="Не удалось загрузить историю чата" text={history.error.message} onAction={() => { void history.refetch(); }} />}
          {ready && messages.length === 0 && <div className="flex flex-col items-center gap-2.5 pb-2 pt-8 text-center">
            <div aria-hidden="true" className="flex size-(--size-avatar-lg) items-center justify-center rounded-full bg-inverse font-semibold text-inverse-foreground">D</div>
            <div className="font-display text-xl font-semibold leading-tight tracking-display">Здравствуйте, {user}</div>
            <div className="text-base text-muted-foreground">Задайте вопрос к датасету {dataset}. Сохранённые результаты расчётов появятся под ответом.</div>
          </div>}
          {messages.map((message) => {
            if (message.role !== 'user') return <Answer key={message.message_id} blocks={message.blocks} resultIds={message.result_ids} />;
            const local = turns.find((value) => value.messageId === message.message_id);
            const state = message.turn;
            const terminal = state && state.status !== 'running';
            const error = terminal ? state.status !== 'success' ? state : null : local?.error;
            const running = !terminal && !error && (local?.running || state?.status === 'running');
            const assistantId = state?.assistant_message_id ?? local?.assistantId;
            const savedAnswer = assistantId && messages.some((value) => value.message_id === assistantId);
            const code = error && ('code' in error ? error.code : null);
            return <div key={message.message_id} data-message-id={message.message_id} className="flex flex-col gap-3">
              <ChatMessage role="user" text={message.text ?? ''} />
              {running && <div role="status" className="text-muted-foreground">{local?.running ? local.status : state?.message}</div>}
              {error && <ErrorState size="inline" title={error.message} text={error.suggestion ?? undefined}
                actionLabel={streaming ? '' : 'Повторить вопрос'} onAction={() => { void conversation.send(message.text ?? ''); }} />}
              {error && <details className="text-xs text-muted-foreground">
                <summary className="cursor-pointer">Технические сведения</summary>
                <div className="mt-1 break-all font-mono">Код: {code ?? 'agent_error'}{state && <><br />Ход: {state.run_id}<br />Этап: {state.node}</>}</div>
              </details>}
              {local && !error && !savedAnswer && local.blocks.length > 0 && <Answer blocks={local.blocks} resultIds={local.resultIds} />}
            </div>;
          })}
        </div>
      </div>
      <QuestionForm key={`${chatId ?? 'new'}:${activeDataset ?? ''}`} ready={ready} streaming={streaming} onSend={(question) => { void conversation.send(question); }} onStop={conversation.stop} />
    </div>
  );
}
