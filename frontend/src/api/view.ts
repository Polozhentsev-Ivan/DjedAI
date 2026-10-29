// API shapes → what the screens show. Components keep taking display strings; formatting lives here.
import type { CellTone } from '@/components/DataTable';
import type { Calculation, CalculationParam } from '@/components/HowCalculated';
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
const DUPLICATES = ['повтор строки', 'повтора строк', 'повторов строк'] as const;

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
  const duplicates = table.duplicate_rows > 0 ? ` · ${formatCount(table.duplicate_rows)} ${plural(table.duplicate_rows, DUPLICATES)}` : '';
  return `${table.table_id} · ${n} ${plural(n, COLUMNS)} · ${rowsLabel(table.row_count)}${duplicates}`;
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

/** Number formats name the decimal separator; the currency and percent are already in the type. */
const SEPARATOR: Record<string, string> = { decimal_comma: 'Десятичная запятая', decimal_point: 'Десятичная точка' };

function parseNote(format: string): string {
  if (format.includes('%')) return `Формат ${humanFormat(format)}`;
  return SEPARATOR[format.split(',')[0]?.trim() ?? ''] ?? `Формат ${format}`;
}

export function columnNote(column: Column, table: Table, relations: readonly Schemas['Relation'][]): string {
  if (column.is_unique && table.primary_key_candidates?.includes(column.name)) return 'Уникальный ключ';
  const relation = relations.find((r) => r.from.table === table.table_id && r.from.column === column.name);
  if (relation) return `Связь с ${relation.to.table}.${relation.to.column}`;
  if (column.parse?.format) return parseNote(column.parse.format);
  if (column.semantic_type === 'category') return `${column.distinct_count} ${plural(column.distinct_count, VALUES)}`;
  return '—';
}

type Transformation = Schemas['Transformation'];
type JournalEntry = { level: LogLevel; text: string };

/** What a parse_* action recognized, by the semantic type it produced. */
const PARSED: Record<string, string> = {
  money: 'денежные суммы', percent: 'проценты', integer: 'целые числа', float: 'дробные числа',
  bool: 'логические значения', date: 'даты', datetime: 'дата и время',
};
/** Why a column stayed text; «text» itself is ordinary text and is not logged. */
const KEPT_TEXT: Record<string, string> = {
  empty_column: 'колонка пустая',
  id_leading_zeros: 'ведущие нули — это идентификатор',
  id_name: 'по названию это идентификатор',
  mixed_currencies: 'в колонке смешаны валюты',
  mixed_money_percent: 'в колонке смешаны деньги и проценты',
  ambiguous_date: 'формат даты неоднозначен: день и месяц не различить',
  unsupported_type: 'тип источника не поддерживается',
};
/** Detail keys the sentences already say or that only matter to the pipeline. */
const SPOKEN = new Set(['currency', 'format', 'decimal_separator', 'success_ratio', 'type', 'non_empty', 'parsed', 'reason', 'from', 'to', 'timezone', 'count', 'examples', 'target', 'examples_with_zeros']);

const raw = (value: unknown): string => (typeof value === 'string' ? value : Array.isArray(value) ? value.map(raw).join(', ') : JSON.stringify(value));

/** null — nothing worth logging, undefined — an action the UI does not know. */
function sentence(t: Transformation, detail: Record<string, unknown>): JournalEntry | null | undefined {
  const str = (key: string) => (typeof detail[key] === 'string' ? detail[key] : undefined);
  const num = (key: string) => (typeof detail[key] === 'number' ? detail[key] : undefined);
  if (t.action.startsWith('parse_') && PARSED[t.action.slice(6)]) {
    const parts = [`распознаны ${PARSED[t.action.slice(6)]}`];
    const [currency, format, separator] = [str('currency'), str('format'), str('decimal_separator')];
    if (currency) parts.push(`валюта ${currency}`);
    if (format) parts.push(`формат ${humanFormat(format)}`);
    if (separator) parts.push(`десятичный разделитель «${separator}»`);
    const ratio = num('success_ratio');
    return { level: 'info', text: `${parts.join(', ')}${ratio === undefined ? '' : ` — ${formatPercent(Math.floor(ratio * 1000) / 10)} непустых значений`}` };
  }
  if (t.action === 'unparsed_values') {
    const count = num('count') ?? 0;
    const examples = Array.isArray(detail.examples) ? detail.examples.map((e) => `«${raw(e)}»`).join(', ') : '';
    const target = PARSED[str('target') ?? ''] ?? str('target') ?? 'нужный тип';
    return {
      level: 'warning',
      text: `${formatCount(count)} ${plural(count, VALUES)} не распознаны как ${target} и заменены на пустые${examples ? `, например ${examples}` : ''}`,
    };
  }
  if (t.action === 'keep_text') {
    const reason = str('reason') ?? '';
    if (reason === 'text') return null;
    const source = str('type');
    const type = reason === 'unsupported_type' && source ? ` (${source})` : '';
    return { level: 'info', text: `оставлена текстом: ${KEPT_TEXT[reason] ?? reason}${type}` };
  }
  const [from, to, timezone] = [str('from'), str('to'), str('timezone')];
  if (t.action === 'cast_type' && from && to) {
    return { level: 'info', text: `тип ${from} приведён к ${to}${timezone ? `, время переведено в ${timezone}` : ''}` };
  }
  return undefined;
}

function transformationEntry(t: Transformation): JournalEntry | null {
  const target = [t.table, t.column].filter(Boolean).join('.');
  const detail = t.detail ?? {};
  const known = sentence(t, detail);
  if (known === null) return null;
  if (known === undefined) {
    // An action the UI does not know yet: show it as is rather than hide it.
    const all = Object.entries(detail).map(([key, value]) => `${key}: ${raw(value)}`).join(', ');
    return { level: 'info', text: `${target}: ${t.action}${all ? ` — ${all}` : ''}` };
  }
  const rest = Object.entries(detail).filter(([key]) => !SPOKEN.has(key)).map(([key, value]) => `${key}: ${raw(value)}`).join(', ');
  return { level: known.level, text: `${target}: ${known.text}${rest ? ` (${rest})` : ''}` };
}

/** Renames of one table in one line: both names are side by side in the structure table. */
function renames(transformations: readonly Transformation[]): JournalEntry[] {
  const byTable = new Map<string, Transformation[]>();
  for (const t of transformations) if (t.action === 'rename_column') byTable.set(t.table, [...(byTable.get(t.table) ?? []), t]);
  return [...byTable].map(([table, list]): JournalEntry => {
    if (list.length === 1) {
      const [only] = list as [Transformation];
      const d = only.detail ?? {};
      return { level: 'info', text: `${table}: колонка «${raw(d.from ?? '')}» получила системное имя ${raw(d.to ?? only.column ?? '')}` };
    }
    return { level: 'info', text: `${table}: ${list.length} ${plural(list.length, COLUMNS)} получили системные имена — соответствие показано в структуре таблицы` };
  });
}

export function journal(catalog: Catalog): JournalEntry[] {
  const transformations = catalog.transformations ?? [];
  return [
    ...renames(transformations),
    ...transformations.filter((t) => t.action !== 'rename_column').map(transformationEntry).filter((e): e is JournalEntry => e !== null),
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

/** `count` boxes around an ellipse; `centred` adds one more box in its middle. */
function layout(count: number, centred = false): { width: number; height: number; at: (i: number) => [number, number] } {
  if (count <= 2 && !centred) {
    // Two boxes at the edges leave room for the label between them; the narrow card shows it near 1:1.
    const width = 340;
    const edge = NODE.width / 2 + 4;
    return { width, height: 90, at: (i) => [count === 1 ? width / 2 : i === 0 ? edge : width - edge, 45] };
  }
  const point = (rx: number, ry: number, i: number): [number, number] => {
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / count;
    return [rx * Math.cos(angle), ry * Math.sin(angle)];
  };
  // Start from the base ellipse and grow it until no two boxes (with a gap) overlap: many tables get a bigger graph.
  let [rx, ry] = count <= 4 ? [112, 100] : [172, 140];
  const boxes = () => [...Array.from({ length: count }, (_, i) => point(rx, ry, i)), ...(centred ? [[0, 0] as [number, number]] : [])];
  // Spokes of a star also need room for their labels between the centre and the ring.
  const cramped = () => centred && boxes().slice(0, count).some(([x, y]) => Math.hypot(x, y) - 2 * exit(x, y) < SPOKE_ROOM);
  while (overlap(boxes()) || cramped()) [rx, ry] = [rx * GROWTH, ry * GROWTH];
  // The graph is as big as its boxes, not the whole ellipse: three tables leave its bottom empty.
  const placed = boxes();
  const xs = placed.map(([x]) => x);
  const ys = placed.map(([, y]) => y);
  const [left, top] = [Math.min(...xs) - NODE.width / 2 - 8, Math.min(...ys) - NODE.height / 2 - 8];
  return {
    width: Math.ceil(Math.max(...xs) + NODE.width / 2 + 8 - left),
    height: Math.ceil(Math.max(...ys) + NODE.height / 2 + 8 - top),
    at: (i) => {
      const [x, y] = i < count ? point(rx, ry, i) : [0, 0];
      return [x - left, y - top];
    },
  };
}

/** Space kept between node boxes, viewBox units. */
const NODE_GAP = 16;
const GROWTH = 1.1;
/** A table joined to this many others is the hub of a star and goes to the middle. */
const HUB_DEGREE = 3;
/** Shortest visible edge that still fits its label; shorter ones keep it in the title and the list. */
const LABEL_ROOM = 70;
/** Spokes of a star get room for the label plus clear line around it and the arrow. */
const SPOKE_ROOM = 110;

/** The table related to the most other tables, if it is a clear star centre. */
function hub(catalog: Catalog): string | undefined {
  const neighbours = new Map<string, Set<string>>();
  for (const r of catalog.relations ?? []) {
    if (r.from.table === r.to.table) continue;
    neighbours.set(r.from.table, (neighbours.get(r.from.table) ?? new Set()).add(r.to.table));
    neighbours.set(r.to.table, (neighbours.get(r.to.table) ?? new Set()).add(r.from.table));
  }
  const tables = new Set((catalog.tables ?? []).map((t) => t.table_id));
  let best: [string, number] | undefined;
  for (const [table, set] of neighbours) if (tables.has(table) && (!best || set.size > best[1])) best = [table, set.size];
  return best && best[1] >= HUB_DEGREE ? best[0] : undefined;
}

function overlap(points: readonly (readonly [number, number])[]): boolean {
  return points.some(([x, y], i) => points.slice(i + 1).some(([u, v]) =>
    Math.abs(x - u) < NODE.width + NODE_GAP && Math.abs(y - v) < NODE.height + NODE_GAP));
}

/** Distance from the box centre to its border along (dx, dy). */
function exit(dx: number, dy: number): number {
  const length = Math.hypot(dx, dy);
  return length * Math.min(NODE.width / 2 / Math.abs(dx), NODE.height / 2 / Math.abs(dy));
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
  const centre = hub(catalog);
  const ring = tables.filter((t) => t.table_id !== centre).map((t) => t.table_id);
  const { width, height, at } = layout(ring.length, centre !== undefined);
  const nodes = tables.map((t) => {
    const [x, y] = at(t.table_id === centre ? ring.length : ring.indexOf(t.table_id));
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
      // On the middle of the line: its card-coloured halo hides the line under the text, the arrow stays clear.
      labelY: round((start[1] + 2 * control[1] + end[1]) / 4),
      label: Math.hypot(end[0] - start[0], end[1] - start[1]) < LABEL_ROOM ? '' : `${r.cardinality} · ${overlap}`,
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

/** Tool arguments one per row; the parent result id is shown as the source, not as a parameter. */
export function callParams(params: Record<string, unknown>): CalculationParam[] {
  return Object.entries(params).filter(([name]) => name !== 'source_result_id').map(([name, value]) => ({ name, value: literal(value) }));
}

/** Calls a dashboard widget repeats on refresh: one call, or the recipe steps up to its output step. */
export function widgetRecipe(widget: Pick<Schemas['Widget'], 'tool_call' | 'recipe'>): string[] {
  if (widget.tool_call) return [toolCall(widget.tool_call.tool, widget.tool_call.params ?? {})];
  return (widget.recipe?.steps ?? []).map((step) => toolCall(step.call.tool, step.call.params ?? {}));
}

/**
 * «Как посчитано» for a saved result. `current` is the dataset's catalog revision now:
 * a newer one means the catalog was re-profiled after this result was computed (B1-11).
 */
export function calculation(result: Schemas['ToolResultMeta'], current?: Schemas['CatalogRevision'] | null): Calculation {
  const read = result.read ?? null;
  const notes = [...limitations(result.quality) ?? [], ...readNotes(read, current)];
  return {
    tool: result.tool ?? 'unknown_tool',
    params: callParams(result.params ?? {}),
    columns: (result.data?.columns ?? []).map((c) => typeof c === 'string' ? { label: c, name: c } : { label: c.label ?? c.name, name: c.name }),
    sql: result.sql ?? '—',
    origin: result.source_result_id ?? undefined,
    stats: result.data?.row_count == null ? undefined : `${rowsLabel(result.data.row_count)} в результате`,
    source: readSource(read),
    limitations: notes.length > 0 ? notes : undefined,
  };
}

/** Where and when the data of a result was read: «Живое чтение 26.10.2026 14:03 · каталог r3». */
export function readSource(read: Schemas['ReadProvenance'] | null): string | undefined {
  if (!read) return undefined;
  if (read.read_mode === 'unknown' || !read.finished_at) return 'Версия каталога и время чтения не сохранены';
  const revision = read.catalog_revision == null ? 'версия каталога неизвестна' : `каталог r${read.catalog_revision}`;
  if (read.read_mode === 'live') return `Живое чтение источника ${formatDateTime(read.finished_at)} · ${revision}`;
  const profiled = read.freshness.profiled_at;
  return `Снимок датасета · ${revision}${profiled ? ` · профиль от ${formatDateTime(profiled)}` : ''}`;
}

function readNotes(read: Schemas['ReadProvenance'] | null, current?: Schemas['CatalogRevision'] | null): string[] {
  if (!read) return [];
  const notes: string[] = [];
  const { state, reason } = read.freshness;
  if ((state === 'aged' || state === 'drifted' || state === 'unknown') && reason) notes.push(reason);
  if (current && read.catalog_revision != null && current.number > read.catalog_revision) {
    notes.push(`Каталог обновлён до r${current.number} после расчёта: результат показан как был сохранён, без пересчёта.`);
  }
  return notes;
}

const FRESHNESS: Record<Schemas['SourceFreshness']['state'], string> = {
  fresh: 'схема источника совпадает с каталогом',
  aged: 'профиль устарел',
  drifted: 'схема источника изменилась',
  incompatible: 'каталог несовместим с источником',
  unknown: 'версия каталога неизвестна',
};

/** «Каталог r2 · профиль от 26.10.2026 09:30 · схема источника совпадает с каталогом». */
export function revisionSummary(d: Schemas['DatasetDetail']): string {
  const parts = [d.revision ? `Каталог r${d.revision.number} · профиль от ${formatDateTime(d.revision.profiled_at)}` : 'Версия каталога неизвестна'];
  if (d.freshness && d.freshness.state !== 'unknown') parts.push(FRESHNESS[d.freshness.state]);
  return parts.join(' · ');
}

/** The source needs re-profiling before its catalog can be trusted again. */
export function needsRefresh(d: Schemas['DatasetDetail']): boolean {
  return !d.revision || (d.freshness?.state ?? 'fresh') !== 'fresh';
}

/** Data limitations behind a result (B2-10); informational notes stay out. */
export function limitations(quality: Schemas['ToolResultMeta']['quality']): string[] | undefined {
  if (!quality) return undefined;
  const lines = quality.lineage === 'partial'
    ? ['Не все колонки расчёта удалось связать с данными: ограничения могут быть неполными.']
    : [];
  for (const column of quality.columns ?? []) {
    for (const issue of column.issues ?? []) {
      if (issue.severity !== 'info') lines.push(issue.message);
    }
  }
  return lines.length > 0 ? lines : undefined;
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
