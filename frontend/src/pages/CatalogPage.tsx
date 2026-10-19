import { ArrowLeftRight, ArrowRight, LoaderCircle } from 'lucide-react';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { PageContainer, PageTitle } from '@/app/PageContainer';
import { useFixtureState } from '@/app/useFixtureState';
import { useWorkspace } from '@/app/workspaceContext';
import { Button } from '@/components/Button';
import { DataTable, type Column, type Row } from '@/components/DataTable';
import { Icon } from '@/components/Icon';
import { EmptyState, ErrorState } from '@/components/StateBlock';
import { StatusBadge } from '@/components/StatusBadge';
import { TransformLogItem } from '@/components/TransformLogItem';
import { CATALOG_SUMMARY, JOURNAL, RELATION, TABLES, type CatalogTable } from '@/fixtures/catalog';
import { cn } from '@/lib/cn';
import { formatPercent } from '@/lib/format';

const COLUMNS: readonly Column[] = [
  { key: 'source', label: 'Исходное имя', type: 'strong', width: 'minmax(220px,2fr)' },
  { key: 'system', label: 'В системе', type: 'mono', width: 'minmax(140px,1fr)' },
  { key: 'type', label: 'Тип данных', type: 'type', width: '150px' },
  { key: 'missing', label: 'Пропуски', type: 'number', align: 'right', width: '90px' },
  { key: 'note', label: 'Примечание', type: 'muted', width: 'minmax(180px,1.6fr)' },
];

const card = 'overflow-hidden rounded-lg border border-border bg-card';
const cardHeader = 'flex flex-wrap items-baseline gap-2 border-b border-border px-5 py-4';
const cardTitle = 'text-base font-semibold tracking-body';
/** Journal and relations sit side by side from 1180px, as in the mockup. */
const twoColumns = 'grid grid-cols-1 gap-5 min-[1180px]:grid-cols-[minmax(0,2fr)_minmax(300px,1fr)]';
const SKELETON_ROWS = [['70%', '55%'], ['50%', '70%'], ['80%', '40%'], ['60%', '65%'], ['45%', '50%'], ['65%', '35%']] as const;

function rowsOf(table: CatalogTable): Row[] {
  return table.columns.map((c) => ({
    id: c.system,
    cells: {
      source: c.source,
      system: c.system,
      type: { text: c.type, tone: 'neutral' },
      missing: { text: c.missing, tone: c.high ? 'warning' : c.missing === '0%' ? 'muted' : undefined, strong: c.high },
      note: c.note,
    },
  }));
}

function Bar({ className }: { className: string }) {
  return <div className={cn('rounded-sm bg-skeleton', className)} />;
}

function CatalogSkeleton() {
  return (
    <>
      <div role="status" className="flex items-center gap-2.5 text-muted-foreground">
        <Icon icon={LoaderCircle} size="sm" spin className="text-primary" />
        Строим профиль таблиц…
      </div>
      <div aria-hidden="true" className="flex animate-pulse gap-2">
        <div className="h-(--size-control-md) w-(--popover-width) max-w-[40%] rounded-full bg-skeleton" />
        <div className="h-(--size-control-md) w-(--grid-card-min) max-w-[35%] rounded-full bg-skeleton" />
      </div>
      <div aria-hidden="true" className={cn(card, 'flex animate-pulse flex-col gap-4 p-5')}>
        <Bar className="h-4 w-[30%]" />
        {SKELETON_ROWS.map(([a, b], index) => (
          <div key={index} className="grid grid-cols-[2fr_1fr_1fr_1fr_1.5fr] gap-5">
            <div className="h-3 rounded-sm bg-skeleton" style={{ width: a }} />
            <div className="h-3 rounded-sm bg-skeleton" style={{ width: b }} />
            <Bar className="h-3 w-[60%]" />
            <Bar className="h-3 w-[40%]" />
            <div className="h-3 rounded-sm bg-skeleton" style={{ width: b }} />
          </div>
        ))}
      </div>
      <div aria-hidden="true" className={cn(twoColumns, 'animate-pulse')}>
        <div className={cn(card, 'flex flex-col gap-3.5 p-5')}>
          <Bar className="h-4 w-[40%]" />
          <Bar className="h-3" />
          <Bar className="h-3 w-[80%]" />
          <Bar className="h-3 w-[90%]" />
        </div>
        <div className={cn(card, 'flex flex-col gap-3.5 p-5')}>
          <Bar className="h-4 w-[60%]" />
          <div className="h-12 rounded-md bg-skeleton" />
          <div className="h-(--size-progress-lg) rounded-full bg-skeleton" />
        </div>
      </div>
    </>
  );
}

export function CatalogPage() {
  const navigate = useNavigate();
  const { datasetId } = useParams();
  const { state, set } = useFixtureState();
  const { datasets, activeDataset } = useWorkspace();
  const [tableName, setTableName] = useState(TABLES[0]?.name ?? '');
  const id = datasetId ?? activeDataset;
  const known = datasets.some((d) => d.id === id && d.status === 'ready');
  const hasDataset = known && state !== 'empty';
  const table = TABLES.find((t) => t.name === tableName) ?? TABLES[0];
  const toSources = () => { void navigate('/sources'); };
  const warnings = JOURNAL.filter((j) => j.level === 'warning').length;

  return (
    <PageContainer>
      <div className="flex flex-col gap-6 text-sm leading-normal tracking-sm">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <PageTitle>{hasDataset ? `Каталог датасета: ${id ?? ''}` : 'Каталог данных'}</PageTitle>
            {hasDataset && <div className="mt-1.5 text-muted-foreground">{CATALOG_SUMMARY}</div>}
          </div>
          {hasDataset && (
            <Button className="px-5 py-2" onClick={() => { void navigate('/chat'); }}>
              Задать вопрос в чате
              <Icon icon={ArrowRight} size="sm" />
            </Button>
          )}
        </div>

        {!hasDataset && (
          <div className="rounded-lg border border-border bg-card">
            <EmptyState title="Датасет не выбран" text="Выберите датасет в верхнем меню или добавьте новый." actionLabel="Добавить источник" onAction={toSources} />
          </div>
        )}
        {hasDataset && state === 'loading' && <CatalogSkeleton />}
        {hasDataset && state === 'error' && (
          <div className="rounded-lg border border-border bg-card">
            <ErrorState
              title="Не удалось построить профиль таблицы"
              text="Файл пуст или повреждён (orders.csv). Проверьте файл и загрузите его заново."
              onAction={() => { set('default'); }}
              secondaryLabel="К источникам"
              onSecondary={toSources}
            />
          </div>
        )}
        {hasDataset && state === 'default' && table && (
          <>
            <div role="tablist" aria-label="Таблицы датасета" className="flex flex-wrap gap-2">
              {TABLES.map((t) => {
                const on = t.name === table.name;
                return (
                  <button
                    key={t.name}
                    type="button"
                    role="tab"
                    aria-selected={on}
                    onClick={() => { setTableName(t.name); }}
                    className={cn(
                      'flex items-baseline gap-2 rounded-full border px-4 py-1.5',
                      on ? 'border-inverse bg-inverse text-inverse-foreground' : 'border-border bg-card text-foreground',
                    )}
                  >
                    <span className="font-mono text-sm font-semibold">{t.name}</span>
                    <span className={cn('text-xs', on ? 'text-inverse-foreground' : 'text-muted-foreground')}>{t.rows} строк</span>
                  </button>
                );
              })}
            </div>

            <section className={card}>
              <div className={cardHeader}>
                <h2 className={cardTitle}>Структура таблицы</h2>
                <span className="text-muted-foreground">
                  {table.name} · {table.columns.length} колонок · {table.rows} строк
                </span>
              </div>
              <DataTable label={`Структура таблицы ${table.name}`} columns={COLUMNS} rows={rowsOf(table)} minWidth="820px" />
            </section>

            <div className={cn(twoColumns, 'items-start')}>
              <section className={card}>
                <div className="flex flex-wrap items-center gap-2 border-b border-border px-5 py-4">
                  <h2 className={cn(cardTitle, 'mr-auto')}>Журнал обработки данных</h2>
                  <StatusBadge variant="tag" tone="info" label={`${JOURNAL.length - warnings} информации`} />
                  <StatusBadge variant="tag" tone="warning" label={`${warnings} предупреждение`} />
                </div>
                {JOURNAL.map((entry) => <TransformLogItem key={entry.text} level={entry.level} text={entry.text} />)}
              </section>
              <section className={card}>
                <h2 className={cn(cardTitle, 'border-b border-border px-5 py-4')}>Обнаруженные связи таблиц</h2>
                <div className="flex flex-col gap-3.5 px-5 py-4">
                  <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2.5">
                    {[RELATION.from, null, RELATION.to].map((end) =>
                      end ? (
                        <div key={end.column} className="rounded-md bg-surface-subtle px-3 py-2.5">
                          <div className="font-semibold">{end.label}</div>
                          <div className="font-mono text-xs text-muted-foreground">{end.column}</div>
                        </div>
                      ) : (
                        <span key="arrow" className="flex text-muted-foreground"><Icon icon={ArrowLeftRight} size="md" /></span>
                      ),
                    )}
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <div className="flex justify-between">
                      <span className="text-secondary-foreground">Уверенность</span>
                      <span className="tabular font-semibold text-success">{formatPercent(RELATION.confidence)}</span>
                    </div>
                    <div
                      role="progressbar"
                      aria-label="Уверенность связи"
                      aria-valuenow={RELATION.confidence}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      className="h-(--size-progress-lg) overflow-hidden rounded-full bg-muted"
                    >
                      <div className="h-full bg-success" style={{ width: `${RELATION.confidence}%` }} />
                    </div>
                    <span className="text-xs text-muted-foreground">{RELATION.note}</span>
                  </div>
                </div>
              </section>
            </div>
          </>
        )}
      </div>
    </PageContainer>
  );
}
