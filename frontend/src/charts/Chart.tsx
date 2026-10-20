import { useEffect, useRef } from 'react';
import { cn } from '@/lib/cn';
import { useTheme } from '@/lib/theme';
import { echarts, type EChartsCoreOption } from './echarts';
import { readChartTheme, type ChartTheme } from './theme';

export type ChartHeight = 'lg' | 'md' | 'sm';

const heights: Record<ChartHeight, string> = {
  lg: 'h-(--chart-height)',
  md: 'h-(--chart-height-md)',
  sm: 'h-(--chart-height-sm)',
};

interface ChartProps {
  /** Builds the option from theme tokens; keep it stable (module-level) to avoid redraws. */
  option: (theme: ChartTheme) => EChartsCoreOption;
  label: string;
  height?: ChartHeight | undefined;
  className?: string | undefined;
}

/** ECharts canvas bound to the design tokens: redraws on theme change, resizes with its box. */
export function Chart({ option, label, height = 'lg', className }: ChartProps) {
  const ref = useRef<HTMLDivElement>(null);
  const theme = useTheme();

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const chart = echarts.init(element, null, { renderer: 'svg' });
    const observer = new ResizeObserver(() => { chart.resize(); });
    observer.observe(element);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, []);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    echarts.getInstanceByDom(element)?.setOption(option(readChartTheme(element)), true);
  }, [option, theme]);

  return <div ref={ref} role="img" aria-label={label} className={cn('w-full', heights[height], className)} />;
}
