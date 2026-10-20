import { Download, Plus } from 'lucide-react';
import type { ReactNode } from 'react';
import { Button } from './Button';
import { Icon } from './Icon';

interface ChartCardProps {
  title: string;
  children: ReactNode;
  subtitle?: string | undefined;
  primaryLabel?: string | undefined;
  onPrimary?: (() => void) | undefined;
  secondaryLabel?: string | undefined;
  onSecondary?: (() => void) | undefined;
}

/** Chart block of an answer: title, actions «+ В дашборд» / «Экспорт» and the chart itself. */
export function ChartCard({ title, subtitle, children, primaryLabel, onPrimary, secondaryLabel, onSecondary }: ChartCardProps) {
  return (
    <section className="rounded-lg border border-border bg-card px-4 pb-2 pt-4 text-sm leading-normal tracking-sm text-card-foreground">
      <div className="flex flex-wrap items-start gap-2.5 px-1">
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-semibold leading-snug tracking-body">{title}</h3>
          {subtitle && <div className="mt-0.5 text-xs tracking-xs text-muted-foreground">{subtitle}</div>}
        </div>
        {primaryLabel && (
          <Button className="gap-1 px-3.5 py-1.5" onClick={onPrimary}>
            <Icon icon={Plus} size="sm" />
            {primaryLabel}
          </Button>
        )}
        {secondaryLabel && (
          <Button variant="outline" className="gap-1 px-3 py-1" onClick={onSecondary}>
            <Icon icon={Download} size="sm" />
            {secondaryLabel}
          </Button>
        )}
      </div>
      {children}
    </section>
  );
}
