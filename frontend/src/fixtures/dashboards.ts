import type { DemoChartName } from '@/charts/DemoChart';
import type { Calculation } from '@/components/HowCalculated';
import type { Kpi } from '@/components/WidgetCard';

export interface DashboardSummary {
  id: string;
  title: string;
}

export interface Widget {
  id: string;
  title: string;
  subtitle: string;
  how: Calculation;
  chart?: DemoChartName;
  kpi?: Kpi;
}

export const DASHBOARDS: readonly DashboardSummary[] = [
  { id: 'q', title: 'Квартальная статистика продаж' },
  { id: 'm', title: 'Маркетинг Q3' },
  { id: 'r', title: 'Регионы' },
];

export const DASHBOARD_META = 'Сформирован по результатам диалога в чате · Последнее обновление: 18.10.2026 14:00';

const amount = { label: 'Сумма с НДС, руб', name: 'amount' };

export const WIDGETS: readonly Widget[] = [
  {
    id: 'rev', title: 'Выручка по месяцам', subtitle: 'Пик продаж — июль', chart: 'revenue',
    how: {
      tool: "time_aggregate(table='orders', freq='month')",
      columns: [amount, { label: 'Дата оплаты', name: 'payment_date' }],
      sql: `SELECT date_trunc('month', payment_date) AS month,
       SUM(amount) AS revenue
FROM   orders
GROUP  BY month
ORDER  BY month;`,
    },
  },
  {
    id: 'reg', title: 'Доля по регионам', subtitle: 'Москва 45%, СПб 25%', chart: 'regions',
    how: {
      tool: "group_share(table='orders', by='customers.region')",
      columns: [amount, { label: 'Регион', name: 'region' }],
      sql: `SELECT c.region, SUM(o.amount) AS revenue
FROM   orders o
JOIN   customers c ON c.id = o.customer_id
GROUP  BY c.region
ORDER  BY revenue DESC;`,
    },
  },
  {
    id: 'cat', title: 'Топ категорий', subtitle: 'Электроника лидирует', chart: 'categories',
    how: {
      tool: "top_n(table='orders', by='category', n=5)",
      columns: [amount, { label: 'Категория', name: 'category' }],
      sql: `SELECT category, SUM(amount) AS revenue
FROM   orders
GROUP  BY category
ORDER  BY revenue DESC
LIMIT  5;`,
    },
  },
  {
    id: 'kpi', title: 'KPI квартала', subtitle: 'Выручка относительно цели',
    kpi: {
      caption: 'Сумма заказов с НДС за квартал', value: '12,4 млн ₽', delta: '+18% к цели', deltaTone: 'success', target: 'Цель квартала: 10 500 000 ₽',
      stats: [{ label: 'Заказов', value: '2 573', delta: '+21%', tone: 'success' }, { label: 'Средний чек', value: '4 820 ₽', delta: '−5%', tone: 'danger' }],
    },
    how: {
      tool: "kpi(table='orders', period='quarter', target=10500000)",
      columns: [amount, { label: 'Дата оплаты', name: 'payment_date' }],
      sql: `SELECT SUM(amount) AS revenue,
       SUM(amount) / 10500000 - 1 AS vs_target
FROM   orders
WHERE  payment_date >= date_trunc('quarter', now());`,
    },
  },
];

/** Widget that fails in the error fixture: one widget down, the rest render normally. */
export const FAILING_WIDGET = 'cat';
