import { DataTable, type Column, type Row } from '@/components/DataTable';
import { formatDate } from '@/lib/format';
import { column, numeric, type Data, type Spec } from './data';
import { measureLabel } from './format';

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
  const rows: Row[] = (data.rows ?? []).map((row, i) => ({
    id: String(i), cells: Object.fromEntries(fields.map((field) => {
      const value: unknown = row[column(data, field)];
      const measure = spec.y?.find((m) => m.field === field);
      let text = value === null || value === undefined ? '—' : typeof value === 'string' ? value : JSON.stringify(value);
      if (numeric(value)) text = measureLabel(value, measure, 0);
      if (field === spec.x?.field && spec.x.kind === 'time' && typeof value === 'string' && !Number.isNaN(new Date(value).getTime())) text = formatDate(value);
      return [field, text];
    })),
  }));
  return <DataTable columns={columns} rows={rows} label={spec.title} />;
}
