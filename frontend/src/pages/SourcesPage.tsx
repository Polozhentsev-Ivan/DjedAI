import { Check, Database, Upload } from 'lucide-react';
import { useState, type SubmitEvent } from 'react';
import { useNavigate } from 'react-router';
import type { ApiError, Schemas } from '@/api/client';
import { useConnectDatabase, useDatasets, useDeleteDataset, useProfilingProgress, useUploadCsv, type ProfilingState } from '@/api/queries';
import { countOrDash, sourceLabel } from '@/api/view';
import { PageContainer, PageTitle } from '@/app/PageContainer';
import { useWorkspace } from '@/app/workspaceContext';
import type { Step } from '@/components/AgentSteps';
import { Button, LinkButton } from '@/components/Button';
import { DataTable, type CellAction, type Column, type Row } from '@/components/DataTable';
import { Icon } from '@/components/Icon';
import { Segmented } from '@/components/Segmented';
import { EmptyState, ErrorState, LoadingState } from '@/components/StateBlock';
import { cn } from '@/lib/cn';
import { formatBytes } from '@/lib/format';

type Tab = 'csv' | 'db';
type DbKind = Schemas['ConnectRequest']['kind'];

const COLUMNS: readonly Column[] = [
  { key: 'name', label: 'Название', type: 'name', width: 'minmax(200px,2fr)' },
  { key: 'source', label: 'Источник', type: 'muted', width: '110px' },
  { key: 'tables', label: 'Таблиц', type: 'number', align: 'right', width: '80px' },
  { key: 'rows', label: 'Строк', type: 'number', align: 'right', width: '120px' },
  { key: 'status', label: 'Статус', type: 'badge', width: '110px' },
  { key: 'actions', label: 'Действия', type: 'actions', width: 'minmax(320px,2.4fr)' },
];

const DB_KINDS = [{ value: 'postgres', label: 'PostgreSQL' }, { value: 'clickhouse', label: 'ClickHouse' }] as const;
const DSN_PLACEHOLDER: Record<DbKind, string> = {
  postgres: 'postgresql://user:password@host:5432/retail',
  clickhouse: 'clickhouse://user:password@host:9000/analytics',
};
const DB_TABLES = ['orders', 'customers', 'products', 'returns'];

const STEP_LABELS = [['read', 'Чтение файла'], ['types', 'Определение типов'], ['profile', 'Профилирование']] as const;
const STATUS: Record<Schemas['DatasetStatus'], { text: string; tone: 'success' | 'danger' | 'info' }> = {
  ready: { text: 'Готов', tone: 'success' },
  error: { text: 'Ошибки', tone: 'danger' },
  processing: { text: 'Обработка', tone: 'info' },
};

const card = 'overflow-hidden rounded-lg border border-border bg-card';

/** A running upload or connection: what to call it and which dataset's events to follow. */
interface Job {
  title: string;
  datasetId: string | null;
}

function progressSteps(progress: ProfilingState): Step[] {
  const known = new Set<string>(STEP_LABELS.map(([key]) => key));
  const labels = [...STEP_LABELS, ...Object.keys(progress.steps).filter((key) => !known.has(key)).map((key) => [key, key] as const)];
  return labels.map(([key, label]) => {
    const step = progress.steps[key];
    return { label, status: step?.status ?? 'pending', pct: step?.status === 'active' ? step.pct : undefined };
  });
}

function JobProgress({ job, progress, onCancel }: { job: Job; progress: ProfilingState; onCancel: () => void }) {
  return (
    <LoadingState
      size="inline"
      title={job.title}
      text={progress.message ?? 'Можно продолжать работу — датасет появится в списке, когда будет готов.'}
      steps={progressSteps(progress)}
      actionLabel="Отменить"
      onAction={onCancel}
    />
  );
}

export function SourcesPage() {
  const navigate = useNavigate();
  const datasets = useDatasets();
  const { activeDataset, setActiveDataset } = useWorkspace();
  const upload = useUploadCsv();
  const connect = useConnectDatabase();
  const remove = useDeleteDataset();
  const [tab, setTab] = useState<Tab>('csv');
  const [dbKind, setDbKind] = useState<DbKind>('postgres');
  const [dsn, setDsn] = useState('');
  const [tables, setTables] = useState<ReadonlySet<string>>(new Set(['orders', 'customers']));
  const [job, setJob] = useState<Job | null>(null);
  const [fileInput, setFileInput] = useState<HTMLInputElement | null>(null);
  const progress = useProfilingProgress(job?.datasetId ?? null);

  const browse = () => { fileInput?.click(); };
  const startUpload = (files: File[]) => {
    const first = files[0];
    if (!first) return;
    const extra = files.length > 1 ? ` и ещё ${files.length - 1}` : '';
    setTab('csv');
    setJob({ title: `Загрузка ${first.name}${extra} · ${formatBytes(files.reduce((sum, f) => sum + f.size, 0))}`, datasetId: null });
    upload.mutate(files, { onSuccess: ({ dataset_id }) => { setJob((j) => j && { ...j, datasetId: dataset_id }); } });
  };
  const submitDb = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    const kindLabel = DB_KINDS.find((k) => k.value === dbKind)?.label ?? dbKind;
    setJob({ title: `Подключение ${kindLabel}`, datasetId: null });
    connect.mutate({ kind: dbKind, dsn, tables: [...tables] }, { onSuccess: ({ dataset_id }) => { setJob((j) => j && { ...j, datasetId: dataset_id }); } });
  };
  // Cancel stops watching and removes the half-made dataset; nothing is retried.
  const cancel = () => {
    if (job?.datasetId) remove.mutate(job.datasetId);
    setJob(null);
    upload.reset();
    connect.reset();
  };

  const jobError: ApiError | null = progress.error ?? (job ? (upload.error ?? connect.error) : null);
  const running = job !== null && !jobError && progress.phase !== 'done';

  const rows: Row[] = (datasets.data ?? []).map((d) => {
    const ready = d.status === 'ready';
    const active = d.dataset_id === activeDataset;
    const actions: CellAction[] = [];
    if (ready && !active) actions.push({ label: 'Выбрать для анализа', onClick: () => { setActiveDataset(d.dataset_id); } });
    if (ready) actions.push({ label: 'Посмотреть каталог', onClick: () => { setActiveDataset(d.dataset_id); void navigate(`/catalog/${d.dataset_id}`); } });
    if (d.status === 'error') actions.push({ label: 'Загрузить заново', onClick: browse });
    actions.push({ label: 'Удалить', tone: 'danger', onClick: () => { remove.mutate(d.dataset_id); } });
    return {
      id: d.dataset_id,
      highlight: active,
      cells: {
        name: { text: d.name, tag: active ? 'активный' : undefined },
        source: { text: sourceLabel(d.source), tone: 'secondary' },
        tables: countOrDash(d.table_count),
        rows: countOrDash(d.row_count),
        status: STATUS[d.status],
        actions,
      },
    };
  });

  const toggleTable = (name: string) => {
    setTables((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const jobBlock = job && (running
    ? <JobProgress job={job} progress={progress} onCancel={cancel} />
    : jobError && (
      <ErrorState
        size="inline"
        title={tab === 'csv' ? 'Не удалось загрузить данные' : 'Не удалось подключить базу данных'}
        text={jobError.suggestion ? `${jobError.message}. ${jobError.suggestion}` : jobError.message}
        actionLabel={tab === 'csv' ? 'Загрузить заново' : 'Закрыть'}
        onAction={tab === 'csv' ? () => { cancel(); browse(); } : cancel}
      />
    ));

  return (
    <PageContainer>
      <div className="flex flex-col gap-6 text-sm leading-normal tracking-sm">
        <div>
          <PageTitle>Источники данных</PageTitle>
          <div className="mt-1.5 text-muted-foreground">Загрузите CSV или подключите базу данных — система распознает типы колонок и связи таблиц</div>
        </div>

        <section className={card}>
          <div role="tablist" aria-label="Способ добавления" className="flex gap-6 border-b border-border px-5">
            {([['csv', 'Загрузить CSV', Upload], ['db', 'Подключить БД', Database]] as const).map(([key, label, icon]) => {
              const on = tab === key;
              return (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={on}
                  onClick={() => { setTab(key); }}
                  className={cn(
                    'flex items-center gap-1.5 border-b-2 pb-3 pt-3.5 text-sm',
                    on ? 'border-primary font-semibold text-foreground' : 'border-transparent font-normal text-muted-foreground',
                  )}
                >
                  <Icon icon={icon} size="sm" />
                  {label}
                </button>
              );
            })}
          </div>
          <div className="p-5">
            <input
              ref={setFileInput}
              type="file"
              accept=".csv,text/csv"
              multiple
              hidden
              data-testid="csv-input"
              onChange={(e) => { startUpload([...(e.target.files ?? [])]); e.target.value = ''; }}
            />
            {jobBlock}
            {!jobBlock && tab === 'csv' && (
              <div
                onDragOver={(e) => { e.preventDefault(); }}
                onDrop={(e) => { e.preventDefault(); startUpload([...e.dataTransfer.files].filter((f) => /\.csv$/i.test(f.name))); }}
                className="flex flex-col items-center gap-2 rounded-lg border-(length:--border-width-dashed) border-dashed border-border bg-surface-subtle px-6 py-8 text-center"
              >
                <div className="mb-1 flex size-(--size-control) items-center justify-center rounded-full bg-card text-primary">
                  <Icon icon={Upload} size="lg" />
                </div>
                <div className="text-base tracking-body">
                  Перетащите CSV файлы сюда или <LinkButton onClick={browse}>Обзор</LinkButton>
                </div>
                <div className="text-xs text-muted-foreground">
                  Поддерживаются: UTF-8, CP1251, разделители: запятая / ; / tab · можно выбрать несколько файлов
                </div>
              </div>
            )}
            {!jobBlock && tab === 'db' && (
              <form className="flex max-w-(--chat-max) flex-col gap-4" onSubmit={submitDb}>
                <div className="flex flex-col gap-1.5">
                  <span className="text-xs font-semibold text-secondary-foreground">Тип базы данных</span>
                  <Segmented label="Тип базы данных" options={DB_KINDS} value={dbKind} onChange={setDbKind} itemClassName="px-4 py-1.5" />
                </div>
                <div className="flex flex-col gap-1.5">
                  <label htmlFor="dsn" className="text-xs font-semibold text-secondary-foreground">Строка подключения (DSN)</label>
                  <input
                    id="dsn"
                    name="dsn"
                    required
                    autoComplete="off"
                    value={dsn}
                    onChange={(e) => { setDsn(e.target.value); }}
                    placeholder={DSN_PLACEHOLDER[dbKind]}
                    className="box-border h-(--size-control) rounded-md border border-input bg-card px-3.5 font-mono text-sm text-foreground outline-none focus-visible:outline-(--focus-ring)"
                  />
                </div>
                <div className="flex flex-col gap-2">
                  <span className="text-xs font-semibold text-secondary-foreground">
                    Таблицы <span className="font-normal text-muted-foreground">· необязательно, по умолчанию — все доступные</span>
                  </span>
                  <div className="flex flex-wrap gap-2">
                    {DB_TABLES.map((name) => {
                      const on = tables.has(name);
                      return (
                        <button
                          key={name}
                          type="button"
                          aria-pressed={on}
                          onClick={() => { toggleTable(name); }}
                          className={cn(
                            'flex items-center gap-1 rounded-full border px-3.5 py-1.5 font-mono text-sm',
                            on ? 'border-accent-foreground bg-accent text-accent-foreground' : 'border-border bg-card text-secondary-foreground',
                          )}
                        >
                          {on && <Icon icon={Check} size="xs" />}
                          {name}
                        </button>
                      );
                    })}
                  </div>
                </div>
                <div className="flex flex-wrap gap-2.5">
                  <Button type="submit" className="px-5 py-2">Подключить</Button>
                  <Button variant="outline" className="px-5 py-2" disabled title="Появится вместе с проверкой соединения в API">
                    Проверить соединение
                  </Button>
                </div>
              </form>
            )}
          </div>
        </section>

        <section className={card}>
          <div className="flex items-baseline gap-2 border-b border-border px-5 py-4">
            <h2 className="text-base font-semibold tracking-body">Ваши датасеты</h2>
            {datasets.isSuccess && <span className="text-muted-foreground">{datasets.data.length}</span>}
          </div>
          {datasets.isPending && <LoadingState size="compact" title="Загружаем список датасетов…" />}
          {datasets.isError && (
            <ErrorState size="compact" title="Не удалось получить список датасетов" text={datasets.error.message} onAction={() => { void datasets.refetch(); }} />
          )}
          {datasets.isSuccess && datasets.data.length === 0 && (
            <EmptyState
              title="У вас пока нет загруженных данных"
              text="Загрузите CSV или подключите БД, чтобы начать анализ."
              actionLabel="Загрузить CSV"
              onAction={() => { setTab('csv'); }}
              secondaryLabel="Подключить БД"
              onSecondary={() => { setTab('db'); }}
            />
          )}
          {datasets.isSuccess && datasets.data.length > 0 && <DataTable label="Ваши датасеты" columns={COLUMNS} rows={rows} minWidth="900px" />}
        </section>
      </div>
    </PageContainer>
  );
}
