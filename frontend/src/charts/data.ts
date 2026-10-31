import type { Schemas } from '@/api/client';

export type Spec = Schemas['ChartTemplate'];
export type Data = Schemas['ResultData'];
export type Measure = Schemas['ChartMeasure'];
export type Cell = string | number | boolean | null;

export function column(data: Data, field: string | undefined): number {
  return field === undefined ? -1 : (data.columns ?? []).findIndex((c) => (typeof c === 'string' ? c : c.name) === field);
}

export function values(data: Data, index: number): Cell[] {
  return (data.rows ?? []).map((row) => {
    const value: unknown = row[index];
    if (value === undefined || value === null) return null;
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
    return JSON.stringify(value);
  });
}

export const categoryLabel = (value: Cell): string => value === null ? 'Нет значения' : String(value);
export const numeric = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const textValue = (value: unknown): string => typeof value === 'object' ? JSON.stringify(value) : typeof value === 'string' ? value : JSON.stringify(value);

/** Above this the browser slows down and points stop being readable; the note under the chart says so. */
export const MAX_CHART_ROWS = 2000;

/** Sort and limit the returned rows only; never aggregate or mutate the saved result. */
export function displayData(spec: Spec, data: Data): Data {
  const rows = [...(data.rows ?? [])];
  if (spec.sort) {
    const index = column(data, spec.sort.field);
    const direction = spec.sort.order === 'asc' ? 1 : -1;
    rows.sort((a, b) => {
      const left: unknown = a[index];
      const right: unknown = b[index];
      if (left === null || left === undefined) return right === null || right === undefined ? 0 : 1;
      if (right === null || right === undefined) return -1;
      const order = numeric(left) && numeric(right) ? left - right : textValue(left).localeCompare(textValue(right), 'ru', { numeric: true });
      return order * direction;
    });
  }
  return { ...data, rows: rows.slice(0, Math.min(spec.limit ?? MAX_CHART_ROWS, MAX_CHART_ROWS)) };
}

/** Explain incompatible shapes instead of inventing missing columns, bins or statistics. */
export function dataError(spec: Spec, data: Data): string | undefined {
  const measures = spec.y ?? [];
  const fields = [spec.x?.field, ...measures.map((m) => m.field), spec.color?.field, spec.sort?.field].filter((f): f is string => f !== undefined);
  if (fields.some((field) => column(data, field) < 0)) return 'В результате нет одного из полей спецификации. Проверьте выбранные колонки.';
  if (spec.type === 'table') return (data.columns ?? []).length === 0 ? 'В результате нет описания колонок.' : undefined;
  if (measures.length === 0) return 'Для отображения нужен хотя бы один числовой показатель.';
  if (spec.type === 'kpi') {
    if (measures.length !== 1 || (data.rows ?? []).length !== 1 || data.truncated) return 'Для KPI нужен полный результат с одной строкой и одним показателем. Агрегат рассчитывается на сервере.';
  } else if (!spec.x) return 'Для графика нужно поле оси X.';
  if (['pie', 'histogram', 'heatmap'].includes(spec.type) && measures.length !== 1) return 'Для этого типа графика нужен один числовой показатель.';
  if (spec.type === 'heatmap' && !spec.color) return 'Для тепловой карты нужны две категории и один числовой показатель.';
  if (spec.color && ['pie', 'histogram', 'box', 'kpi'].includes(spec.type)) return 'Цветовая разбивка недоступна для этого типа. Покажите группы отдельными графиками.';
  if (spec.type === 'box' && measures.length !== 5) return 'Для box plot нужны пять рассчитанных сервером показателей: нижний ус, Q1, медиана, Q3, верхний ус — в этом порядке.';
  const indexes = measures.map((m) => column(data, m.field));
  const numericIndexes = spec.type === 'scatter' || spec.x?.kind === 'number' ? [...indexes, column(data, spec.x?.field)] : indexes;
  if (numericIndexes.some((index) => values(data, index).some((value) => value !== null && !numeric(value)))) return 'Показатели должны содержать числа или пропуски, без текстовых значений.';
  if (spec.type === 'histogram' && values(data, indexes[0] ?? -1).some((v) => numeric(v) && v < 0)) return 'Частоты готовых интервалов гистограммы не могут быть отрицательными.';
  if (spec.type === 'pie' && values(data, indexes[0] ?? -1).some((v) => numeric(v) && v < 0)) return 'Для круговой диаграммы нужны неотрицательные значения.';
  if (spec.type === 'box') {
    if (new Set(measures.map((m) => m.format ?? 'number')).size > 1) return 'Все пять показателей box plot должны использовать один числовой формат.';
    if ((data.rows ?? []).some((row) => indexes.some((index, i) => {
      const value: unknown = row[index];
      const previous: unknown = row[indexes[i - 1] ?? -1];
      return !numeric(value) || (i > 0 && numeric(previous) && value < previous);
    }))) return 'Статистики box plot должны быть полными и идти по возрастанию: нижний ус ≤ Q1 ≤ медиана ≤ Q3 ≤ верхний ус.';
  }
  if (spec.color && spec.type !== 'scatter' && spec.type !== 'box' && spec.type !== 'kpi') {
    const x = column(data, spec.x?.field);
    const color = column(data, spec.color.field);
    const keys = (data.rows ?? []).map((row) => JSON.stringify([row[x], row[color]]));
    if (new Set(keys).size !== keys.length) return 'Для пары категорий есть несколько строк. Подготовьте агрегат на сервере перед визуализацией.';
  }
  return undefined;
}
