import { DataTable, type Column, type Row } from '@/components/DataTable';
import { formatDate } from '@/lib/format';
import { column, numeric, type Data, type Spec } from './data';
import { measureLabel } from './format';

const ISO_TIMESTAMP = /^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?:[T ]([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d)(\.\d+)?)?(Z|[+-]\d{2}(?::?\d{2})?)?)?$/;

/** 0 — date, 1 — minutes, 2 — seconds, 3 — fractions of a second. */
type Precision = 0 | 1 | 2 | 3;

function precision(value: string): Precision | null {
  const match = ISO_TIMESTAMP.exec(value);
  if (!match) return null;
  const [, , , , hours = '00', minutes = '00', seconds = '00', fraction = ''] = match;
  if (/[1-9]/.test(fraction)) return 3;
  if (seconds !== '00') return 2;
  return hours !== '00' || minutes !== '00' ? 1 : 0;
}

/**
 * The finest time any ISO value in the column carries, so 09:00 and 10:00 stay apart while a
 * column of plain dates (or midnights) shows dates only; null when the column holds no ISO values.
 */
function columnPrecision(data: Data, index: number): Precision | null {
  let finest: Precision | null = null;
  for (const row of data.rows ?? []) {
    const value: unknown = row[index];
    const found = typeof value === 'string' ? precision(value) : null;
    if (found !== null && (finest === null || found > finest)) finest = found;
  }
  return finest;
}

/** The time exactly as stored, not shifted to the viewer's zone; an explicit offset is named instead. */
function timestampLabel(value: string, level: Precision): string | null {
  const match = ISO_TIMESTAMP.exec(value);
  if (!match) return null;
  const [, year, month, day, hours = '00', minutes = '00', seconds = '00', fraction = '', offset] = match;
  const date = `${day}.${month}.${year}`;
  if (level === 0) return date;
  const time = `${hours}:${minutes}${level >= 2 ? `:${seconds}` : ''}${level === 3 ? fraction : ''}`;
  const zone = offset === 'Z' ? ' UTC' : offset ? ` ${offset}` : '';
  return `${date} ${time}${zone}`;
}

/** A column the spec does not describe is numeric when every present value is a number. */
function numericColumn(data: Data, index: number): boolean {
  const present = (data.rows ?? []).map((row) => row[index]).filter((value) => value !== null && value !== undefined);
  return present.length > 0 && present.every(numeric);
}

/**
 * Column order and labels come from the spec, or from an explicit `fields` projection (a table block);
 * an empty projection shows all result columns. Unknown fields are skipped rather than shown empty.
 */
export function ResultTable({ spec, data, fields: projection }: { spec: Spec; data: Data; fields?: readonly string[] | undefined }) {
  const requested = projection ?? [spec.x?.field, ...(spec.y ?? []).map((m) => m.field), spec.color?.field].filter((f): f is string => f !== undefined);
  const all = (data.columns ?? []).map((c) => typeof c === 'string' ? c : c.name);
  const known = requested.filter((field) => all.includes(field));
  const fields = known.length > 0 ? [...new Set(known)] : all;
  const columns: Column[] = fields.map((field) => {
    const measure = spec.y?.find((m) => m.field === field);
    const descriptor = data.columns?.[column(data, field)];
    const described = measure !== undefined || field === spec.x?.field || field === spec.color?.field;
    const number = measure !== undefined || (!described && numericColumn(data, column(data, field)));
    const label = field === spec.x?.field ? spec.x.label : field === spec.color?.field ? spec.color.label : measure?.label;
    return { key: field, label: label ?? (typeof descriptor === 'object' ? descriptor.label : undefined) ?? field, type: number ? 'number' : 'text', align: number ? 'right' : 'left', width: 'minmax(120px,1fr)' };
  });
  // Table blocks have no spec, so any ISO column is formatted; a chart table only formats its time axis.
  const timeLevels = new Map(fields.map((field) => [field, projection || (field === spec.x?.field && spec.x.kind === 'time') ? columnPrecision(data, column(data, field)) : null]));
  const rows: Row[] = (data.rows ?? []).map((row, i) => ({
    id: String(i), cells: Object.fromEntries(fields.map((field) => {
      const value: unknown = row[column(data, field)];
      const measure = spec.y?.find((m) => m.field === field);
      let text = value === null || value === undefined ? '—' : typeof value === 'string' ? value : JSON.stringify(value);
      if (numeric(value)) text = measureLabel(value, measure, 0);
      const level = timeLevels.get(field) ?? null;
      const stamp = typeof value === 'string' && level !== null ? timestampLabel(value, level) : null;
      if (stamp !== null) text = stamp;
      else if (field === spec.x?.field && spec.x.kind === 'time' && typeof value === 'string' && !Number.isNaN(new Date(value).getTime())) text = formatDate(value);
      return [field, text];
    })),
  }));
  return <DataTable columns={columns} rows={rows} label={spec.title} />;
}
