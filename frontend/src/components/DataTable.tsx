import type { CSSProperties, ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { LinkButton } from './Button';
import { StatusBadge, type BadgeTone } from './StatusBadge';

export type ColumnType = 'text' | 'strong' | 'mono' | 'number' | 'muted' | 'name' | 'badge' | 'type' | 'actions';
export type CellTone = 'muted' | 'warning' | 'danger' | 'success' | 'secondary';

export interface Column {
  key: string;
  label: string;
  type?: ColumnType | undefined;
  align?: 'left' | 'right' | undefined;
  /** CSS grid track; the only hardcoded size allowed by the mockup. */
  width?: string | undefined;
}

export interface CellObject {
  text: string;
  tone?: CellTone | BadgeTone | undefined;
  strong?: boolean | undefined;
  /** Small accent tag next to a name, e.g. «активный». */
  tag?: string | undefined;
}

export interface CellAction {
  label: string;
  onClick: () => void;
  tone?: 'danger' | undefined;
}

export type Cell = string | CellObject | readonly CellAction[];

export interface Row {
  id: string;
  cells: Record<string, Cell | undefined>;
  highlight?: boolean | undefined;
}

interface DataTableProps {
  columns: readonly Column[];
  rows: readonly Row[];
  minWidth?: string | undefined;
  label?: string | undefined;
}

const toneInk: Record<CellTone, string> = {
  muted: 'text-muted-foreground',
  warning: 'text-warning',
  danger: 'text-destructive',
  success: 'text-success',
  secondary: 'text-secondary-foreground',
};

function isActions(cell: Cell | undefined): cell is readonly CellAction[] {
  return Array.isArray(cell);
}

function asObject(cell: Cell | undefined): CellObject {
  if (cell === undefined) return { text: '' };
  if (typeof cell === 'string') return { text: cell };
  if (isActions(cell)) return { text: '' };
  return cell;
}

function renderCell(column: Column, cell: Cell | undefined): ReactNode {
  const type = column.type ?? 'text';
  if (type === 'actions') {
    const actions = isActions(cell) ? cell : [];
    return (
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {actions.map((action) => (
          <LinkButton key={action.label} tone={action.tone === 'danger' ? 'danger' : 'link'} onClick={action.onClick}>
            {action.label}
          </LinkButton>
        ))}
      </div>
    );
  }
  const value = asObject(cell);
  if (type === 'name') {
    return (
      <span className="inline-flex flex-wrap items-center gap-2">
        <span className="font-mono font-semibold">{value.text}</span>
        {value.tag && <StatusBadge variant="tag" tone="accent" label={value.tag} />}
      </span>
    );
  }
  if (type === 'badge' || type === 'type') {
    const tone = (value.tone ?? 'neutral') as BadgeTone;
    return <StatusBadge tone={tone} label={value.text} variant={type === 'type' ? 'type' : 'status'} />;
  }
  const tone = value.tone && value.tone in toneInk ? toneInk[value.tone as CellTone] : undefined;
  return (
    <span
      className={cn(
        'tabular',
        tone ?? (type === 'mono' ? 'text-muted-foreground' : type === 'muted' ? 'text-secondary-foreground' : 'text-foreground'),
        type === 'strong' || value.strong ? 'font-semibold' : 'font-normal',
        type === 'mono' ? 'font-mono text-xs' : 'text-sm',
        type === 'number' ? 'whitespace-nowrap' : 'whitespace-normal',
      )}
    >
      {value.text}
    </span>
  );
}

/** Grid-based table from the mockup; columns are CSS grid tracks. */
export function DataTable({ columns, rows, minWidth = '0', label }: DataTableProps) {
  const grid: CSSProperties = { gridTemplateColumns: columns.map((c) => c.width ?? 'minmax(0,1fr)').join(' ') };
  return (
    <div className="overflow-x-auto text-sm leading-normal tracking-sm text-foreground">
      <div role="table" aria-label={label} style={{ minWidth }}>
        <div role="row" className="grid gap-4 border-b border-border px-5 py-2.5 text-xs font-semibold text-muted-foreground" style={grid}>
          {columns.map((column) => (
            <span key={column.key} role="columnheader" className={column.align === 'right' ? 'text-right' : 'text-left'}>
              {column.label}
            </span>
          ))}
        </div>
        {rows.map((row) => (
          <div
            key={row.id}
            role="row"
            className={cn('grid items-center gap-4 border-b border-border-subtle px-5 py-3', row.highlight ? 'bg-secondary' : 'bg-transparent')}
            style={grid}
          >
            {columns.map((column) => (
              <div key={column.key} role="cell" className={cn('min-w-0', column.align === 'right' ? 'text-right' : 'text-left')}>
                {renderCell(column, row.cells[column.key])}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
