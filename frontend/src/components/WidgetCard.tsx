import { useState, type ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { LinkButton } from './Button';
import { HowCalculated, type Calculation } from './HowCalculated';
import { ErrorState, LoadingState } from './StateBlock';
import { StatusBadge } from './StatusBadge';

export interface KpiStat {
  label: string;
  value: string;
  delta: string;
  tone: 'success' | 'danger';
}

export interface Kpi {
  caption: string;
  value: string;
  delta: string;
  deltaTone: 'success' | 'danger';
  target: string;
  stats: readonly KpiStat[];
}

export type WidgetState = 'default' | 'loading' | 'error';

interface WidgetCardProps {
  title: string;
  subtitle: string;
  how: Calculation;
  /** Chart content; omit it for a KPI widget. */
  chart?: ReactNode;
  kpi?: Kpi | undefined;
  state?: WidgetState | undefined;
  onRemove?: (() => void) | undefined;
  onRetry?: (() => void) | undefined;
  errorTitle?: string | undefined;
  errorText?: string | undefined;
}

function KpiBody({ kpi }: { kpi: Kpi }) {
  return (
    <div className="flex flex-1 flex-col justify-center gap-2.5 px-2 pb-4 pt-2">
      <span className="text-xs text-muted-foreground">{kpi.caption}</span>
      <div className="flex flex-wrap items-baseline gap-3">
        <span className="tabular font-display text-3xl font-semibold leading-tight tracking-display">{kpi.value}</span>
        {kpi.delta && <StatusBadge tone={kpi.deltaTone} label={kpi.delta} dot={false} />}
      </div>
      {kpi.target && <span className="text-muted-foreground">{kpi.target}</span>}
      {kpi.stats.length > 0 && (
      <div className="mt-2 grid grid-cols-2 gap-3 border-t border-border-subtle pt-3">
        {kpi.stats.map((stat) => (
          <div key={stat.label}>
            <div className="text-xs text-muted-foreground">{stat.label}</div>
            <div className="tabular text-base font-semibold">
              {stat.value}{' '}
              <span className={cn('text-sm', stat.tone === 'success' ? 'text-success' : 'text-destructive')}>{stat.delta}</span>
            </div>
          </div>
        ))}
      </div>
      )}
    </div>
  );
}

/** Dashboard widget: chart or KPI; «Как посчитано» replaces the content while open. */
export function WidgetCard({
  title, subtitle, how, chart, kpi, state = 'default', onRemove, onRetry,
  errorTitle = 'Ошибка обновления данных', errorText = 'Источник не ответил за 30 сек.',
}: WidgetCardProps) {
  const [showHow, setShowHow] = useState(false);
  const error = state === 'error';
  return (
    <section
      aria-busy={state === 'loading'}
      className="relative box-border flex h-full min-h-(--widget-min-height) flex-col overflow-hidden rounded-lg border border-border bg-card text-sm leading-normal tracking-sm text-card-foreground"
    >
      {state === 'loading' && (
        <div className="absolute inset-0 z-[2] flex items-center justify-center bg-overlay">
          <LoadingState size="compact" title="Обновление данных…" />
        </div>
      )}
      <div className="flex items-start gap-3 px-5 pb-2 pt-4">
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-semibold leading-snug tracking-body">{title}</h3>
          <div className="mt-0.5 text-muted-foreground">{subtitle}</div>
        </div>
        <div className="flex flex-none gap-3 pt-0.5">
          <LinkButton aria-expanded={showHow} onClick={() => { setShowHow((open) => !open); }}>
            {showHow ? 'Скрыть расчёт' : 'Как посчитано'}
          </LinkButton>
          <LinkButton tone="muted" onClick={onRemove}>Удалить</LinkButton>
        </div>
      </div>
      <div className="relative flex flex-1 flex-col px-3 pb-3">
        {error && (
          <div className="flex min-h-(--chart-height-md) flex-1 items-center justify-center">
            <ErrorState size="compact" title={errorTitle} text={errorText} onAction={onRetry} />
          </div>
        )}
        {!error && showHow && <HowCalculated variant="panel" {...how} />}
        {!error && !showHow && (kpi ? <KpiBody kpi={kpi} /> : chart)}
      </div>
    </section>
  );
}
