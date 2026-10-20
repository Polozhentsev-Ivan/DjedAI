import { createDb, ordersCatalog } from '@/mocks/db';
import {
  calculation, catalogSummary, columnNote, countOrDash, dashboardMeta, datasetMeta, formatMeasure, humanFormat,
  journal, kpiView, missingCell, relationViews, sourceLabel, tableMeta, toolCall, typeLabel,
} from './view';

// Intl separates digit groups with no-break spaces; \s matches them.
const plain = (text: string) => text.replace(/\s/g, ' ');
const db = createDb();
const orders = (ordersCatalog.tables ?? [])[0]!;
const column = (name: string) => orders.columns.find((c) => c.name === name)!;

describe('dataset views', () => {
  it('labels sources and counts', () => {
    expect(sourceLabel({ kind: 'csv_upload', files: 2 })).toBe('CSV (2)');
    expect(sourceLabel({ kind: 'csv_upload' })).toBe('CSV');
    expect(sourceLabel({ kind: 'clickhouse' })).toBe('ClickHouse');
    expect(countOrDash(null)).toBe('–');
    expect(plain(countOrDash(1420000))).toBe('1 420 000');
  });

  it('describes datasets in the picker', () => {
    const [ready, , failed] = db.datasets;
    expect(plain(datasetMeta(ready!))).toBe('CSV (2) · 120 450 строк');
    expect(datasetMeta(failed!)).toBe('Ошибки загрузки');
    expect(datasetMeta({ ...ready!, status: 'processing' })).toBe('Обрабатывается…');
    expect(datasetMeta({ ...ready!, row_count: null })).toBe('CSV (2) · –');
  });

  it('summarizes the catalog header', () => {
    expect(plain(catalogSummary(db.datasets[0]!))).toMatch(/^2 таблицы · 120 450 строк · CSV · загружен \d{2}\.\d{2}\.2026 \d{2}:\d{2}$/);
    expect(plain(tableMeta(orders))).toBe('orders · 6 колонок · 120 000 строк');
  });
});

describe('column views', () => {
  it('names types like the mockup', () => {
    expect(typeLabel(column('amount'))).toBe('money (₽)');
    expect(typeLabel({ ...column('amount'), currency: null })).toBe('money');
    expect(typeLabel(column('order_id'))).toBe('text (id)');
    expect(typeLabel({ ...column('order_id'), physical_type: 'BIGINT' })).toBe('integer (id)');
    expect(typeLabel(column('status'))).toBe('category');
  });

  it('highlights large shares of missing values', () => {
    expect(missingCell(column('order_id'))).toEqual({ text: '0%', tone: 'muted' });
    expect(missingCell(column('payment_date'))).toEqual({ text: '0,1%' });
    expect(missingCell(column('discount'))).toEqual({ text: '5,2%', tone: 'warning', strong: true });
  });

  it('explains columns in the note', () => {
    const relations = ordersCatalog.relations ?? [];
    expect(columnNote(column('order_id'), orders, relations)).toBe('Уникальный ключ');
    expect(columnNote(column('customer_id'), orders, relations)).toBe('Связь с customers.id');
    expect(columnNote(column('payment_date'), orders, relations)).toBe('Формат ДД.ММ.ГГГГ');
    expect(columnNote(column('status'), orders, relations)).toBe('4 значения');
    expect(columnNote(column('amount'), orders, relations)).toBe('—');
    expect(humanFormat('%d.%m.%Y %H:%M')).toBe('ДД.ММ.ГГГГ ЧЧ:ММ');
  });

  it('builds the journal and relations', () => {
    expect(journal(ordersCatalog).map((j) => j.level)).toEqual(['info', 'info', 'info', 'warning']);
    const [relation] = relationViews(ordersCatalog);
    expect(relation).toMatchObject({ from: { label: 'Клиент', column: 'orders.customer_id' }, to: { label: 'ID клиента' }, overlap: 99.8 });
    expect(relation?.note).toBe('Многие к одному · уверенность высокая');
  });
});

describe('result views', () => {
  it('writes the tool call and calculation', () => {
    expect(toolCall('top_n', { table: 'orders', n: 5 })).toBe("top_n(table='orders', n=5)");
    const how = calculation(db.results.r_rev!);
    expect(how.tool).toBe("time_aggregate(table='orders', freq='month')");
    expect(how.columns).toEqual([{ label: 'Дата оплаты', name: 'month' }, { label: 'Сумма с НДС, руб', name: 'revenue' }]);
    expect(calculation({ ...db.results.r_rev!, sql: null, data: null }).sql).toBe('—');
  });

  it('formats measures', () => {
    expect(plain(formatMeasure(12400000, 'money'))).toBe('12,4 млн ₽');
    expect(plain(formatMeasure(4820, 'money'))).toBe('4 820 ₽');
    expect(formatMeasure(12.5, 'percent')).toBe('12,5%');
    expect(formatMeasure(null, 'int')).toBe('—');
    expect(formatMeasure('n/a', 'int')).toBe('n/a');
  });

  it('builds the KPI card from data and delta facts', () => {
    const kpi = db.dashboards[0]!.widgets[3]!;
    const view = kpiView(kpi.chart, db.resultData.r_kpi!, db.results.r_kpi);
    expect(view && { ...view, value: plain(view.value), target: plain(view.target) }).toMatchObject({
      caption: 'Сумма заказов с НДС за квартал', value: '12,4 млн ₽', delta: '+18% к цели', deltaTone: 'success', target: 'Цель квартала: 10 500 000 ₽',
    });
    expect(view?.stats.map((s) => [s.label, plain(s.value), s.delta, s.tone])).toEqual([['Заказов', '2 573', '+21%', 'success'], ['Средний чек', '4 820 ₽', '−5%', 'danger']]);
    expect(kpiView(kpi.chart, { ...db.resultData.r_kpi!, rows: [] }, undefined)).toBeNull();
    const bare = kpiView(kpi.chart, db.resultData.r_kpi!, undefined);
    expect(bare?.delta).toBe('');
    expect(bare?.target).toBe('');
  });

  it('describes the dashboard', () => {
    expect(dashboardMeta(db.dashboards[0]!)).toMatch(/^Сформирован по результатам диалога в чате · Последнее обновление: \d{2}\.\d{2}\.2026 \d{2}:\d{2}$/);
    expect(dashboardMeta({ ...db.dashboards[0]!, description: null, refreshed_at: null })).toBe('');
  });
});
