import { useCallback } from 'react';
import type { Schemas } from '@/api/client';
import { EmptyState } from '@/components/StateBlock';
import { Chart, type ChartHeight } from './Chart';
import { isSupported, pieLegend, specOption } from './spec';
import type { ChartTheme } from './theme';

interface SpecChartProps {
  spec: Schemas['ChartSpec'];
  data: Schemas['ResultData'];
  height?: ChartHeight | undefined;
}

/** Chart described by a ChartSpec over the rows of its result. */
export function SpecChart({ spec, data, height = 'lg' }: SpecChartProps) {
  const option = useCallback((theme: ChartTheme) => specOption(spec, data, theme), [spec, data]);
  const label = spec.subtitle ? `${spec.title}. ${spec.subtitle}` : spec.title;

  if (!isSupported(spec.type)) {
    return <EmptyState size="compact" title="Этот тип графика пока не отображается" text={`Тип «${spec.type}» добавит задача рендерера графиков.`} />;
  }
  if (data.rows.length === 0) return <EmptyState size="compact" title="Нет данных для графика" />;
  if (spec.type !== 'pie') return <Chart option={option} label={label} height={height} />;
  return (
    <div className="flex items-center gap-4">
      <Chart option={option} label={label} height={height} className="w-1/2 flex-none" />
      <ul className="m-0 flex min-w-0 list-none flex-col gap-3 p-0 text-xs text-foreground">
        {pieLegend(spec, data).map((item) => (
          <li key={item.label} className="flex items-center gap-2">
            <span aria-hidden="true" className="size-2 flex-none rounded-full" style={{ background: item.color }} />
            <span className="truncate">{item.label}</span>
            <span className="tabular text-secondary-foreground">{item.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
