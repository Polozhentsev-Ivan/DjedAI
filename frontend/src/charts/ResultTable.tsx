import { DataTable, type Column, type Row } from '@/components/DataTable';
import { formatDate } from '@/lib/format';
import { column, numeric, type Data, type Spec } from './data';
import { measureLabel } from './format';

/** Column order and labels come from the spec; an empty projection shows all result columns. */
export function ResultTable({ spec, data }: { spec: Spec; data: Data }) {
  const requested = [spec.x?.field, ...(spec.y ?? []).map((m) => m.field), spec.color?.field].filter((f): f is string => f !== undefined);
  const fields = requested.length > 0 ? [...new Set(requested)] : (data.columns ?? []).map((c) => typeof c === 'string' ? c : c.name);
  const columns: Column[] = fields.map((field) => {
    const measure = spec.y?.find((m) => m.field === field);
    const descriptor = data.columns?.[column(data, field)];
    const label = field === spec.x?.field ? spec.x.label : field === spec.color?.field ? spec.color.label : measure?.label;
    return { key: field, label: label ?? (typeof descriptor === 'object' ? descriptor.label : undefined) ?? field, type: measure ? 'number' : 'text', align: measure ? 'right' : 'left', width: 'minmax(120px,1fr)' };
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
