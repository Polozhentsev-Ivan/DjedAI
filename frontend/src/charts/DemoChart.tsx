import { Chart, type ChartHeight } from './Chart';
import { categoriesOption, REGIONS, regionsOption, revenueOption } from './demo';

export type DemoChartName = 'revenue' | 'regions' | 'categories';

interface DemoChartProps {
  name: DemoChartName;
  height?: ChartHeight | undefined;
}

const labels: Record<DemoChartName, string> = {
  revenue: 'Линейный график выручки по месяцам',
  regions: 'Кольцевая диаграмма долей выручки по регионам',
  categories: 'Столбчатая диаграмма выручки по категориям',
};

const options = { revenue: revenueOption, regions: regionsOption, categories: categoriesOption };

/** Static charts from the mockup, used on fixtures until ChartSpec rendering (B4-5). */
export function DemoChart({ name, height = 'lg' }: DemoChartProps) {
  if (name !== 'regions') return <Chart option={options[name]} label={labels[name]} height={height} />;
  return (
    <div className="flex items-center gap-4">
      <Chart option={regionsOption} label={labels.regions} height={height} className="w-[45%] flex-none" />
      <ul className="m-0 flex min-w-0 list-none flex-col gap-3 p-0 text-xs text-foreground">
        {REGIONS.map(([region, share], index) => (
          <li key={region} className="flex items-center gap-2">
            <span aria-hidden="true" className="size-2 flex-none rounded-full" style={{ background: `var(--chart-${index + 1})` }} />
            <span className="truncate">{region}</span>
            <span className="tabular text-secondary-foreground">{share}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
