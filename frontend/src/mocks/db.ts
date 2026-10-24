// Mock backend data in the API shape of schema/openapi.yaml; the demo values come from docs/ui-design.
import type { Schemas } from '@/api/client';

type DatasetDetail = Schemas['DatasetDetail'];
type ToolResultMeta = Schemas['ToolResultMeta'];
type ResultData = Schemas['ResultData'];
type Dashboard = Schemas['Dashboard'];

const MONTHS = ['2026-01-01', '2026-02-01', '2026-03-01', '2026-04-01', '2026-05-01', '2026-06-01', '2026-07-01', '2026-08-01', '2026-09-01', '2026-10-01'];
const REVENUE = [1800000, 2000000, 2200000, 2300000, 2500000, 2800000, 3400000, 2900000, 2700000, 2800000];

const ordersCatalog: Schemas['DatasetCatalog'] = {
  dataset_id: 'ds_orders',
  name: 'orders_2026',
  created_at: '2026-10-18T13:52:00',
  source: { kind: 'csv_upload' },
  summary: 'Заказы и клиенты интернет-магазина за 2026 год',
  tables: [
    {
      table_id: 'orders', raw_table: 'raw_orders', original_name: 'orders.csv', row_count: 120000,
      description: 'Заказы интернет-магазина: сумма, скидка, статус и клиент',
      primary_key_candidates: ['order_id'], time_columns: ['payment_date'], duplicate_rows: 0,
      columns: [
        { name: 'order_id', original_name: 'Номер заказа', physical_type: 'VARCHAR', semantic_type: 'id', role: 'id', null_ratio: 0, distinct_count: 120000, is_unique: true, description: 'Уникальный номер заказа' },
        { name: 'payment_date', original_name: 'Дата оплаты', physical_type: 'DATE', semantic_type: 'date', role: 'time', null_ratio: 0.001, distinct_count: 304, is_unique: false, parse: { format: '%d.%m.%Y', success_ratio: 0.9999 } },
        { name: 'amount', original_name: 'Сумма с НДС, руб', physical_type: 'DECIMAL(18,2)', semantic_type: 'money', role: 'measure', null_ratio: 0, distinct_count: 9811, is_unique: false, currency: 'RUB', description: 'Сумма заказа с НДС в рублях' },
        { name: 'discount', original_name: 'Скидка', physical_type: 'DOUBLE', semantic_type: 'percent', role: 'measure', null_ratio: 0.052, distinct_count: 21, is_unique: false },
        { name: 'status', original_name: 'Статус', physical_type: 'VARCHAR', semantic_type: 'category', role: 'dimension', null_ratio: 0, distinct_count: 4, is_unique: false },
        { name: 'customer_id', original_name: 'Клиент', physical_type: 'VARCHAR', semantic_type: 'id', role: 'id', null_ratio: 0, distinct_count: 450, is_unique: false, description: 'Клиент, оформивший заказ' },
      ],
    },
    {
      table_id: 'customers', raw_table: 'raw_customers', original_name: 'customers.csv', row_count: 450,
      description: 'Справочник клиентов с регионом и датой регистрации',
      primary_key_candidates: ['id'], time_columns: ['registered_at'], duplicate_rows: 0,
      columns: [
        { name: 'id', original_name: 'ID клиента', physical_type: 'VARCHAR', semantic_type: 'id', role: 'id', null_ratio: 0, distinct_count: 450, is_unique: true },
        { name: 'name', original_name: 'Название клиента', physical_type: 'VARCHAR', semantic_type: 'text', role: 'text', null_ratio: 0, distinct_count: 447, is_unique: false },
        { name: 'region', original_name: 'Регион', physical_type: 'VARCHAR', semantic_type: 'category', role: 'dimension', null_ratio: 0.004, distinct_count: 8, is_unique: false },
        { name: 'registered_at', original_name: 'Дата регистрации', physical_type: 'DATE', semantic_type: 'date', role: 'time', null_ratio: 0, distinct_count: 431, is_unique: false, parse: { format: '%d.%m.%Y', success_ratio: 1 } },
      ],
    },
  ],
  relations: [
    { from: { table: 'orders', column: 'customer_id' }, to: { table: 'customers', column: 'id' }, overlap: 0.998, jaccard: 0.998, cardinality: 'N:1', confidence: 'high' },
  ],
  warnings: ['В колонке «Дата оплаты» 12 строк содержали нераспознанный текст; заменены на NULL. Исходные значения сохранены в raw'],
  transformations: [
    { table: 'orders', column: 'amount', action: 'parse_money', detail: { currency: '₽', decimal: ',' } },
    { table: 'orders', column: 'payment_date', action: 'parse_date', detail: { format: '%d.%m.%Y' } },
    { table: 'orders', column: 'status', action: 'detect_category', detail: { count: 4, values: ['оплачен', 'доставлен', 'отменён', 'возврат'] } },
  ],
};

function readyDataset(catalog: Schemas['DatasetCatalog'], rows: number): DatasetDetail {
  return {
    dataset_id: catalog.dataset_id, name: catalog.name, source: { ...catalog.source, files: catalog.tables?.length ?? 0 }, status: 'ready',
    table_count: catalog.tables?.length ?? 0, row_count: rows, created_at: catalog.created_at, catalog, error: null,
  };
}

const resultData = {
  r_rev: { columns: [{ name: 'month', type: 'date', label: 'Дата оплаты' }, { name: 'revenue', type: 'money', label: 'Сумма с НДС, руб' }], rows: MONTHS.map((month, i) => [month, REVENUE[i] ?? 0]), row_count: 10, truncated: false },
  r_reg: {
    columns: [{ name: 'region', type: 'category', label: 'Регион' }, { name: 'share', type: 'percent', label: 'Сумма с НДС, руб' }],
    rows: [['Москва', 45], ['Санкт-Петербург', 25], ['Казань', 12], ['Екатеринбург', 10], ['Другие', 8]],
    row_count: 5, truncated: false,
  },
  r_cat: {
    columns: [{ name: 'category', type: 'category', label: 'Категория' }, { name: 'revenue', type: 'money', label: 'Сумма с НДС, руб' }],
    rows: [['Электроника', 4100000], ['Бытовая техника', 2600000], ['Одежда', 1900000], ['Дом и сад', 1400000], ['Спорт', 900000]],
    row_count: 5, truncated: false,
  },
  r_kpi: { columns: [
    { name: 'revenue', type: 'money', label: 'Сумма с НДС, руб' },
    { name: 'target', type: 'money', label: 'Цель квартала' },
    { name: 'orders', type: 'int', label: 'Заказов' },
    { name: 'avg_check', type: 'money', label: 'Средний чек' },
  ], rows: [[12400000, 10500000, 2573, 4820]], row_count: 1, truncated: false },
} satisfies Record<string, ResultData>;

const revenueResult: ToolResultMeta = {
  data: resultData.r_rev,
  result_id: 'r_rev', tool: 'time_aggregate', params: { table: 'orders', freq: 'month' }, status: 'ok',
  sql: `SELECT date_trunc('month', payment_date) AS month,
       SUM(amount) AS revenue
FROM   orders
GROUP  BY month
ORDER  BY month;`,
  facts: [{ text: 'Пик продаж — июль', kind: 'peak' }], duration_ms: 800,
};

const regionsResult: ToolResultMeta = {
  data: resultData.r_reg,
  result_id: 'r_reg', tool: 'group_share', params: { table: 'orders', by: 'customers.region' }, status: 'ok',
  sql: `SELECT c.region, SUM(o.amount) AS revenue
FROM   orders o
JOIN   customers c ON c.id = o.customer_id
GROUP  BY c.region
ORDER  BY revenue DESC;`,
  facts: [],
};

const categoriesResult: ToolResultMeta = {
  data: resultData.r_cat,
  result_id: 'r_cat', tool: 'top_n', params: { table: 'orders', by: 'category', n: 5 }, status: 'ok',
  sql: `SELECT category, SUM(amount) AS revenue
FROM   orders
GROUP  BY category
ORDER  BY revenue DESC
LIMIT  5;`,
  facts: [],
};

const kpiResult: ToolResultMeta = {
  data: resultData.r_kpi,
  result_id: 'r_kpi', tool: 'kpi', params: { table: 'orders', period: 'quarter', target: 10500000 }, status: 'ok',
  sql: `SELECT SUM(amount) AS revenue,
       SUM(amount) / 10500000 - 1 AS vs_target
FROM   orders
WHERE  payment_date >= date_trunc('quarter', now());`,
  facts: [
    { text: '+18% к цели', kind: 'delta', values: { field: 'revenue', delta: 18, base: 'target' } },
    { text: 'Заказов на 21% больше', kind: 'delta', values: { field: 'orders', delta: 21 } },
    { text: 'Средний чек ниже на 5%', kind: 'delta', values: { field: 'avg_check', delta: -5 } },
  ],
};

const amount = { field: 'revenue', label: 'Сумма с НДС, руб', format: 'money' } as const;

function widget(id: string, result: ToolResultMeta, chart: Omit<Schemas['ChartSpec'], 'chart_id' | 'result_id'>, caption: string): Schemas['Widget'] {
  return { widget_id: id, tool_call: { tool: result.tool ?? 'unknown_tool', params: result.params ?? {} }, layout: { x: 0, y: 0, w: 6, h: 4 }, chart: { ...chart, chart_id: `chart_${id}`, result_id: result.result_id }, caption, result_id: result.result_id, status: 'ok', error: null };
}

function quarterly(id: string, title: string): Dashboard {
  return {
    dashboard_id: id, dataset_id: 'ds_orders', title, description: 'Сформирован по результатам диалога в чате', source_chat_id: 'c_q1',
    created_at: '2026-10-18T13:58:00', refreshed_at: '2026-10-18T14:00:00',
    widgets: [
      widget('w_rev', revenueResult, { type: 'line', title: 'Выручка по месяцам', x: { field: 'month', label: 'Месяц', kind: 'time' }, y: [amount] }, 'Пик продаж — июль'),
      widget('w_reg', regionsResult, { type: 'pie', title: 'Доля по регионам', x: { field: 'region', label: 'Регион', kind: 'category' }, y: [{ field: 'share', label: 'Доля', format: 'percent' }] }, 'Москва 45%, СПб 25%'),
      widget('w_cat', categoriesResult, { type: 'bar', title: 'Топ категорий', orientation: 'horizontal', x: { field: 'category', label: 'Категория', kind: 'category' }, y: [amount] }, 'Электроника лидирует'),
      widget('w_kpi', kpiResult, {
        type: 'kpi', title: 'KPI квартала',
        y: [{ field: 'revenue', label: 'Сумма заказов с НДС за квартал', format: 'money' }, { field: 'orders', label: 'Заказов', format: 'int' }, { field: 'avg_check', label: 'Средний чек', format: 'money' }],
      }, 'Выручка относительно цели'),
    ],
  };
}

/** Local time `daysAgo` days back at hh:mm, as an ISO string without zone. */
function daysAgoAt(daysAgo: number, hours: number, minutes: number): string {
  const date = new Date();
  date.setDate(date.getDate() - daysAgo);
  date.setHours(hours, minutes, 0, 0);
  return date.toISOString();
}

export interface MockDb {
  datasets: DatasetDetail[];
  results: Record<string, ToolResultMeta>;
  resultData: Record<string, ResultData>;
  dashboards: Dashboard[];
  chats: Schemas['ChatDetail'][];
}

/** Fresh copy of the demo data; handlers mutate it like a real backend would. */
export function createDb(): MockDb {
  const retail = structuredClone(ordersCatalog);
  retail.dataset_id = 'ds_retail';
  retail.name = 'retail_crm_prod';
  retail.source = { kind: 'postgres' };
  return structuredClone({
    datasets: [
      readyDataset(ordersCatalog, 120450),
      { ...readyDataset(retail, 1420000), table_count: 5 },
      {
        dataset_id: 'ds_marketing', name: 'marketing_q3', source: { kind: 'csv_upload', files: 1 }, status: 'error',
        table_count: null, row_count: null, created_at: '2026-10-12T10:00:00', catalog: null,
        error: { code: 'empty_file', message: 'Не удалось построить профиль таблицы', suggestion: 'Файл пуст или повреждён (marketing.csv). Проверьте файл и загрузите его заново.' },
      },
    ],
    results: { r_rev: revenueResult, r_reg: regionsResult, r_cat: categoriesResult, r_kpi: kpiResult },
    resultData,
    dashboards: [quarterly('d_q', 'Квартальная статистика продаж'), quarterly('d_m', 'Маркетинг Q3'), quarterly('d_r', 'Регионы')],
    chats: [
      { chat_id: 'c_q1', dataset_id: 'ds_orders', title: 'Продажи Q1', created_at: daysAgoAt(0, 14, 5), messages: [] },
      { chat_id: 'c_reg', dataset_id: 'ds_orders', title: 'Регионы', created_at: daysAgoAt(1, 17, 40), messages: [] },
      { chat_id: 'c_seg', dataset_id: 'ds_orders', title: 'Сегменты', created_at: '2026-10-12T09:30:00', messages: [] },
    ],
  });
}

/** Catalog given to every uploaded file in the mock: one table named after the file. */
export function uploadedDataset(id: string, fileName: string, files: number): DatasetDetail {
  const name = fileName.replace(/\.csv$/i, '');
  const catalog = structuredClone(ordersCatalog);
  catalog.dataset_id = id;
  catalog.name = name;
  catalog.created_at = new Date().toISOString();
  catalog.source = { kind: 'csv_upload' };
  return { ...readyDataset(catalog, 8940), source: { kind: 'csv_upload', files }, status: 'processing', catalog: null, table_count: null, row_count: null };
}

export { ordersCatalog };
