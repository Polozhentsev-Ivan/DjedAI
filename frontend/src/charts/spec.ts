// ChartSpec (§6.2) → ECharts options; table and KPI use React in SpecChart.
import { categoryLabel, column, displayData, numeric, values, type Spec, type Data, type Cell } from './data';
import type { EChartsCoreOption } from './echarts';
import { axisLabel, escapeHtml, maxAbs, measureLabel, timeLabels, tooltipBox } from './format';
import { specialOption } from './special';
import type { ChartTheme } from './theme';

export { measureLabel } from './format';
export const SUPPORTED_TYPES = ['line', 'area', 'bar', 'pie', 'scatter', 'histogram', 'heatmap', 'box', 'table', 'kpi'] as const;
export type SupportedType = (typeof SUPPORTED_TYPES)[number];
export function isSupported(type: Spec['type']): type is SupportedType {
  return (SUPPORTED_TYPES as readonly string[]).includes(type);
}

export interface LegendItem {
  label: string;
  value: string;
  color: string;
}

/** Pie legend shown as HTML next to the chart, so it can never overlap the ring. */
export function pieLegend(spec: Spec, input: Data): LegendItem[] {
  const data = displayData(spec, input);
  const labelIndex = column(data, spec.x?.field);
  const valueIndex = column(data, spec.y?.[0]?.field);
  const raw = values(data, valueIndex);
  return values(data, labelIndex).map((label, i) => ({
    label: categoryLabel(label),
    value: measureLabel(numeric(raw[i]) ? raw[i] : null, spec.y?.[0], maxAbs(data, [valueIndex])),
    color: `var(--chart-${(i % 6) + 1})`,
  }));
}

export function specOption(spec: Spec, input: Data, t: ChartTheme): EChartsCoreOption {
  const data = displayData(spec, input);
  const special = specialOption(spec, data, t);
  if (special) return special;
  const text = { fontFamily: t.font, color: t.muted, fontSize: t.fontSize };
  const measures = spec.y ?? [];
  const yIndexes = measures.map((m) => column(data, m.field));
  const xIndex = column(data, spec.x?.field);
  const scale = maxAbs(data, yIndexes);
  const first = measures[0];

  if (spec.type === 'pie') {
    const labels = values(data, xIndex).map(categoryLabel);
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

  const rowsX = values(data, xIndex);
  const groups = spec.color ? values(data, column(data, spec.color.field)) : null;
  const rawX = groups ? [...new Set(rowsX)] : rowsX;
  const numericX = spec.x?.kind === 'number';
  const seriesData = measures.flatMap((measure) => (groups ? [...new Set(groups)] : [null]).map((group) => {
    const raw = values(data, column(data, measure.field));
    return {
      measure,
      name: [...(groups ? [categoryLabel(group)] : []), measure.label ?? measure.field].join(' · '),
      data: rawX.map((value, index) => {
        const rowIndex = groups ? rowsX.findIndex((v, i) => v === value && groups[i] === group) : index;
        const amount = raw[rowIndex] ?? null;
        return numericX ? [value, amount] : amount;
      }),
    };
  }));
  const time = spec.x?.kind === 'time' ? timeLabels(rawX) : null;
  const categories = time?.axis ?? rawX.map(String);
  const tooltipNames = time?.tooltip ?? categories;
  const marks = (points: (Cell | Cell[])[], horizontal = false) => {
    const annotations = (spec.annotations ?? []).flatMap((a) => {
      const i = rawX.findIndex((v) => String(v) === a.x);
      const v = points[i];
      const amount = Array.isArray(v) ? v[1] : v;
      return i >= 0 && numeric(amount) ? [{ name: a.text, coord: horizontal ? [amount, i] : [numericX ? Number(a.x) : i, amount], value: a.text }] : [];
    });
    return annotations.length > 0 ? { symbol: 'circle', symbolSize: 8, data: annotations, label: { color: t.ink, fontSize: t.fontSize, position: horizontal ? 'right' : 'top', formatter: '{b}' } } : undefined;
  };
  const tooltip = {
    ...tooltipBox(t),
    trigger: 'axis',
    formatter: (points: { dataIndex: number; value: number | null | [number, number | null]; seriesIndex: number }[]) => {
      const head = tooltipNames[points[0]?.dataIndex ?? 0] ?? '';
      return [escapeHtml(head), ...points.map((p) => {
        const series = seriesData[p.seriesIndex];
        const value = Array.isArray(p.value) ? p.value[1] : p.value;
        return `${seriesData.length > 1 ? `${escapeHtml(series?.name ?? '')}: ` : ''}<b>${escapeHtml(measureLabel(value, series?.measure, scale))}</b>`;
      })].join('<br/>');
    },
  };

  if (spec.type === 'bar' && spec.orientation === 'horizontal') {
    return {
      textStyle: text,
      grid: { left: 8, right: 80, top: seriesData.length > 1 ? 38 : 8, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' },
      tooltip: { ...tooltip, axisPointer: { type: 'none' } },
      xAxis: { type: 'value', show: false },
      yAxis: { type: 'category', inverse: true, data: categories, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: t.ink } },
      color: t.palette,
      legend: seriesData.length > 1 ? { type: 'scroll', top: 0, textStyle: { color: t.ink } } : undefined,
      series: seriesData.map((series) => ({
        name: series.name, type: 'bar', barMaxWidth: 14, stack: spec.stacked ? 'total' : undefined, markPoint: marks(series.data, true),
        // The leader stays solid, the rest are muted: the mockup's top-N look.
        data: series.data.map((value, i) => ({ value: Array.isArray(value) ? value[1] : value, itemStyle: { color: seriesData.length === 1 ? t.primary : undefined, opacity: seriesData.length === 1 && i > 0 ? 0.45 : 1, borderRadius: [0, 4, 4, 0] } })),
        label: { show: seriesData.length === 1, position: 'right', color: t.ink, fontSize: t.fontSize, formatter: (p: { value: number | null }) => measureLabel(p.value, first, scale) },
      })),
    };
  }

  const single = seriesData.length === 1;
  return {
    textStyle: text,
    color: t.palette,
    legend: single ? undefined : { type: 'scroll', top: 0, textStyle: { color: t.ink } },
    grid: { left: 8, right: 20, top: 34, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' },
    tooltip,
    xAxis: { type: numericX ? 'value' : 'category', data: numericX ? undefined : categories, name: spec.x?.label, boundaryGap: spec.type === 'bar', axisLine: { lineStyle: { color: t.line } }, axisTick: { show: false }, axisLabel: { color: t.muted } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: t.line, type: 'dashed' } }, axisLabel: { color: t.muted, formatter: (v: number) => axisLabel(v, first, scale) } },
    series: seriesData.map(({ measure, name, data: points }) => {
      const markPoint = marks(points);
      const series = { name, data: points, stack: spec.stacked ? 'total' : undefined };
      if (spec.type === 'bar') return { ...series, markPoint, type: 'bar', barMaxWidth: 28, itemStyle: { borderRadius: [4, 4, 0, 0] } };
      return {
        ...series, type: 'line', symbol: 'circle', symbolSize: 6, lineStyle: { width: 2 },
        areaStyle: single || spec.type === 'area' ? { opacity: spec.type === 'area' ? 0.2 : 0.08 } : undefined,
        markPoint: markPoint ?? (single ? {
          symbol: 'circle', symbolSize: 10, data: [{ type: 'max' }],
          label: { show: true, position: 'top', distance: 8, formatter: (p: { value: number }) => measureLabel(p.value, measure, scale), color: t.ink, fontWeight: 600, fontSize: t.fontSize },
        } : undefined),
      };
    }),
  };
}
