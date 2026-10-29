import * as Collapsible from '@radix-ui/react-collapsible';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useState } from 'react';
import { apiUrl } from '@/api/client';
import { cn } from '@/lib/cn';
import { Icon } from './Icon';

export interface CalculationColumn {
  label: string;
  name: string;
}

export interface CalculationParam {
  name: string;
  value: string;
}

export interface Calculation {
  tool: string;
  /** Arguments of the tool call, one per row, as they were passed. */
  params?: readonly CalculationParam[] | undefined;
  columns: readonly CalculationColumn[];
  sql: string;
  /** Result this one was computed from (`source_result_id`): the previous step of a chain. */
  origin?: string | undefined;
  stats?: string | undefined;
  /** Where and when the data was read and against which catalog revision (B1-11). */
  source?: string | undefined;
  /** Data limitations of the result: typing losses, ambiguities, incomplete lineage. */
  limitations?: readonly string[] | undefined;
}

interface HowCalculatedProps extends Calculation {
  variant?: 'collapsible' | 'panel' | undefined;
  summary?: string | undefined;
  defaultOpen?: boolean | undefined;
}

function Body({ tool, params, columns, sql, origin, stats, source, limitations, panel }: Calculation & { panel: boolean }) {
  const label = 'min-w-(--label-col) text-xs text-muted-foreground';
  return (
    <div className={cn('flex flex-col gap-3', panel ? 'px-2 pb-2 pt-1' : 'border-t border-border bg-secondary p-3.5')}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={label}>Инструмент</span>
        <code className="rounded-xs bg-muted px-2 py-0.5 font-mono text-xs">{tool}</code>
      </div>
      {params && params.length > 0 && (
        <div className="flex flex-wrap items-baseline gap-2">
          <span className={label}>Параметры</span>
          <ul aria-label="Параметры" className="m-0 flex flex-1 list-none flex-col gap-1 p-0">
            {params.map((param) => (
              <li key={param.name}><span className="font-mono text-xs text-muted-foreground">{param.name}</span> = <code className="font-mono text-xs">{param.value}</code></li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex flex-wrap items-baseline gap-2">
        <span className={label}>Колонки</span>
        <span className="flex flex-wrap gap-x-3 gap-y-1">
          {columns.map((column) => (
            <span key={column.name}>
              «{column.label}» <span className="font-mono text-xs text-muted-foreground">{column.name}</span>
            </span>
          ))}
        </span>
      </div>
      {stats && (
        <div className="flex flex-wrap items-baseline gap-2">
          <span className={label}>Обработано</span>
          <span className="tabular">{stats}</span>
        </div>
      )}
      {origin && (
        <div className="flex flex-wrap items-baseline gap-2">
          <span className={label}>Источник результата</span>
          <a href={apiUrl(`/results/${encodeURIComponent(origin)}`)} target="_blank" rel="noreferrer" className="font-mono text-xs">{origin}</a>
        </div>
      )}
      {source && (
        <div className="flex flex-wrap items-baseline gap-2">
          <span className={label}>Данные</span>
          <span>{source}</span>
        </div>
      )}
      {limitations && limitations.length > 0 && (
        <div className="flex flex-wrap items-baseline gap-2">
          <span className={label}>Ограничения</span>
          <ul className="m-0 flex flex-1 list-disc flex-col gap-1 pl-4">
            {limitations.map((line) => <li key={line}>{line}</li>)}
          </ul>
        </div>
      )}
      <pre className="m-0 overflow-auto whitespace-pre rounded-sm bg-code px-3.5 py-3 font-mono text-xs leading-code text-foreground">{sql}</pre>
    </div>
  );
}

/** «Как посчитано»: tool, parameters, columns, SQL and source result behind an answer or a widget. */
export function HowCalculated({ variant = 'collapsible', summary, defaultOpen = false, ...calculation }: HowCalculatedProps) {
  const [open, setOpen] = useState(defaultOpen);
  const base = 'overflow-hidden rounded-md text-sm leading-normal tracking-sm text-foreground';
  if (variant === 'panel') {
    return (
      <div className={base}>
        <Body {...calculation} panel />
      </div>
    );
  }
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen} className={cn(base, 'border border-border')}>
      <Collapsible.Trigger className="flex w-full items-center gap-2 bg-card px-3.5 py-2.5 text-left text-sm text-foreground">
        <Icon icon={open ? ChevronDown : ChevronRight} size="sm" className="text-muted-foreground" />
        <span className="font-semibold">Как посчитано</span>
        {summary && <span className="ml-auto text-xs text-muted-foreground">{summary}</span>}
      </Collapsible.Trigger>
      <Collapsible.Content>
        <Body {...calculation} panel={false} />
      </Collapsible.Content>
    </Collapsible.Root>
  );
}
