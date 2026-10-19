// Demo series from docs/ui-design/charts.js. Real charts come from ChartSpec in B4-5.
import { formatMillions } from '@/lib/format';
import type { EChartsCoreOption } from './echarts';
import type { ChartTheme } from './theme';

export const MONTHS = ['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн', 'Июл', 'Авг', 'Сен', 'Окт'];
export const REVENUE = [1800000, 2000000, 2200000, 2300000, 2500000, 2800000, 3400000, 2900000, 2700000, 2800000];
export const REGIONS: readonly (readonly [string, number])[] = [
  ['Москва', 45], ['Санкт-Петербург', 25], ['Казань', 12], ['Екатеринбург', 10], ['Другие', 8],
];
export const CATEGORIES: readonly (readonly [string, number])[] = [
  ['Электроника', 4100000], ['Бытовая техника', 2600000], ['Одежда', 1900000], ['Дом и сад', 1400000], ['Спорт', 900000],
];

/** Charts keep one decimal on every value, as in the mockup: «0,0 млн», «0,9 млн ₽». */
function millions(value: number): string {
  return formatMillions(value);
}

function rubles(value: number): string {
  return formatMillions(value, '₽');
}

interface AxisPoint { name: string; value: number }

function tooltip(t: ChartTheme) {
  return {
    backgroundColor: t.surface,
    borderColor: t.line,
    textStyle: { color: t.ink, fontSize: t.fontSize },
    extraCssText: `box-shadow:none;border-radius:${t.radius}`,
  };
}

export function revenueOption(t: ChartTheme): EChartsCoreOption {
  return {
    textStyle: { fontFamily: t.font, color: t.muted, fontSize: t.fontSize },
    color: [t.primary],
    grid: { left: 8, right: 20, top: 34, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' },
    tooltip: { ...tooltip(t), trigger: 'axis', formatter: (points: AxisPoint[]) => `${points[0]?.name ?? ''} 2026<br/><b>${rubles(points[0]?.value ?? 0)}</b>` },
    xAxis: { type: 'category', data: MONTHS, boundaryGap: false, axisLine: { lineStyle: { color: t.line } }, axisTick: { show: false }, axisLabel: { color: t.muted } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: t.line, type: 'dashed' } }, axisLabel: { color: t.muted, formatter: millions } },
    series: [{
      type: 'line', data: REVENUE, symbol: 'circle', symbolSize: 6, lineStyle: { width: 2 }, areaStyle: { opacity: 0.08 },
      markPoint: {
        symbol: 'circle', symbolSize: 10, data: [{ type: 'max' }],
        label: { show: true, position: 'top', distance: 8, formatter: (p: { value: number }) => rubles(p.value), color: t.ink, fontWeight: 600, fontSize: t.fontSize },
      },
    }],
  };
}

/** Pie without the ECharts legend: the legend is HTML next to the chart, so it never overlaps. */
export function regionsOption(t: ChartTheme): EChartsCoreOption {
  return {
    textStyle: { fontFamily: t.font, color: t.muted, fontSize: t.fontSize },
    color: t.palette,
    tooltip: { ...tooltip(t), trigger: 'item', formatter: (p: { name: string; value: number }) => `${p.name}: <b>${p.value}%</b>` },
    series: [{
      type: 'pie', radius: ['52%', '90%'], center: ['50%', '50%'], label: { show: false },
      itemStyle: { borderColor: t.surface, borderWidth: 2 },
      data: REGIONS.map(([name, value]) => ({ name, value })),
    }],
  };
}

export function categoriesOption(t: ChartTheme): EChartsCoreOption {
  return {
    textStyle: { fontFamily: t.font, color: t.muted, fontSize: t.fontSize },
    grid: { left: 8, right: 80, top: 8, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' },
    tooltip: { ...tooltip(t), trigger: 'axis', axisPointer: { type: 'none' }, formatter: (points: AxisPoint[]) => `${points[0]?.name ?? ''}<br/><b>${rubles(points[0]?.value ?? 0)}</b>` },
    xAxis: { type: 'value', show: false },
    yAxis: { type: 'category', inverse: true, data: CATEGORIES.map(([name]) => name), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: t.ink } },
    series: [{
      type: 'bar', barWidth: 14,
      data: CATEGORIES.map(([, value], index) => ({ value, itemStyle: { color: t.primary, opacity: index === 0 ? 1 : 0.45, borderRadius: [0, 4, 4, 0] } })),
      label: { show: true, position: 'right', color: t.ink, fontSize: t.fontSize, formatter: (p: { value: number }) => rubles(p.value) },
    }],
  };
}
