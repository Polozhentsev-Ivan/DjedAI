import { createDb, ordersCatalog } from '@/mocks/db';
import {
  calculation, catalogSummary, columnNote, countOrDash, dashboardMeta, datasetMeta, formatMeasure, humanFormat,
  journal, kpiView, missingCell, relationGraph, relationViews, sourceLabel, tableMeta, toolCall, typeLabel,
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
    const parsed = (format: string) => columnNote({ ...column('amount'), parse: { format, success_ratio: 1 } }, orders, relations);
    expect(parsed('decimal_comma, RUB')).toBe('Десятичная запятая');
    expect(parsed('decimal_point')).toBe('Десятичная точка');
    expect(parsed('iso')).toBe('Формат iso');
    expect(columnNote(column('amount'), orders, relations)).toBe('—');
    expect(humanFormat('%d.%m.%Y %H:%M')).toBe('ДД.ММ.ГГГГ ЧЧ:ММ');
  });

  it('builds the journal and relations', () => {
    expect(journal(ordersCatalog)).toEqual([
      { level: 'info', text: 'orders: 3 колонки получили системные имена — соответствие показано в структуре таблицы' },
      { level: 'info', text: 'orders.order_id: оставлена текстом: ведущие нули — это идентификатор' },
      { level: 'info', text: 'orders.amount: распознаны денежные суммы, валюта RUB, десятичный разделитель «,» — 100% непустых значений' },
      { level: 'info', text: 'orders.payment_date: распознаны даты, формат ДД.ММ.ГГГГ — 99,9% непустых значений' },
      { level: 'warning', text: 'orders.payment_date: 12 значений не распознаны как даты и заменены на пустые, например «не указана», «потом»' },
      { level: 'warning', text: 'Модель не описала в customers: region' },
    ]);
    const [relation] = relationViews(ordersCatalog);
    expect(relation).toMatchObject({ from: { label: 'Клиент', column: 'orders.customer_id' }, to: { label: 'ID клиента' }, overlap: 99.8 });
    expect(relation?.note).toBe('Многие к одному · уверенность высокая');
  });

  it('explains every journal action in words and keeps unknown ones as is', () => {
    const entry = (action: string, detail: Record<string, unknown>, column: string | null = 'c') =>
      journal({ ...ordersCatalog, warnings: [], transformations: [{ table: 't', column, action, detail }] });
    expect(entry('rename_column', { from: 'Цена', to: 'price' })).toEqual([{ level: 'info', text: 't: колонка «Цена» получила системное имя price' }]);
    expect(entry('rename_column', {})[0]?.text).toBe('t: колонка «» получила системное имя c');
    expect(entry('keep_text', { reason: 'text' })).toEqual([]);
    expect(entry('keep_text', { reason: 'unsupported_type', type: 'INTERVAL' })[0]?.text).toBe('t.c: оставлена текстом: тип источника не поддерживается (INTERVAL)');
    expect(entry('keep_text', { reason: 'mixed_currencies', currencies: ['RUB', 'USD'] })[0]?.text).toBe('t.c: оставлена текстом: в колонке смешаны валюты (currencies: RUB, USD)');
    expect(entry('keep_text', { reason: 'new_reason' })[0]?.text).toBe('t.c: оставлена текстом: new_reason');
    expect(entry('keep_text', {})[0]?.text).toBe('t.c: оставлена текстом: ');
    expect(entry('parse_bool', {})[0]?.text).toBe('t.c: распознаны логические значения');
    expect(entry('unparsed_values', { target: 'weird' })[0]).toEqual({ level: 'warning', text: 't.c: 0 значений не распознаны как weird и заменены на пустые' });
    expect(entry('unparsed_values', {})[0]?.text).toContain('как нужный тип');
    expect(entry('cast_type', { from: 'TIMESTAMP WITH TIME ZONE', to: 'TIMESTAMP', timezone: 'UTC' })[0]?.text)
      .toBe('t.c: тип TIMESTAMP WITH TIME ZONE приведён к TIMESTAMP, время переведено в UTC');
    expect(entry('cast_type', { from: 'UUID', to: 'VARCHAR' })[0]?.text).toBe('t.c: тип UUID приведён к VARCHAR');
    expect(entry('cast_type', {})[0]?.text).toBe('t.c: cast_type');
    expect(entry('parse_vector', { size: 3 }, null)[0]?.text).toBe('t: parse_vector — size: 3');
    expect(plain(tableMeta({ ...orders, duplicate_rows: 2 }))).toBe('orders · 6 колонок · 120 000 строк · 2 повтора строк');
  });

  it('draws every table and an arrow from the many side to the key', () => {
    const graph = relationGraph(ordersCatalog);
    expect(graph.nodes.map((n) => n.id)).toEqual(['orders', 'customers']);
    const [from, to] = graph.nodes;
    expect(from!.y).toBe(to!.y);
    expect(from!.x).toBeLessThan(to!.x);
    expect(graph.edges).toHaveLength(1);
    const [edge] = graph.edges;
    expect(edge).toMatchObject({ key: 'orders.customer_id->customers.id', label: 'N:1 · 99,8%', directed: true, weak: false });
    expect(edge?.title).toBe('orders.customer_id → customers.id: многие к одному, совпадение 99,8%, уверенность высокая');
    // The arrow starts at the right border of «orders» and ends at the left border of «customers».
    const half = graph.node.width / 2;
    expect(edge?.path).toBe(`M${from!.x + half},${from!.y} Q${(from!.x + to!.x) / 2},${from!.y} ${to!.x - half},${to!.y}`);
    expect([edge?.labelX, edge?.labelY]).toEqual([(from!.x + to!.x) / 2, from!.y]);
  });

  it('keeps tables without relations and skips unknown or self references', () => {
    const relation = ordersCatalog.relations![0]!;
    const lonely = relationGraph({ ...ordersCatalog, tables: ordersCatalog.tables!.slice(0, 1), relations: [relation] });
    expect(lonely.nodes).toEqual([{ id: 'orders', rows: expect.any(String) as string, x: lonely.width / 2, y: 45 }]);
    expect(lonely.edges).toEqual([]);
    const self = { ...relation, to: { table: 'orders', column: 'order_id' } };
    expect(relationGraph({ ...ordersCatalog, relations: [self] }).edges).toEqual([]);
    expect(relationGraph({ ...ordersCatalog, relations: [] }).edges).toEqual([]);
    expect(relationGraph({ ...ordersCatalog, tables: [], relations: [] }).nodes).toEqual([]);
  });

  it('spreads many tables and parallel relations apart', () => {
    const base = ordersCatalog.tables![1]!;
    const tables = ['a', 'b', 'c', 'd', 'e'].map((id) => ({ ...base, table_id: id }));
    const link = (from: string, to: string, cardinality: 'N:1' | 'N:M' = 'N:1', confidence: 'high' | 'low' = 'high') => ({
      ...ordersCatalog.relations![0]!, from: { table: from, column: 'x' }, to: { table: to, column: 'id' }, cardinality, confidence,
    });
    const graph = relationGraph({ ...ordersCatalog, tables, relations: [link('a', 'b'), link('b', 'a', 'N:M', 'low'), link('a', 'b')] });
    expect(graph.width).toBeLessThanOrEqual(480);
    expect(new Set(graph.nodes.map((n) => `${n.x},${n.y}`)).size).toBe(5);
    expect(new Set(graph.edges.map((e) => `${e.labelX},${e.labelY}`)).size).toBe(3);
    expect(graph.edges[1]).toMatchObject({ directed: false, weak: true });
    const four = relationGraph({ ...ordersCatalog, tables: tables.slice(0, 4), relations: [] });
    expect([four.width, four.height]).toEqual([360, 260]);
  });

  it('never lets table boxes overlap and keeps them inside the graph', () => {
    const base = ordersCatalog.tables![1]!;
    for (let count = 1; count <= 40; count += 1) {
      const tables = Array.from({ length: count }, (_, i) => ({ ...base, table_id: `t${i}` }));
      const { nodes, node, width, height } = relationGraph({ ...ordersCatalog, tables, relations: [] });
      for (const [i, a] of nodes.entries()) {
        expect(a.x - node.width / 2).toBeGreaterThanOrEqual(0);
        expect(a.y - node.height / 2).toBeGreaterThanOrEqual(0);
        expect(a.x + node.width / 2).toBeLessThanOrEqual(width);
        expect(a.y + node.height / 2).toBeLessThanOrEqual(height);
        for (const b of nodes.slice(i + 1)) {
          const apart = Math.abs(a.x - b.x) >= node.width + 8 || Math.abs(a.y - b.y) >= node.height + 8;
          expect(apart, `${count} tables: ${a.id} and ${b.id}`).toBe(true);
        }
      }
    }
    const twenty = relationGraph({ ...ordersCatalog, tables: Array.from({ length: 20 }, (_, i) => ({ ...base, table_id: `t${i}` })), relations: [] });
    expect(twenty.width).toBeGreaterThan(480);
  });

  it('puts the hub of a star in the middle without overlaps', () => {
    const base = ordersCatalog.tables![1]!;
    const relation = ordersCatalog.relations![0]!;
    for (const count of [4, 5, 12, 20, 30]) {
      const tables = [{ ...base, table_id: 'customers' }, ...Array.from({ length: count - 1 }, (_, i) => ({ ...base, table_id: `orders_${i}` }))];
      const relations = tables.slice(1).map((t) => ({ ...relation, from: { table: t.table_id, column: 'customer_id' } }));
      const { nodes, node, width, height, edges } = relationGraph({ ...ordersCatalog, tables, relations });
      const ring = nodes.slice(1).map((n) => n.y);
      expect(nodes[0]?.id).toBe('customers');
      expect(Math.abs(nodes[0]!.x - width / 2)).toBeLessThanOrEqual(1);
      expect(nodes[0]!.y).toBeGreaterThan(Math.min(...ring));
      expect(nodes[0]!.y).toBeLessThan(Math.max(...ring));
      expect(height).toBeGreaterThan(0);
      for (const [i, a] of nodes.entries()) {
        for (const b of nodes.slice(i + 1)) {
          expect(Math.abs(a.x - b.x) >= node.width + 8 || Math.abs(a.y - b.y) >= node.height + 8, `${count}: ${a.id} ${b.id}`).toBe(true);
        }
      }
      expect(edges.every((e) => e.label === 'N:1 · 99,8%')).toBe(true);
    }
    // Two neighbours are not a star: no table moves to the middle.
    const chain = relationGraph({ ...ordersCatalog, tables: ['a', 'b', 'c'].map((id) => ({ ...base, table_id: id })), relations: [
      { ...relation, from: { table: 'a', column: 'x' }, to: { table: 'b', column: 'id' } },
      { ...relation, from: { table: 'c', column: 'x' }, to: { table: 'b', column: 'id' } },
      { ...relation, from: { table: 'b', column: 'x' }, to: { table: 'b', column: 'id' } },
    ] });
    // A ring of three: one box on top, two below, and no empty band under them.
    const [top, ...bottom] = chain.nodes;
    for (const n of bottom) {
      expect(n.y).toBeGreaterThan(top!.y);
      expect(chain.height - (n.y + chain.node.height / 2 + 8)).toBeLessThanOrEqual(1);
    }
  });

  it('keeps the label off an edge too short for it', () => {
    const base = ordersCatalog.tables![1]!;
    const relation = ordersCatalog.relations![0]!;
    // Neighbours on a large ring sit close: the edge between them has no room for text.
    const tables = Array.from({ length: 30 }, (_, i) => ({ ...base, table_id: `t${i}` }));
    const graph = relationGraph({ ...ordersCatalog, tables, relations: [{ ...relation, from: { table: 't0', column: 'x' }, to: { table: 't1', column: 'id' } }] });
    expect(graph.edges[0]).toMatchObject({ label: '' });
    expect(graph.edges[0]?.title).toContain('совпадение 99,8%');
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
