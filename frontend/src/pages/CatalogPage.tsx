import { ArrowLeftRight, ArrowRight, LoaderCircle } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import type { Schemas } from '@/api/client';
import { useDataset, useProfilingProgress } from '@/api/queries';
import { catalogSummary, columnNote, journal, missingCell, relationViews, rowsLabel, tableMeta, typeLabel } from '@/api/view';
import { PageContainer, PageTitle } from '@/app/PageContainer';
import { useWorkspace } from '@/app/workspaceContext';
import { Button } from '@/components/Button';
import { DataTable, type Column, type Row } from '@/components/DataTable';
import { Icon } from '@/components/Icon';
import { EmptyState, ErrorState } from '@/components/StateBlock';
import { StatusBadge } from '@/components/StatusBadge';
import { TransformLogItem } from '@/components/TransformLogItem';
import { cn } from '@/lib/cn';
import { formatPercent, plural } from '@/lib/format';

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

function rowsOf(table: Schemas['CatalogTable'], relations: readonly Schemas['Relation'][]): Row[] {
  return table.columns.map((c) => ({
    id: c.name,
    cells: {
      source: c.original_name,
      system: c.name,
      type: { text: typeLabel(c), tone: 'neutral' },
      missing: missingCell(c),
      note: columnNote(c, table, relations),
    },
  }));
}

function Bar({ className }: { className: string }) {
  return <div className={cn('rounded-sm bg-skeleton', className)} />;
}

function CatalogSkeleton({ label }: { label: string }) {
  return (
    <>
      <div role="status" className="flex items-center gap-2.5 text-muted-foreground">
        <Icon icon={LoaderCircle} size="sm" spin className="text-primary" />
        {label}
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

function Catalog({ catalog }: { catalog: Schemas['DatasetCatalog'] }) {
  const tables = catalog.tables ?? [];
  const [tableId, setTableId] = useState(tables[0]?.table_id ?? '');
  const table = tables.find((t) => t.table_id === tableId) ?? tables[0];
  const entries = journal(catalog);
  const warnings = entries.filter((j) => j.level === 'warning').length;
  const infos = entries.length - warnings;
  const relations = relationViews(catalog);
  if (!table) return <EmptyState title="В датасете нет таблиц" />;
  return (
    <>
      <div role="tablist" aria-label="Таблицы датасета" className="flex flex-wrap gap-2">
        {tables.map((t) => {
          const on = t.table_id === table.table_id;
          return (
            <button
              key={t.table_id}
              type="button"
              role="tab"
              aria-selected={on}
              onClick={() => { setTableId(t.table_id); }}
              className={cn(
                'flex items-baseline gap-2 rounded-full border px-4 py-1.5',
                on ? 'border-inverse bg-inverse text-inverse-foreground' : 'border-border bg-card text-foreground',
              )}
            >
              <span className="font-mono text-sm font-semibold">{t.table_id}</span>
              <span className={cn('text-xs', on ? 'text-inverse-foreground' : 'text-muted-foreground')}>{rowsLabel(t.row_count)}</span>
            </button>
          );
        })}
      </div>

      <section className={card}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Структура таблицы</h2>
          <span className="text-muted-foreground">{tableMeta(table)}</span>
        </div>
        <DataTable label={`Структура таблицы ${table.table_id}`} columns={COLUMNS} rows={rowsOf(table, catalog.relations ?? [])} minWidth="820px" />
      </section>

      <div className={cn(twoColumns, 'items-start')}>
        <section className={card}>
          <div className="flex flex-wrap items-center gap-2 border-b border-border px-5 py-4">
            <h2 className={cn(cardTitle, 'mr-auto')}>Журнал обработки данных</h2>
            {infos > 0 && <StatusBadge variant="tag" tone="info" label={`${infos} ${plural(infos, ['информация', 'информации', 'информаций'])}`} />}
            {warnings > 0 && <StatusBadge variant="tag" tone="warning" label={`${warnings} ${plural(warnings, ['предупреждение', 'предупреждения', 'предупреждений'])}`} />}
          </div>
          {entries.length === 0 && <div className="px-5 py-4 text-muted-foreground">Данные загружены без преобразований</div>}
          {entries.map((entry) => <TransformLogItem key={entry.text} level={entry.level} text={entry.text} />)}
        </section>
        <section className={card}>
          <h2 className={cn(cardTitle, 'border-b border-border px-5 py-4')}>Обнаруженные связи таблиц</h2>
          {relations.length === 0 && <div className="px-5 py-4 text-muted-foreground">Связи между таблицами не найдены</div>}
          {relations.map((relation) => (
            <div key={relation.key} className="flex flex-col gap-3.5 px-5 py-4">
              <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2.5">
                {[relation.from, null, relation.to].map((end) =>
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
                  <span className="text-secondary-foreground">Совпадение ключей</span>
                  <span className="tabular font-semibold text-success">{formatPercent(relation.overlap)}</span>
                </div>
                <div
                  role="progressbar"
                  aria-label="Совпадение ключей"
                  aria-valuenow={relation.overlap}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  className="h-(--size-progress-lg) overflow-hidden rounded-full bg-muted"
                >
                  <div className="h-full bg-success" style={{ width: `${relation.overlap}%` }} />
                </div>
                <span className="text-xs text-muted-foreground">{relation.note}</span>
              </div>
            </div>
          ))}
        </section>
      </div>
    </>
  );
}

export function CatalogPage() {
  const navigate = useNavigate();
  const { datasetId } = useParams();
  const { ready, activeDataset, setActiveDataset } = useWorkspace();
  const id = datasetId ?? activeDataset;
  const dataset = useDataset(id);
  const toSources = () => { void navigate('/sources'); };
  const detail = dataset.data;
  const [progressAttempt, setProgressAttempt] = useState(0);
  const progress = useProfilingProgress(detail?.status === 'processing' ? detail.dataset_id : null, progressAttempt);
  useEffect(() => {
    if (datasetId && detail?.status === 'ready') setActiveDataset(detail.dataset_id);
  }, [datasetId, detail?.status, detail?.dataset_id, setActiveDataset]);
  const notFound = dataset.isError && dataset.error.status === 404;
  const waiting = (!ready && !datasetId) || (id !== null && dataset.isPending) || detail?.status === 'processing';
  const empty = ready && (id === null || notFound);

  return (
    <PageContainer>
      <div className="flex flex-col gap-6 text-sm leading-normal tracking-sm">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <PageTitle>{detail ? `Каталог датасета: ${detail.name}` : 'Каталог данных'}</PageTitle>
            {detail?.status === 'ready' && <div className="mt-1.5 text-muted-foreground">{catalogSummary(detail)}</div>}
          </div>
          {detail?.status === 'ready' && (
            <Button className="px-5 py-2" onClick={() => { void navigate('/chat'); }}>
              Задать вопрос в чате
              <Icon icon={ArrowRight} size="sm" />
            </Button>
          )}
        </div>

        {empty && (
          <div className="rounded-lg border border-border bg-card">
            <EmptyState title="Датасет не выбран" text="Выберите датасет в верхнем меню или добавьте новый." actionLabel="Добавить источник" onAction={toSources} />
          </div>
        )}
        {!empty && waiting && <CatalogSkeleton label={detail?.status === 'processing' ? 'Строим профиль таблиц…' : 'Загружаем каталог…'} />}
        {progress.error && <ErrorState size="inline" title={progress.error.message} text={progress.error.suggestion ?? undefined} onAction={() => { setProgressAttempt((attempt) => attempt + 1); void dataset.refetch(); }} />}
        {!empty && dataset.isError && !notFound && (
          <div className="rounded-lg border border-border bg-card">
            <ErrorState title="Не удалось загрузить каталог" text={dataset.error.message} onAction={() => { void dataset.refetch(); }} secondaryLabel="К источникам" onSecondary={toSources} />
          </div>
        )}
        {detail?.status === 'error' && (
          <div className="rounded-lg border border-border bg-card">
            <ErrorState
              title={detail.error?.message ?? 'Не удалось построить профиль таблицы'}
              text={detail.error?.suggestion ?? undefined}
              onAction={() => { void dataset.refetch(); }}
              secondaryLabel="К источникам"
              onSecondary={toSources}
            />
          </div>
        )}
        {detail?.status === 'ready' && detail.catalog && <Catalog key={detail.dataset_id} catalog={detail.catalog} />}
      </div>
    </PageContainer>
  );
}
