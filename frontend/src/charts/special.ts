import { categoryLabel, column, numeric, values, type Data, type Spec } from './data';
import type { EChartsCoreOption } from './echarts';
import { axisLabel, escapeHtml, maxAbs, measureLabel, tooltipBox } from './format';
import type { ChartTheme } from './theme';

/** These series consume server-prepared rows: there are no client statistical transforms. */
export function specialOption(spec: Spec, data: Data, t: ChartTheme): EChartsCoreOption | undefined {
  const measures = spec.y ?? [];
  const first = measures[0];
  const x = values(data, column(data, spec.x?.field));
  const y = values(data, column(data, first?.field));
  const scale = maxAbs(data, measures.map((m) => column(data, m.field)));
  const base = { textStyle: { fontFamily: t.font, color: t.muted, fontSize: t.fontSize }, color: t.palette };
  const grid = { left: 8, right: 20, top: 40, bottom: 48, outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' };
  const category = { type: 'category', nameTextStyle: { color: t.muted, align: 'left' }, axisLine: { lineStyle: { color: t.line } }, axisTick: { show: false }, axisLabel: { color: t.muted, hideOverlap: true } };
  const value = { type: 'value', nameTextStyle: { color: t.muted, align: 'left' }, axisLine: { show: false }, splitLine: { lineStyle: { color: t.line, type: 'dashed' } }, axisLabel: { color: t.muted, formatter: (v: number) => axisLabel(v, first, scale) } };
  const tooltip = { ...tooltipBox(t), trigger: 'item' };

  if (spec.type === 'scatter') {
    const groups = spec.color ? values(data, column(data, spec.color.field)).map(categoryLabel) : x.map(() => '');
    const names = [...new Set(groups)];
    return {
      ...base, grid,
      legend: spec.color || measures.length > 1 ? { type: 'scroll', textStyle: { color: t.ink }, top: 0 } : undefined,
      xAxis: { ...value, name: spec.x?.label ?? spec.x?.field, nameLocation: 'middle', nameGap: 26, nameTextStyle: { color: t.muted, align: 'center' }, axisLabel: { color: t.muted, formatter: (v: number) => measureLabel(v, undefined, 0) } },
      yAxis: { ...value, name: first?.label ?? first?.field },
      tooltip: { ...tooltip, formatter: (p: { value: number[]; seriesName: string; seriesIndex: number }) => `${escapeHtml(p.seriesName)}<br/>${escapeHtml(spec.x?.label ?? spec.x?.field ?? '')}: <b>${escapeHtml(measureLabel(p.value[0] ?? null, undefined, 0))}</b><br/>${escapeHtml(measureLabel(p.value[1] ?? null, measures[Math.floor(p.seriesIndex / names.length)], scale))}` },
      series: measures.flatMap((measure) => names.map((name) => {
        const ys = values(data, column(data, measure.field));
        return {
          name: [name, measure.label ?? measure.field].filter(Boolean).join(' · '), type: 'scatter', symbolSize: 9,
          itemStyle: { opacity: 0.8 }, emphasis: { focus: 'series' },
          data: x.flatMap((v, i) => numeric(v) && numeric(ys[i]) && groups[i] === name ? [[v, ys[i]]] : []),
        };
      })),
    };
  }
  if (spec.type === 'heatmap') {
    const other = values(data, column(data, spec.color?.field));
    const xs = [...new Set(x)];
    const ys = [...new Set(other)];
    const points = y.flatMap((v, i) => numeric(v) ? [[xs.indexOf(x[i] ?? null), ys.indexOf(other[i] ?? null), v]] : []);
    const min = points.reduce((n, p) => Math.min(n, p[2] ?? n), Infinity);
    const max = points.reduce((n, p) => Math.max(n, p[2] ?? n), -Infinity);
    return {
      ...base, grid: { ...grid, bottom: 90 },
      xAxis: { ...category, data: xs.map(categoryLabel), name: spec.x?.label, nameLocation: 'middle', nameGap: 26, nameTextStyle: { color: t.muted, align: 'center' } },
      yAxis: { ...category, data: ys.map(categoryLabel), name: spec.color?.label, splitArea: { show: true, areaStyle: { color: [t.surface] } } },
      visualMap: { min: Number.isFinite(min) ? min : 0, max: Number.isFinite(max) ? max : 0, dimension: 2, orient: 'horizontal', left: 'center', bottom: 6, calculable: false, inRange: { color: [t.line, t.primary] }, text: [measureLabel(Number.isFinite(max) ? max : null, first, scale), measureLabel(Number.isFinite(min) ? min : null, first, scale)], textStyle: { color: t.muted }, formatter: (v: number) => measureLabel(v, first, scale) },
      tooltip: { ...tooltip, formatter: (p: { value: number[] }) => `${escapeHtml(categoryLabel(xs[p.value[0] ?? -1] ?? null))} · ${escapeHtml(categoryLabel(ys[p.value[1] ?? -1] ?? null))}<br/><b>${escapeHtml(measureLabel(p.value[2] ?? null, first, scale))}</b>` },
      series: [{ type: 'heatmap', data: points, itemStyle: { borderColor: t.surface, borderWidth: 2 }, emphasis: { itemStyle: { borderColor: t.primary } } }],
    };
  }
  if (spec.type === 'box') {
    const horizontal = spec.orientation === 'horizontal';
    const categories = { ...category, data: x.map(categoryLabel) };
    return {
      ...base, grid,
      xAxis: horizontal ? value : categories, yAxis: horizontal ? categories : value,
      tooltip: { ...tooltip, formatter: (p: { dataIndex: number }) => {
        const row = data.rows?.[p.dataIndex] ?? [];
        return [escapeHtml(categoryLabel(x[p.dataIndex] ?? null)), ...measures.map((m, i) => {
          const v: unknown = row[column(data, m.field)];
          return `${['Нижний ус', 'Q1', 'Медиана', 'Q3', 'Верхний ус'][i] ?? ''}: <b>${escapeHtml(measureLabel(numeric(v) ? v : null, m, scale))}</b>`;
        })].join('<br/>');
      } },
      series: [{ type: 'boxplot', layout: horizontal ? 'horizontal' : 'vertical', itemStyle: { color: t.surface, borderColor: t.primary, borderWidth: 2 }, data: (data.rows ?? []).map((row) => measures.map((m) => row[column(data, m.field)])) }],
    };
  }
  if (spec.type === 'histogram') {
    return {
      ...base, grid,
      xAxis: { ...category, data: x.map(categoryLabel), name: spec.x?.label, nameLocation: 'middle', nameGap: 26, nameTextStyle: { color: t.muted, align: 'center' } },
      yAxis: { ...value, name: first?.label ?? 'Частота' },
      tooltip: { ...tooltip, formatter: (p: { name: string; value: number | null }) => `${escapeHtml(p.name)}<br/><b>${escapeHtml(measureLabel(p.value, first, scale))}</b>` },
      series: [{ type: 'bar', name: first?.label ?? first?.field, barCategoryGap: '0%', itemStyle: { borderColor: t.surface, borderWidth: 1, color: t.primary }, data: y }],
    };
  }
  return undefined;
}
