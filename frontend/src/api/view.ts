// API shapes → what the screens show. Components keep taking display strings; formatting lives here.
import type { CellTone } from '@/components/DataTable';
import type { Calculation } from '@/components/HowCalculated';
import type { LogLevel } from '@/components/TransformLogItem';
import type { Kpi, KpiStat } from '@/components/WidgetCard';
import { formatCount, formatDateTime, formatDelta, formatMoney, formatMoneyShort, formatPercent, plural } from '@/lib/format';
import type { Schemas } from './client';

type Catalog = Schemas['DatasetCatalog'];
type Column = Schemas['CatalogColumn'];
type Table = Schemas['CatalogTable'];

const ROWS = ['строка', 'строки', 'строк'] as const;
const TABLES = ['таблица', 'таблицы', 'таблиц'] as const;
const COLUMNS = ['колонка', 'колонки', 'колонок'] as const;
const VALUES = ['значение', 'значения', 'значений'] as const;

export const rowsLabel = (n: number) => `${formatCount(n)} ${plural(n, ROWS)}`;

/** «CSV (2)», «Postgres», «ClickHouse». */
export function sourceLabel(source: Schemas['DatasetSource']): string {
  if (source.kind === 'postgres') return 'Postgres';
  if (source.kind === 'clickhouse') return 'ClickHouse';
  return source.files ? `CSV (${source.files})` : 'CSV';
}

const sourceShort = { csv_upload: 'CSV', postgres: 'PostgreSQL', clickhouse: 'ClickHouse' } as const;

export function countOrDash(n: number | null): string {
  return n === null ? '–' : formatCount(n);
}

/** Second line of a dataset in the top bar picker. */
export function datasetMeta(d: Schemas['DatasetSummary']): string {
  if (d.status === 'processing') return 'Обрабатывается…';
  if (d.status === 'error') return 'Ошибки загрузки';
  return `${sourceLabel(d.source)} · ${d.row_count === null ? '–' : rowsLabel(d.row_count)}`;
}

/** «2 таблицы · 120 450 строк · CSV · загружен 18.10.2026 13:52». */
export function catalogSummary(d: Schemas['DatasetDetail']): string {
  const tables = d.table_count ?? d.catalog?.tables?.length ?? 0;
  const parts = [`${tables} ${plural(tables, TABLES)}`];
  if (d.row_count !== null) parts.push(rowsLabel(d.row_count));
  parts.push(sourceShort[d.source.kind], `загружен ${formatDateTime(d.created_at)}`);
  return parts.join(' · ');
}

export function tableMeta(table: Table): string {
  const n = table.columns.length;
  return `${table.table_id} · ${n} ${plural(n, COLUMNS)} · ${rowsLabel(table.row_count)}`;
}

/** Type badge text: «money (₽)», «text (id)», «date». */
export function typeLabel(column: Column): string {
  if (column.semantic_type === 'money') {
    const symbols: Record<string, string> = { RUB: '₽', USD: '$', EUR: '€' };
    return column.currency ? `money (${symbols[column.currency] ?? column.currency})` : 'money';
  }
  if (column.semantic_type === 'id') return /INT/i.test(column.physical_type) ? 'integer (id)' : 'text (id)';
  return column.semantic_type;
}

/** Share of missing values worth a warning. */
const MISSING_WARNING = 0.05;

export function missingCell(column: Column): { text: string; tone?: CellTone; strong?: boolean } {
  const text = formatPercent(Math.round(column.null_ratio * 1000) / 10);
  if (column.null_ratio === 0) return { text, tone: 'muted' };
  if (column.null_ratio >= MISSING_WARNING) return { text, tone: 'warning', strong: true };
  return { text };
}

/** strftime pattern → «ДД.ММ.ГГГГ ЧЧ:ММ». */
export function humanFormat(pattern: string): string {
  const parts: Record<string, string> = { '%d': 'ДД', '%m': 'ММ', '%Y': 'ГГГГ', '%y': 'ГГ', '%H': 'ЧЧ', '%M': 'ММ', '%S': 'СС' };
  return pattern.replace(/%[dmYyHMS]/g, (token) => parts[token] ?? token);
}

export function columnNote(column: Column, table: Table, relations: readonly Schemas['Relation'][]): string {
  if (column.is_unique && table.primary_key_candidates?.includes(column.name)) return 'Уникальный ключ';
  const relation = relations.find((r) => r.from.table === table.table_id && r.from.column === column.name);
  if (relation) return `Связь с ${relation.to.table}.${relation.to.column}`;
  if (column.parse?.format) return `Формат ${humanFormat(column.parse.format)}`;
  if (column.semantic_type === 'category') return `${column.distinct_count} ${plural(column.distinct_count, VALUES)}`;
  return '—';
}

export function journal(catalog: Catalog): { level: LogLevel; text: string }[] {
  return [
    ...(catalog.transformations ?? []).map((t) => {
      const target = [t.table, t.column].filter(Boolean).join('.');
      const detail = Object.entries(t.detail ?? {}).map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join(', ');
      return { level: 'info' as const, text: `${target}: ${t.action}${detail ? ` — ${detail}` : ''}` };
    }),
    ...(catalog.warnings ?? []).map((text) => ({ level: 'warning' as const, text })),
  ];
}

const CARDINALITY = { '1:1': 'Один к одному', 'N:1': 'Многие к одному', '1:N': 'Один ко многим', 'N:M': 'Многие ко многим' } as const;
const CONFIDENCE = { high: 'высокая', medium: 'средняя', low: 'низкая' } as const;

export interface RelationView {
  key: string;
  from: { label: string; column: string };
  to: { label: string; column: string };
  /** Matched keys, percent. */
  overlap: number;
  note: string;
}

function columnLabel(catalog: Catalog, ref: Schemas['ColumnRef']): string {
  const table = catalog.tables?.find((t) => t.table_id === ref.table);
  return table?.columns.find((c) => c.name === ref.column)?.original_name ?? ref.column;
}

export function relationViews(catalog: Catalog): RelationView[] {
  return (catalog.relations ?? []).map((r) => ({
    key: `${r.from.table}.${r.from.column}->${r.to.table}.${r.to.column}`,
    from: { label: columnLabel(catalog, r.from), column: `${r.from.table}.${r.from.column}` },
    to: { label: columnLabel(catalog, r.to), column: `${r.to.table}.${r.to.column}` },
    overlap: Math.round(r.overlap * 1000) / 10,
    note: `${CARDINALITY[r.cardinality]} · уверенность ${CONFIDENCE[r.confidence]}`,
  }));
}

export interface GraphNode {
  id: string;
  rows: string;
  /** Centre of the node box in viewBox units. */
  x: number;
  y: number;
}

export interface GraphEdge {
  key: string;
  /** SVG path from the border of the «from» box to the border of the «to» box. */
  path: string;
  labelX: number;
  labelY: number;
  label: string;
  /** Accessible sentence: what joins to what and how well. */
  title: string;
  /** N:M has no direction, so no arrow. */
  directed: boolean;
  weak: boolean;
}

export interface RelationGraph {
  width: number;
  height: number;
  node: { width: number; height: number };
  nodes: GraphNode[];
  edges: GraphEdge[];
}

const NODE = { width: 120, height: 44 } as const;
/** Gap between parallel edges of one table pair. */
const BEND = 28;
const LABEL_LIFT = 12;

function layout(count: number): { width: number; height: number; at: (i: number) => [number, number] } {
  if (count <= 2) {
    // Two boxes at the edges leave room for the label between them; the narrow card shows it near 1:1.
    const width = 340;
    const edge = NODE.width / 2 + 4;
    return { width, height: 90, at: (i) => [count === 1 ? width / 2 : i === 0 ? edge : width - edge, 45] };
  }
  const width = count <= 4 ? 360 : 480;
  const height = count <= 4 ? 260 : 340;
  const rx = width / 2 - NODE.width / 2 - 8;
  const ry = height / 2 - NODE.height / 2 - 8;
  return {
    width,
    height,
    at: (i) => {
      const angle = -Math.PI / 2 + (2 * Math.PI * i) / count;
      return [width / 2 + rx * Math.cos(angle), height / 2 + ry * Math.sin(angle)];
    },
  };
}

/** Where the ray from the box centre towards (dx, dy) leaves the box. */
function border([x, y]: readonly [number, number], dx: number, dy: number): [number, number] {
  const scale = Math.min(NODE.width / 2 / Math.abs(dx), NODE.height / 2 / Math.abs(dy));
  return [x + dx * scale, y + dy * scale];
}

const round = (n: number) => Math.round(n * 10) / 10;

/** Tables as nodes, computed relations as edges from the many side to the unique key. */
export function relationGraph(catalog: Catalog): RelationGraph {
  const tables = catalog.tables ?? [];
  const { width, height, at } = layout(tables.length);
  const nodes = tables.map((t, i) => {
    const [x, y] = at(i);
    return { id: t.table_id, rows: rowsLabel(t.row_count), x: round(x), y: round(y) };
  });
  const position = new Map(nodes.map((n) => [n.id, [n.x, n.y] as const]));
  const seen = new Map<string, number>();
  const edges: GraphEdge[] = [];
  for (const r of catalog.relations ?? []) {
    const from = position.get(r.from.table);
    const to = position.get(r.to.table);
    if (!from || !to || r.from.table === r.to.table) continue;
    const pair = [r.from.table, r.to.table].sort().join('|');
    const index = seen.get(pair) ?? 0;
    seen.set(pair, index + 1);
    // 0, +1, −1, +2, … gaps aside the straight line; the sign follows the sorted pair, not the direction.
    const offset = (index % 2 === 0 ? -1 : 1) * Math.ceil(index / 2) * BEND * (r.from.table < r.to.table ? 1 : -1);
    const [dx, dy] = [to[0] - from[0], to[1] - from[1]];
    const length = Math.hypot(dx, dy);
    const control = [(from[0] + to[0]) / 2 - (dy / length) * offset, (from[1] + to[1]) / 2 + (dx / length) * offset] as const;
    const start = border(from, control[0] - from[0], control[1] - from[1]);
    const end = border(to, control[0] - to[0], control[1] - to[1]);
    const overlap = formatPercent(Math.round(r.overlap * 1000) / 10);
    edges.push({
      key: `${r.from.table}.${r.from.column}->${r.to.table}.${r.to.column}`,
      path: `M${round(start[0])},${round(start[1])} Q${round(control[0])},${round(control[1])} ${round(end[0])},${round(end[1])}`,
      labelX: round((start[0] + 2 * control[0] + end[0]) / 4),
      // Above the line, so the label never covers the arrow.
      labelY: round((start[1] + 2 * control[1] + end[1]) / 4 - LABEL_LIFT),
      label: `${r.cardinality} · ${overlap}`,
      title: `${r.from.table}.${r.from.column} → ${r.to.table}.${r.to.column}: ${CARDINALITY[r.cardinality].toLowerCase()}, совпадение ${overlap}, уверенность ${CONFIDENCE[r.confidence]}`,
      directed: r.cardinality !== 'N:M',
      weak: r.confidence === 'low',
    });
  }
  return { width, height, node: NODE, nodes, edges };
}

function literal(value: unknown): string {
  if (typeof value === 'string') return `'${value}'`;
  return JSON.stringify(value);
}

/** time_aggregate + {table, freq} → «time_aggregate(table='orders', freq='month')». */
export function toolCall(tool: string, params: Record<string, unknown>): string {
  return `${tool}(${Object.entries(params).map(([k, v]) => `${k}=${literal(v)}`).join(', ')})`;
}

export function calculation(result: Schemas['ToolResultMeta']): Calculation {
  return {
    tool: toolCall(result.tool ?? 'unknown_tool', result.params ?? {}),
    columns: (result.data?.columns ?? []).map((c) => typeof c === 'string' ? { label: c, name: c } : { label: c.label ?? c.name, name: c.name }),
    sql: result.sql ?? '—',
    stats: result.data?.row_count == null ? undefined : `${rowsLabel(result.data.row_count)} в результате`,
  };
}

export function formatMeasure(value: unknown, format: Schemas['ChartMeasure']['format']): string {
  if (typeof value !== 'number') {
    if (value == null) return '—';
    if (typeof value === 'string') return value;
    if (typeof value === 'boolean' || typeof value === 'bigint') return String(value);
    return JSON.stringify(value);
  }
  if (format === 'money') return Math.abs(value) >= 1e6 ? formatMoneyShort(value) : formatMoney(value);
  if (format === 'percent') return formatPercent(value);
  return formatCount(value);
}

/** KPI card from a one-row result: main measure, delta facts and the other measures. */
export function kpiView(chart: Schemas['ChartTemplate'], data: Schemas['ResultData'], result: Schemas['ToolResultMeta'] | undefined): Kpi | null {
  const row = data.rows?.[0];
  const [main, ...rest] = chart.y ?? [];
  if (!row || !main) return null;
  const columns = data.columns ?? [];
  const nameOf = (c: (typeof columns)[number]) => typeof c === 'string' ? c : c.name;
  const valueOf = (field: string): unknown => row[columns.findIndex((c) => nameOf(c) === field)] ?? null;
  const deltaOf = (field: string) => result?.facts?.find((f) => f.kind === 'delta' && f.values?.field === field);
  const mainDelta = deltaOf(main.field);
  const base = typeof mainDelta?.values?.base === 'string' ? mainDelta.values.base : null;
  const baseColumn = base ? columns.find((c) => nameOf(c) === base) : undefined;
  const baseValue = base ? valueOf(base) : null;
  const deltaNumber = Number(mainDelta?.values?.delta ?? 0);
  return {
    caption: main.label ?? main.field,
    value: formatMeasure(valueOf(main.field), main.format),
    delta: mainDelta?.text ?? '',
    deltaTone: deltaNumber < 0 ? 'danger' : 'success',
    target: baseColumn && typeof baseValue === 'number' ? `${typeof baseColumn === 'string' ? baseColumn : baseColumn.label ?? baseColumn.name}: ${formatMoney(baseValue)}` : '',
    stats: rest.map((measure): KpiStat => {
      const delta = Number(deltaOf(measure.field)?.values?.delta ?? 0);
      return {
        label: measure.label ?? measure.field,
        value: formatMeasure(valueOf(measure.field), measure.format),
        delta: delta === 0 ? '' : formatDelta(delta),
        tone: delta < 0 ? 'danger' : 'success',
      };
    }),
  };
}

export function dashboardMeta(d: Schemas['Dashboard']): string {
  const parts = [d.description ?? null, d.refreshed_at ? `Последнее обновление: ${formatDateTime(d.refreshed_at)}` : null];
  return parts.filter(Boolean).join(' · ');
}
