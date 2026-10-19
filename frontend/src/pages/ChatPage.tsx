import { Square } from 'lucide-react';
import { useEffect, useRef, useState, type SubmitEvent } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { useFixtureState } from '@/app/useFixtureState';
import { useChats, useSaveToDashboard } from '@/api/queries';
import { useWorkspace } from '@/app/workspaceContext';
import { ResultChart } from '@/charts/ResultChart';
import { AgentSteps } from '@/components/AgentSteps';
import { Button } from '@/components/Button';
import { ChartCard } from '@/components/ChartCard';
import { ChatMessage } from '@/components/ChatMessage';
import { ClarifyBlock } from '@/components/ClarifyBlock';
import { HowCalculated } from '@/components/HowCalculated';
import { Icon } from '@/components/Icon';
import { ErrorState } from '@/components/StateBlock';
import { SuggestionChips } from '@/components/SuggestionChips';
import { Toast } from '@/components/Toast';
import { AGENT_STEPS, ANSWER, ANSWER_CHART, ANSWER_RESULT_ID, CALCULATION, CLARIFY_OPTIONS, FACTS, PARTIAL_ANSWER, QUESTION, STARTERS, SUGGESTIONS } from '@/fixtures/chat';
import { cn } from '@/lib/cn';

/** Dashboard the demo answer is already on (mock data); #86 takes it from a dashboard_link block. */
const DASHBOARD = { id: 'd_q', title: 'Квартальная статистика продаж' };
const TOAST_MS = 4500;

/**
 * Chat screen on fixtures. Streaming blocks from the SSE transport are wired in B4-4 (#86);
 * here sending only plays the prototype's loading phase.
 */
export function ChatPage() {
  const navigate = useNavigate();
  const { chatId } = useParams();
  const fixture = useFixtureState();
  const { user, activeDataset, datasets } = useWorkspace();
  const chats = useChats(activeDataset);
  const save = useSaveToDashboard();
  const [text, setText] = useState('');
  const [toast, setToast] = useState<{ id: string; title: string } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => { clearTimeout(toastTimer.current); }, []);

  const chat = chats.data?.find((c) => c.chat_id === chatId);
  const state = chat ? fixture.state : 'empty';
  const streaming = state === 'loading';
  const dataset = datasets.find((d) => d.dataset_id === activeDataset)?.name ?? '—';

  const send = () => {
    setText('');
    if (!chat) {
      // Creating a chat and streaming the answer is #86; the fixture opens the latest chat.
      const first = chats.data?.[0];
      if (first) void navigate(`/chat/${first.chat_id}`);
      return;
    }
    fixture.simulate('loading', 2600);
  };
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!streaming && text.trim()) send();
  };
  const addToDashboard = () => {
    if (!activeDataset) return;
    save.mutate({ dataset_id: activeDataset, dashboard_id: DASHBOARD.id, result_id: ANSWER_RESULT_ID, chart: ANSWER_CHART }, {
      onSuccess: (dashboard) => {
        setToast({ id: dashboard.dashboard_id, title: dashboard.title });
        clearTimeout(toastTimer.current);
        toastTimer.current = setTimeout(() => { setToast(null); }, TOAST_MS);
      },
    });
  };
  const openDashboard = () => { if (toast) void navigate(`/dashboards/${toast.id}`); };
  const gutter = 'px-8';

  return (
    <div className="absolute inset-0 flex min-h-0 flex-col text-sm leading-normal tracking-sm text-foreground">
      <div className={cn('flex flex-none items-baseline gap-2.5 border-b border-border py-3.5', gutter)}>
        <h1 className="text-base font-semibold tracking-body">{chat?.title ?? 'Новый чат'}</h1>
        <span className="text-xs text-muted-foreground">
          датасет <span className="font-mono">{dataset}</span>
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        <div className={cn('mx-auto box-content flex max-w-(--chat-max) flex-col gap-7 pb-10 pt-7', gutter)}>
          {state === 'empty' && (
            <>
              <div className="flex flex-col items-center gap-2.5 px-0 pb-2 pt-8 text-center">
                <div aria-hidden="true" className="flex size-(--size-avatar-lg) items-center justify-center rounded-full bg-inverse font-semibold text-inverse-foreground">D</div>
                <div className="font-display text-xl font-semibold leading-tight tracking-display">Здравствуйте, {user}</div>
                <div className="max-w-(--text-max) text-base tracking-body text-muted-foreground">
                  Задайте вопрос к датасету {dataset} — ответ придёт с фактами, графиком и объяснением расчёта.
                </div>
              </div>
              <div className="grid grid-cols-[repeat(auto-fit,minmax(var(--grid-card-min),1fr))] gap-3">
                {STARTERS.map((starter) => (
                  <button
                    key={starter.question}
                    type="button"
                    onClick={send}
                    className="press flex flex-col items-start gap-1 rounded-lg border border-border bg-card p-4 text-left text-foreground hover:border-link"
                  >
                    <span className="text-sm font-semibold">{starter.question}</span>
                    <span className="text-xs text-muted-foreground">{starter.columns}</span>
                  </button>
                ))}
              </div>
            </>
          )}

          {(state === 'default' || streaming) && <ChatMessage role="user" text={QUESTION} />}

          {state === 'default' && (
            <ChatMessage role="assistant" parts={ANSWER} facts={FACTS}>
              <ChartCard
                title={ANSWER_CHART.title}
                subtitle={ANSWER_CHART.subtitle ?? undefined}
                primaryLabel={save.isPending ? 'Сохраняем…' : 'В дашборд'}
                onPrimary={addToDashboard}
                secondaryLabel="Экспорт"
              >
                <ResultChart resultId={ANSWER_RESULT_ID} spec={ANSWER_CHART} />
              </ChartCard>
              {save.isError && <ErrorState size="inline" title="Не удалось добавить график на дашборд" text={save.error.message} onAction={addToDashboard} />}
              <HowCalculated defaultOpen summary="1 инструмент · 1 SQL-запрос · 0,8 сек" {...CALCULATION} />
              <div className="text-sm tracking-sm text-secondary-foreground">
                Этот график уже есть на дашборде <Link to={`/dashboards/${DASHBOARD.id}`}>«{DASHBOARD.title}» →</Link>
              </div>
              <SuggestionChips items={SUGGESTIONS} onSelect={send} />
            </ChatMessage>
          )}

          {streaming && (
            <ChatMessage role="assistant" parts={PARTIAL_ANSWER} streaming>
              <AgentSteps title="Ассистент работает" steps={AGENT_STEPS} />
            </ChatMessage>
          )}

          {state === 'error' && (
            <>
              <ChatMessage role="user" text="Покажи сумму заказов по регионам" />
              <ChatMessage role="assistant">
                <ClarifyBlock
                  question="Какую сумму показать?"
                  hint="В таблице orders две колонки с суммой — от выбора зависит результат."
                  options={CLARIFY_OPTIONS}
                  onSelect={send}
                />
              </ChatMessage>
              <ChatMessage role="user" text="Покажи выручку по дням за 2019–2026" />
              <ChatMessage role="assistant">
                <ErrorState size="inline" title="Запрос превысил таймаут 30 сек." text="Попробуйте уточнить период — например, только 2026 год." onAction={send} />
              </ChatMessage>
            </>
          )}
        </div>
      </div>

      <form onSubmit={submit} className={cn('flex-none border-t border-border bg-background pb-4 pt-3.5', gutter)}>
        <div className="mx-auto flex max-w-(--chat-max) items-center gap-2.5">
          <input
            name="question"
            aria-label="Вопрос к данным"
            autoComplete="off"
            value={text}
            onChange={(e) => { setText(e.target.value); }}
            disabled={streaming}
            placeholder={streaming ? 'Ассистент отвечает…' : 'Введите вопрос к данным (например: «Где больше возвратов?»)'}
            className="box-border h-(--size-control) min-w-0 flex-1 rounded-full border border-input bg-card px-5 text-base tracking-body text-foreground outline-none focus-visible:outline-(--focus-ring) disabled:opacity-70"
          />
          {streaming ? (
            <Button variant="inverse" className="h-(--size-control) flex-none gap-2 px-5" onClick={() => { fixture.set('default'); }}>
              <Icon icon={Square} size="xs" />
              Остановить
            </Button>
          ) : (
            <Button type="submit" className="h-(--size-control) flex-none px-5 text-base tracking-body">Отправить</Button>
          )}
        </div>
      </form>

      {toast && <Toast message={`График добавлен на дашборд «${toast.title}»`} actionLabel="Открыть →" onAction={openDashboard} />}
    </div>
  );
}
