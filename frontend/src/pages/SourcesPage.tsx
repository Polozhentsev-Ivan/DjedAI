import { Check, Database, Upload } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router';
import { PageContainer, PageTitle } from '@/app/PageContainer';
import { useFixtureState } from '@/app/useFixtureState';
import { useWorkspace } from '@/app/workspaceContext';
import { Button, LinkButton } from '@/components/Button';
import { DataTable, type CellAction, type Column, type Row } from '@/components/DataTable';
import { Icon } from '@/components/Icon';
import { Segmented } from '@/components/Segmented';
import { EmptyState, ErrorState, LoadingState } from '@/components/StateBlock';
import { cn } from '@/lib/cn';

type Tab = 'csv' | 'db';
type DbKind = 'postgres' | 'clickhouse';

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

const UPLOAD_STEPS = [
  { label: 'Чтение файла', status: 'done' },
  { label: 'Определение типов', status: 'active', pct: 60 },
  { label: 'Профилирование', status: 'pending' },
] as const;

const card = 'overflow-hidden rounded-lg border border-border bg-card';

export function SourcesPage() {
  const navigate = useNavigate();
  const { state, set, simulate } = useFixtureState();
  const { datasets, activeDataset, setActiveDataset, removeDataset } = useWorkspace();
  const [tab, setTab] = useState<Tab>('csv');
  const [dbKind, setDbKind] = useState<DbKind>('postgres');
  const [tables, setTables] = useState<ReadonlySet<string>>(new Set(['orders', 'customers']));
  const [fileInput, setFileInput] = useState<HTMLInputElement | null>(null);

  const browse = () => { fileInput?.click(); };
  const empty = state === 'empty' || datasets.length === 0;

  const rows: Row[] = datasets.map((d) => {
    const ready = d.status === 'ready';
    const active = d.id === activeDataset;
    const actions: CellAction[] = [];
    if (ready && !active) actions.push({ label: 'Выбрать для анализа', onClick: () => { setActiveDataset(d.id); } });
    if (ready) actions.push({ label: 'Посмотреть каталог', onClick: () => { setActiveDataset(d.id); void navigate(`/catalog/${d.id}`); } });
    if (!ready) actions.push({ label: 'Загрузить заново', onClick: browse });
    actions.push({ label: 'Удалить', tone: 'danger', onClick: () => { removeDataset(d.id); } });
    return {
      id: d.id,
      highlight: active,
      cells: {
        name: { text: d.id, tag: active ? 'активный' : undefined },
        source: { text: d.source, tone: 'secondary' },
        tables: d.tables,
        rows: d.rows,
        status: { text: ready ? 'Готов' : 'Ошибки', tone: ready ? 'success' : 'danger' },
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
              onChange={(e) => { if (e.target.files?.length) simulate('loading', 3200); e.target.value = ''; }}
            />
            {tab === 'csv' && (state === 'default' || state === 'empty') && (
              <div className="flex flex-col items-center gap-2 rounded-lg border-(length:--border-width-dashed) border-dashed border-border bg-surface-subtle px-6 py-8 text-center">
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
            {tab === 'csv' && state === 'loading' && (
              <LoadingState
                size="inline"
                title="Загрузка sales.csv · 18,4 МБ"
                text="Можно продолжать работу — датасет появится в списке, когда будет готов."
                steps={UPLOAD_STEPS}
                actionLabel="Отменить"
                onAction={() => { set('default'); }}
              />
            )}
            {tab === 'csv' && state === 'error' && (
              <ErrorState
                size="inline"
                title="Не удалось загрузить данные"
                text="Файл «sales.csv»: не удалось разобрать строку 3 (разное число колонок)"
                actionLabel="Загрузить заново"
                onAction={browse}
              />
            )}
            {tab === 'db' && (
              <form className="flex max-w-(--chat-max) flex-col gap-4" onSubmit={(e) => { e.preventDefault(); }}>
                <div className="flex flex-col gap-1.5">
                  <span className="text-xs font-semibold text-secondary-foreground">Тип базы данных</span>
                  <Segmented label="Тип базы данных" options={DB_KINDS} value={dbKind} onChange={setDbKind} itemClassName="px-4 py-1.5" />
                </div>
                <label className="flex flex-col gap-1.5">
                  <span className="text-xs font-semibold text-secondary-foreground">Строка подключения (DSN)</span>
                  <input
                    name="dsn"
                    autoComplete="off"
                    placeholder={DSN_PLACEHOLDER[dbKind]}
                    className="box-border h-(--size-control) rounded-md border border-input bg-card px-3.5 font-mono text-sm text-foreground outline-none focus-visible:outline-(--focus-ring)"
                  />
                </label>
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
                  <Button variant="outline" className="px-5 py-2">Проверить соединение</Button>
                </div>
              </form>
            )}
          </div>
        </section>

        <section className={card}>
          <div className="flex items-baseline gap-2 border-b border-border px-5 py-4">
            <h2 className="text-base font-semibold tracking-body">Ваши датасеты</h2>
            <span className="text-muted-foreground">{empty ? 0 : datasets.length}</span>
          </div>
          {empty ? (
            <EmptyState
              title="У вас пока нет загруженных данных"
              text="Загрузите CSV или подключите БД, чтобы начать анализ."
              actionLabel="Загрузить CSV"
              onAction={() => { setTab('csv'); }}
              secondaryLabel="Подключить БД"
              onSecondary={() => { setTab('db'); }}
            />
          ) : (
            <DataTable label="Ваши датасеты" columns={COLUMNS} rows={rows} minWidth="900px" />
          )}
        </section>
      </div>
    </PageContainer>
  );
}
