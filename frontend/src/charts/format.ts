import { formatCount, formatDate, formatMillions, formatMoney, formatPercent } from '@/lib/format';
import type { Cell, Data, Measure } from './data';
import { numeric, values } from './data';
import type { ChartTheme } from './theme';

const number = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 20 });
const MONTHS = ['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн', 'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек'];

/** Missing values remain missing; percent values already use the 0–100 scale. */
export function measureLabel(value: number | null, measure: Measure | undefined, scale: number): string {
  if (value === null || !Number.isFinite(value)) return '—';
  if (measure?.format === 'percent') return formatPercent(value);
  if (measure?.format === 'money') return scale >= 1e6 ? formatMillions(value, '₽') : formatMoney(value);
  return measure?.format === 'int' ? formatCount(value) : number.format(value).replace('-', '−');
}

export function axisLabel(value: number, measure: Measure | undefined, scale: number): string {
  if (measure?.format === 'money' && scale >= 1e6) return formatMillions(value);
  return measureLabel(value, measure, scale);
}

export function timeLabels(raw: Cell[]): { axis: string[]; tooltip: string[] } {
  const dates = raw.map((v) => new Date(String(v)));
  const monthly = dates.every((d) => !Number.isNaN(d.getTime()) && d.getDate() === 1);
  if (monthly) return { axis: dates.map((d) => MONTHS[d.getMonth()] ?? ''), tooltip: dates.map((d) => `${MONTHS[d.getMonth()] ?? ''} ${d.getFullYear()}`) };
  const text = dates.map((d, i) => Number.isNaN(d.getTime()) ? String(raw[i]) : formatDate(d));
  return { axis: text, tooltip: text };
}

export function tooltipBox(t: ChartTheme) {
  return { backgroundColor: t.surface, borderColor: t.line, textStyle: { color: t.ink, fontSize: t.fontSize }, extraCssText: `box-shadow:none;border-radius:${t.radius}` };
}

/** ECharts tooltip strings are HTML; every data label must be escaped at that sink. */
export function escapeHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function maxAbs(data: Data, indexes: number[]): number {
  return indexes.reduce((max, index) => values(data, index).reduce<number>((result, value) => numeric(value) ? Math.max(result, Math.abs(value)) : result, max), 0);
}
