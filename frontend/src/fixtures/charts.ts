// Hand-authored rendering examples for #87, independent of agent chart selection (#75).
import type { Schemas } from '../api/client';

export interface ChartExample { spec: Schemas['ChartSpec']; data: Schemas['ResultData'] }

const monthly: Schemas['ResultData'] = {
  columns: ['month', 'amount'], rows: [['2026-01-01', 1800000], ['2026-02-01', 2400000], ['2026-03-01', 2100000], ['2026-04-01', 3400000]], row_count: 4, truncated: false,
};
const money = { field: 'amount', label: 'Выручка, ₽', format: 'money' } as const;
const titles = { line: 'Выручка по месяцам', bar: 'Выручка по регионам', area: 'Динамика выручки', pie: 'Доля регионов' } as const;

export const chartExamples: ChartExample[] = [
  ...(['line', 'bar', 'area', 'pie'] as const).map((type) => ({
    spec: {
      chart_id: `chart_${type}`, result_id: `r_${type}`, type, title: titles[type], subtitle: 'Ручная спецификация · сохранённый результат',
      x: { field: type === 'bar' || type === 'pie' ? 'region' : 'month', kind: type === 'bar' || type === 'pie' ? 'category' as const : 'time' as const },
      y: [money],
    },
    data: type === 'bar' || type === 'pie' ? { columns: ['region', 'amount'], rows: [['Москва', 2400000], ['Казань', 1800000], ['Томск', 900000]], row_count: 3, truncated: false } : monthly,
  })),
  {
    spec: { chart_id: 'chart_scatter', result_id: 'r_scatter', type: 'scatter', title: 'Заказы и средний чек', subtitle: 'Одна точка — один магазин', x: { field: 'orders', label: 'Число заказов', kind: 'number' }, y: [{ field: 'average', label: 'Средний чек, ₽', format: 'money' }], color: { field: 'region', label: 'Регион' } },
    data: { columns: ['orders', 'average', 'region'], rows: [[12, 820, 'Москва'], [25, 950, 'Москва'], [18, 760, 'Казань'], [32, 1100, 'Казань'], [40, 1250, 'Москва']], row_count: 5, truncated: false },
  },
  {
    spec: { chart_id: 'chart_histogram', result_id: 'r_histogram', type: 'histogram', title: 'Распределение сумм заказа', subtitle: 'Интервалы и частоты рассчитаны сервером', x: { field: 'interval', label: 'Сумма заказа, ₽' }, y: [{ field: 'count', label: 'Число заказов', format: 'int' }] },
    data: { columns: ['interval', 'count'], rows: [['0–500', 3], ['500–1000', 12], ['1000–1500', 8], ['1500–2000', 4], ['2000–2500', 1]], row_count: 5, truncated: false },
  },
  {
    spec: { chart_id: 'chart_heatmap', result_id: 'r_heatmap', type: 'heatmap', title: 'Продажи по регионам и кварталам', subtitle: 'Одна ячейка — готовый агрегат', x: { field: 'quarter', label: 'Квартал' }, color: { field: 'region', label: 'Регион' }, y: [{ field: 'amount', label: 'Выручка, ₽', format: 'money' }] },
    data: { columns: ['quarter', 'region', 'amount'], rows: [['I', 'Москва', 120000], ['II', 'Москва', 180000], ['III', 'Москва', 240000], ['I', 'Казань', 80000], ['II', 'Казань', 150000], ['III', 'Казань', 210000]], row_count: 6, truncated: false },
  },
  {
    spec: { chart_id: 'chart_box', result_id: 'r_box', type: 'box', title: 'Разброс сумм заказов', subtitle: 'Усы, квартили и медиана рассчитаны сервером', x: { field: 'region', label: 'Регион' }, y: ['low', 'q1', 'median', 'q3', 'high'].map((field) => ({ field, format: 'money' as const })) },
    data: { columns: ['region', 'low', 'q1', 'median', 'q3', 'high'], rows: [['Москва', 100, 400, 800, 1200, 1800], ['Казань', 80, 350, 600, 950, 1500], ['Томск', 50, 250, 450, 700, 1200]], row_count: 3, truncated: false },
  },
  {
    spec: { chart_id: 'chart_table', result_id: 'r_table', type: 'table', title: 'Выручка и доля по регионам', subtitle: 'Исходные значения сохранённого результата', x: { field: 'region', label: 'Регион' }, y: [money, { field: 'share', label: 'Доля', format: 'percent' }] },
    data: { columns: ['region', 'amount', 'share'], rows: [['Москва', 2400000, 60], ['Казань', 1600000, 40]], row_count: 2, truncated: false },
  },
  {
    spec: { chart_id: 'chart_kpi', result_id: 'r_kpi', type: 'kpi', title: 'Общая выручка', subtitle: 'Агрегат из одной строки результата', y: [money] },
    data: { columns: ['amount'], rows: [[4000000]], row_count: 1, truncated: false },
  },
];
