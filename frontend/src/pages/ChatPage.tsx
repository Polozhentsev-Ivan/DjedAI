import { Square } from 'lucide-react';
import { useEffect, useState, type SubmitEvent } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import type { Schemas } from '@/api/client';
import { useChatConversation } from '@/api/useChatConversation';
import { useWorkspace } from '@/app/workspaceContext';
import { ResultChart } from '@/charts/ResultChart';
import { Button } from '@/components/Button';
import { ChatMessage } from '@/components/ChatMessage';
import { Icon } from '@/components/Icon';
import { SavedResult } from '@/components/SavedResult';
import { EmptyState, ErrorState, LoadingState } from '@/components/StateBlock';
import { cn } from '@/lib/cn';

function Blocks({ blocks }: { blocks: readonly Schemas['Block'][] }) {
  return blocks.map((block, index) => {
    if (block.type === 'text') return <div key={index} className="whitespace-pre-wrap">{block.markdown}</div>;
    if (block.type === 'chart') return <section key={index}><h2 className="font-semibold">{block.chart.title}</h2><ResultChart resultId={block.chart.result_id} spec={block.chart} /></section>;
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
  const { history, turn } = conversation;
  const detail = history.data;
  const chatDataset = detail?.dataset_id;
  useEffect(() => {
    if (chatDataset) setActiveDataset(chatDataset);
  }, [chatDataset, setActiveDataset]);
  const streaming = turn?.running ?? false;
  const dataset = datasets.find((item) => item.dataset_id === activeDataset)?.name ?? '—';
  const messages = turn?.history ?? detail?.messages ?? [];
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
          {chatId && history.isPending && !turn && <LoadingState size="compact" title="Загружаем историю чата…" />}
          {chatId && history.isError && <ErrorState size="inline" title="Не удалось загрузить историю чата" text={history.error.message} onAction={() => { void history.refetch(); }} />}
          {ready && messages.length === 0 && !turn?.question && <div className="flex flex-col items-center gap-2.5 pb-2 pt-8 text-center">
            <div aria-hidden="true" className="flex size-(--size-avatar-lg) items-center justify-center rounded-full bg-inverse font-semibold text-inverse-foreground">D</div>
            <div className="font-display text-xl font-semibold leading-tight tracking-display">Здравствуйте, {user}</div>
            <div className="text-base text-muted-foreground">Задайте вопрос к датасету {dataset}. Сохранённые результаты расчётов появятся под ответом.</div>
          </div>}
          {messages.map((message) => message.role === 'user'
            ? <ChatMessage key={message.message_id} role="user" text={message.text ?? ''} />
            : <Answer key={message.message_id} blocks={message.blocks} resultIds={message.result_ids} />)}
          {turn?.question && <ChatMessage role="user" text={turn.question} />}
          {turn && (turn.blocks.length > 0 || turn.resultIds.length > 0) && <Answer blocks={turn.blocks} resultIds={turn.resultIds} />}
          {streaming && <div role="status" className="text-muted-foreground">{turn?.status}</div>}
          {turn?.error && <ErrorState size="inline" title={turn.error.message} text={turn.error.suggestion ?? undefined} actionLabel="" />}
        </div>
      </div>
      <QuestionForm key={`${chatId ?? 'new'}:${activeDataset ?? ''}`} ready={ready} streaming={streaming} onSend={(question) => { void conversation.send(question); }} onStop={conversation.stop} />
    </div>
  );
}
