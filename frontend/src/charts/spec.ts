// ChartSpec (specification §6.2) + result rows → ECharts option. Covers line, area, bar and pie;
// the remaining types are added by the full renderer task (B4-5).
import type { Schemas } from '@/api/client';
import { formatCount, formatDate, formatMillions, formatMoney, formatPercent } from '@/lib/format';
import type { EChartsCoreOption } from './echarts';
import type { ChartTheme } from './theme';

type Spec = Schemas['ChartTemplate'];
type Data = Schemas['ResultData'];
type Measure = Schemas['ChartMeasure'];
type Cell = string | number | boolean | null;

export const SUPPORTED_TYPES = ['line', 'area', 'bar', 'pie'] as const;
export type SupportedType = (typeof SUPPORTED_TYPES)[number];

export function isSupported(type: Spec['type']): type is SupportedType {
  return (SUPPORTED_TYPES as readonly string[]).includes(type);
}

const MONTHS = ['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн', 'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек'];

function column(data: Data, field: string | undefined): number {
  return field === undefined ? -1 : (data.columns ?? []).findIndex((c) => (typeof c === 'string' ? c : c.name) === field);
}

function values(data: Data, index: number): Cell[] {
  return (data.rows ?? []).map((row) => {
    const value: unknown = row[index];
    if (value === undefined || value === null) return null;
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
    return JSON.stringify(value);
  });
}

/** Time axis labels: month names when every point is the first of a month, dates otherwise. */
function timeLabels(raw: Cell[]): { axis: string[]; tooltip: string[] } {
  const dates = raw.map((v) => new Date(String(v)));
  const monthly = dates.every((d) => !Number.isNaN(d.getTime()) && d.getDate() === 1);
  if (monthly) {
    return { axis: dates.map((d) => MONTHS[d.getMonth()] ?? ''), tooltip: dates.map((d) => `${MONTHS[d.getMonth()] ?? ''} ${d.getFullYear()}`) };
  }
  const text = dates.map((d, i) => (Number.isNaN(d.getTime()) ? String(raw[i]) : formatDate(d)));
  return { axis: text, tooltip: text };
}

/** Value label: money in millions once the scale reaches a million, as in the mockup. */
export function measureLabel(value: number, measure: Measure | undefined, scale: number): string {
  if (measure?.format === 'percent') return formatPercent(value);
  if (measure?.format === 'money') return scale >= 1e6 ? formatMillions(value, '₽') : formatMoney(value);
  return formatCount(value);
}

function axisLabel(value: number, measure: Measure | undefined, scale: number): string {
  if (measure?.format === 'percent') return formatPercent(value);
  if (measure?.format === 'money' && scale >= 1e6) return formatMillions(value);
  return formatCount(value);
}

function tooltipBox(t: ChartTheme) {
  return {
    backgroundColor: t.surface,
    borderColor: t.line,
    textStyle: { color: t.ink, fontSize: t.fontSize },
    extraCssText: `box-shadow:none;border-radius:${t.radius}`,
  };
}

/** ECharts inserts formatter strings as HTML, while category names come from data. */
function escapeHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function maxAbs(data: Data, indexes: number[]): number {
  return Math.max(0, ...indexes.flatMap((i) => values(data, i)).map((v) => (typeof v === 'number' ? Math.abs(v) : 0)));
}

export interface LegendItem {
  label: string;
  value: string;
  color: string;
}

/** Pie legend shown as HTML next to the chart, so it can never overlap the ring. */
export function pieLegend(spec: Spec, data: Data): LegendItem[] {
  const labelIndex = column(data, spec.x?.field);
  const valueIndex = column(data, spec.y?.[0]?.field);
  const raw = values(data, valueIndex).map((v) => (typeof v === 'number' ? v : 0));
  const total = raw.reduce((a, b) => a + b, 0) || 1;
  const percent = spec.y?.[0]?.format === 'percent';
  return values(data, labelIndex).map((label, i) => ({
    label: String(label ?? ''),
    value: formatPercent(percent ? (raw[i] ?? 0) : Math.round(((raw[i] ?? 0) / total) * 1000) / 10),
    color: `var(--chart-${(i % 6) + 1})`,
  }));
}

export function specOption(spec: Spec, data: Data, t: ChartTheme): EChartsCoreOption {
  const text = { fontFamily: t.font, color: t.muted, fontSize: t.fontSize };
  const measures = spec.y ?? [];
  const yIndexes = measures.map((m) => column(data, m.field));
  const xIndex = column(data, spec.x?.field);
  const scale = maxAbs(data, yIndexes);
  const first = measures[0];

  if (spec.type === 'pie') {
    const labels = values(data, xIndex).map(String);
    return {
      textStyle: text,
      color: t.palette,
      tooltip: { ...tooltipBox(t), trigger: 'item', formatter: (p: { name: string; value: number }) => `${escapeHtml(p.name)}: <b>${escapeHtml(measureLabel(p.value, first, scale))}</b>` },
      series: [{
        type: 'pie', radius: ['52%', '90%'], center: ['50%', '50%'], label: { show: false },
        itemStyle: { borderColor: t.surface, borderWidth: 2 },
        data: values(data, yIndexes[0] ?? -1).map((value, i) => ({ name: labels[i] ?? '', value })),
      }],
    };
  }

  const rawX = values(data, xIndex);
  const time = spec.x?.kind === 'time' ? timeLabels(rawX) : null;
  const categories = time?.axis ?? rawX.map(String);
  const tooltipNames = time?.tooltip ?? categories;
  const tooltip = {
    ...tooltipBox(t),
    trigger: 'axis',
    formatter: (points: { dataIndex: number; value: number; seriesIndex: number }[]) => {
      const head = tooltipNames[points[0]?.dataIndex ?? 0] ?? '';
      return [escapeHtml(head), ...points.map((p) => `<b>${escapeHtml(measureLabel(p.value, measures[p.seriesIndex], scale))}</b>`)].join('<br/>');
    },
  };

  if (spec.type === 'bar' && spec.orientation === 'horizontal') {
    return {
      textStyle: text,
      grid: { left: 8, right: 80, top: 8, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' },
      tooltip: { ...tooltip, axisPointer: { type: 'none' } },
      xAxis: { type: 'value', show: false },
      yAxis: { type: 'category', inverse: true, data: categories, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: t.ink } },
      series: [{
        type: 'bar', barWidth: 14,
        // The leader stays solid, the rest are muted: the mockup's top-N look.
        data: values(data, yIndexes[0] ?? -1).map((value, i) => ({ value, itemStyle: { color: t.primary, opacity: i === 0 ? 1 : 0.45, borderRadius: [0, 4, 4, 0] } })),
        label: { show: true, position: 'right', color: t.ink, fontSize: t.fontSize, formatter: (p: { value: number }) => measureLabel(p.value, first, scale) },
      }],
    };
  }

  const single = measures.length === 1;
  return {
    textStyle: text,
    color: t.palette,
    grid: { left: 8, right: 20, top: 34, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' },
    tooltip,
    xAxis: { type: 'category', data: categories, boundaryGap: spec.type === 'bar', axisLine: { lineStyle: { color: t.line } }, axisTick: { show: false }, axisLabel: { color: t.muted } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: t.line, type: 'dashed' } }, axisLabel: { color: t.muted, formatter: (v: number) => axisLabel(v, first, scale) } },
    series: measures.map((measure, i) => {
      const series = { name: measure.label ?? measure.field, data: values(data, yIndexes[i] ?? -1), stack: spec.stacked ? 'total' : undefined };
      if (spec.type === 'bar') return { ...series, type: 'bar', barMaxWidth: 28, itemStyle: { borderRadius: [4, 4, 0, 0] } };
      return {
        ...series, type: 'line', symbol: 'circle', symbolSize: 6, lineStyle: { width: 2 },
        areaStyle: single || spec.type === 'area' ? { opacity: spec.type === 'area' ? 0.2 : 0.08 } : undefined,
        markPoint: single ? {
          symbol: 'circle', symbolSize: 10, data: [{ type: 'max' }],
          label: { show: true, position: 'top', distance: 8, formatter: (p: { value: number }) => measureLabel(p.value, measure, scale), color: t.ink, fontWeight: 600, fontSize: t.fontSize },
        } : undefined,
      };
    }),
  };
}
