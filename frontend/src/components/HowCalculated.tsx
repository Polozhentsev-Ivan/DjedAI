import * as Collapsible from '@radix-ui/react-collapsible';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useState } from 'react';
import { cn } from '@/lib/cn';
import { Icon } from './Icon';

export interface CalculationColumn {
  label: string;
  name: string;
}

export interface Calculation {
  tool: string;
  columns: readonly CalculationColumn[];
  sql: string;
  stats?: string | undefined;
}

interface HowCalculatedProps extends Calculation {
  variant?: 'collapsible' | 'panel' | undefined;
  summary?: string | undefined;
  defaultOpen?: boolean | undefined;
}

function Body({ tool, columns, sql, stats, panel }: Calculation & { panel: boolean }) {
  const label = 'min-w-(--label-col) text-xs text-muted-foreground';
  return (
    <div className={cn('flex flex-col gap-3', panel ? 'px-2 pb-2 pt-1' : 'border-t border-border bg-secondary p-3.5')}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={label}>Инструмент</span>
        <code className="rounded-xs bg-muted px-2 py-0.5 font-mono text-xs">{tool}</code>
      </div>
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
      <pre className="m-0 overflow-auto whitespace-pre rounded-sm bg-code px-3.5 py-3 font-mono text-xs leading-code text-foreground">{sql}</pre>
    </div>
  );
}

/** «Как посчитано»: tool, columns and SQL behind an answer or a widget. */
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
