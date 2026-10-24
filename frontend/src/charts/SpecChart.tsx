import { useCallback } from 'react';
import type { Schemas } from '@/api/client';
import { EmptyState, ErrorState } from '@/components/StateBlock';
import { Chart, type ChartHeight } from './Chart';
import { column, dataError, displayData, numeric } from './data';
import { measureLabel } from './format';
import { ResultTable } from './ResultTable';
import { isSupported, pieLegend, specOption } from './spec';
import type { ChartTheme } from './theme';

interface SpecChartProps {
  spec: Schemas['ChartTemplate'];
  data: Schemas['ResultData'];
  height?: ChartHeight | undefined;
}

/** Chart described by a ChartSpec over the rows of its result. */
export function SpecChart({ spec, data, height = 'lg' }: SpecChartProps) {
  const option = useCallback((theme: ChartTheme) => specOption(spec, data, theme), [spec, data]);
  const label = spec.subtitle ? `${spec.title}. ${spec.subtitle}` : spec.title;

  if (!isSupported(spec.type)) {
    return <ErrorState size="compact" title="Неизвестный тип графика" actionLabel="" />;
  }
  if ((data.rows ?? []).length === 0) return <EmptyState size="compact" title="Нет данных для графика" />;
  const error = dataError(spec, data);
  if (error) return <ErrorState size="compact" title="Не удалось построить визуализацию" text={error} actionLabel="" />;
  const shown = displayData(spec, data);
  const numericRows = (shown.rows ?? []).some((row) => {
    const x: unknown = row[column(shown, spec.x?.field)];
    return (spec.y ?? []).some((m) => {
      const value: unknown = row[column(shown, m.field)];
      return numeric(value) && (spec.type !== 'pie' || value > 0) && (spec.type !== 'scatter' || numeric(x));
    });
  });
  if (spec.type !== 'table' && spec.type !== 'kpi' && !numericRows) return <EmptyState size="compact" title="Нет значений для графика" text="В выбранных строках нет доступных числовых значений." />;
  const limited = data.truncated || (data.rows ?? []).length > (shown.rows ?? []).length;
  let content;
  if (spec.type === 'table') content = <ResultTable spec={spec} data={shown} />;
  else if (spec.type === 'kpi') {
    const measure = spec.y?.[0];
    const value: unknown = data.rows?.[0]?.[column(data, measure?.field)];
    content = <div role="group" aria-label={label} className="flex flex-col justify-center gap-2.5 px-2 pb-4 pt-2">
      <span className="text-xs text-muted-foreground">{measure?.label ?? measure?.field}</span>
      <span className="tabular font-display text-3xl font-semibold leading-tight tracking-display">{measureLabel(numeric(value) ? value : null, measure, numeric(value) ? Math.abs(value) : 0)}</span>
    </div>;
  } else if (spec.type !== 'pie') content = <Chart option={option} label={label} height={height} />;
  else content = <Pie spec={spec} data={data} option={option} label={label} height={height} />;
  return <div className="min-w-0">
    {content}
    {limited && <p className="px-2 pb-2 text-xs text-muted-foreground">Показана часть результата: {shown.rows?.length ?? 0} строк. Сортировка и лимит применены к доступным строкам; полный расчёт хранится на сервере.</p>}
  </div>;
}

function Pie({ spec, data, option, label, height }: SpecChartProps & { option: (theme: ChartTheme) => ReturnType<typeof specOption>; label: string; height: ChartHeight }) {
  return (
    <div className="flex flex-col items-center gap-4 sm:flex-row">
      <Chart option={option} label={label} height={height} className="flex-none sm:w-1/2" />
      <ul className="m-0 flex min-w-0 list-none flex-col gap-3 p-0 text-xs text-foreground">
        {pieLegend(spec, data).map((item) => (
          <li key={`${item.label}-${item.value}`} className="flex items-center gap-2">
            <span aria-hidden="true" className="size-2 flex-none rounded-full" style={{ background: item.color }} />
            <span className="truncate">{item.label}</span>
            <span className="tabular text-secondary-foreground">{item.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
