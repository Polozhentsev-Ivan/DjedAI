import { http, HttpResponse } from 'msw/http';
import { API_BASE, type Schemas } from '@/api/client';
import { createDb, ordersCatalog, uploadedDataset, type MockDb } from './db';

/**
 * Scenario of the mock backend, picked with `?mock=` in dev:
 * - `empty`: the user has no datasets and no dashboards;
 * - `slow`: reads never finish, to look at loading states;
 * - `error`: profiling and one dashboard widget fail, uploads stop on a CSV error.
 */
export type Scenario = 'normal' | 'empty' | 'slow' | 'error';

interface Options {
  db?: MockDb;
  scenario?: () => Scenario;
  /** Pause between streamed events, ms. Tests pass 0. */
  tick?: number;
}

const encoder = new TextEncoder();

function delay(ms: number | 'infinite'): Promise<void> {
  return new Promise((resolve) => { if (ms !== 'infinite') setTimeout(resolve, ms); });
}

function errorJson(status: number, error: Schemas['ErrorBody']) {
  return HttpResponse.json({ error } satisfies Schemas['ErrorResponse'], { status });
}

type SseFrame = readonly [string, unknown];

/** text/event-stream response that sends frames one by one with a pause. */
function eventStream(frames: readonly SseFrame[], tick: number, signal: AbortSignal, beforeSend?: (event: string) => void): HttpResponse<ReadableStream> {
  let cancelled = false;
  const stopped = () => signal.aborted || cancelled;
  const body = new ReadableStream<Uint8Array>({
    async start(controller) {
      for (const [event, data] of frames) {
        if (stopped()) break;
        if (tick > 0) await delay(tick);
        if (stopped()) break;
        beforeSend?.(event);
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      }
      if (!cancelled) controller.close();
    },
    cancel() { cancelled = true; },
  });
  return new HttpResponse(body, { headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' } });
}

function notFound(what: string) {
  return errorJson(404, { code: 'not_found', message: `${what} не найден` });
}

function summary({ catalog: _catalog, error: _error, ...rest }: Schemas['DatasetDetail']): Schemas['DatasetSummary'] {
  return rest;
}

export function createHandlers({ db = createDb(), scenario = () => 'normal', tick = 700 }: Options = {}) {
  const api = (path: string) => `${API_BASE}${path}`;
  const pending = new Map<string, Schemas['DatasetDetail']>();
  let uploads = 0;

  const slow = async () => {
    if (scenario() === 'slow') await delay('infinite');
  };

  const profilingFrames = (id: string): SseFrame[] => {
    const dataset = pending.get(id);
    const file = dataset ? `${dataset.name}.csv` : 'data.csv';
    const failed: SseFrame[] = [
      ['progress', { stage: 'read', progress: 1, message: `Прочитан ${file}` }],
      ['progress', { stage: 'types', progress: 0.3, message: 'Определение типов' }],
      ['error', { message: `Файл «${file}»: не удалось разобрать строку 3 (разное число колонок)`, detail: 'Проверьте разделитель и загрузите файл заново.' }],
    ];
    if (scenario() === 'error') return failed;
    return [
      ['progress', { stage: 'read', progress: 0, message: `Чтение ${file}` }],
      ['progress', { stage: 'read', progress: 1, message: `Прочитан ${file}` }],
      ['progress', { stage: 'types', progress: 0.3, message: 'Определение типов' }],
      ['progress', { stage: 'types', progress: 0.6, message: 'Определение типов' }],
      ['progress', { stage: 'types', progress: 1, message: 'Типы определены' }],
      ['progress', { stage: 'profile', progress: 0, message: 'Профилирование' }],
      ['progress', { stage: 'profile', progress: 1, message: 'Профилирование завершено' }],
      ['done', { dataset_id: id }],
    ];
  };

  const finishProfiling = (id: string, ok: boolean) => {
    const dataset = db.datasets.find((d) => d.dataset_id === id);
    const ready = pending.get(id);
    pending.delete(id);
    if (!dataset || !ready) return;
    if (!ok) {
      Object.assign(dataset, { status: 'error', error: { code: 'csv_parse', message: 'Не удалось разобрать файл' } });
      return;
    }
    const catalog = structuredClone(ordersCatalog);
    catalog.dataset_id = id;
    catalog.name = ready.name;
    catalog.source = { kind: ready.source.kind };
    Object.assign(dataset, { status: 'ready', catalog, table_count: catalog.tables?.length ?? 0, row_count: 8940 });
  };

  return [
    http.get(api('/datasets'), async () => {
      await slow();
      return HttpResponse.json(scenario() === 'empty' ? [] : db.datasets.map(summary));
    }),

    http.get(api('/datasets/:id'), async ({ params }) => {
      await slow();
      const dataset = db.datasets.find((d) => d.dataset_id === params.id);
      if (!dataset || scenario() === 'empty') return notFound('Датасет');
      if (scenario() === 'error' && dataset.status === 'ready') {
        return HttpResponse.json({
          ...dataset, status: 'error', catalog: null,
          error: { code: 'empty_file', message: 'Не удалось построить профиль таблицы', suggestion: 'Файл пуст или повреждён (orders.csv). Проверьте файл и загрузите его заново.' },
        });
      }
      return HttpResponse.json(dataset);
    }),

    http.delete(api('/datasets/:id'), ({ params }) => {
      db.datasets = db.datasets.filter((d) => d.dataset_id !== params.id);
      pending.delete(String(params.id));
      return new HttpResponse(null, { status: 204 });
    }),

    http.post(api('/datasets/upload'), async ({ request }) => {
      const form = await request.formData();
      const files = form.getAll('files').filter((f): f is File => typeof f !== 'string');
      if (files.length === 0) {
        return errorJson(422, { code: 'no_files', message: 'Выберите хотя бы один CSV-файл' });
      }
      uploads += 1;
      const id = `ds_upload_${uploads}`;
      const dataset = uploadedDataset(id, files[0]?.name ?? 'data.csv', files.length);
      pending.set(id, dataset);
      db.datasets.unshift(structuredClone(dataset));
      return HttpResponse.json({ dataset_id: id, status: 'processing' }, { status: 202 });
    }),

    http.post(api('/datasets/connect'), async ({ request }) => {
      const body = (await request.json()) as Schemas['ConnectRequest'];
      if (!/^(postgresql|clickhouse):\/\//.test(body.dsn)) {
        return errorJson(422, { code: 'bad_dsn', message: 'Строка подключения не распознана', suggestion: 'Пример: postgresql://user:password@host:5432/db' });
      }
      uploads += 1;
      const id = `ds_db_${uploads}`;
      const dataset = { ...uploadedDataset(id, body.kind === 'postgres' ? 'postgres_db' : 'clickhouse_db', 0), source: { kind: body.kind } };
      pending.set(id, dataset);
      db.datasets.unshift(structuredClone(dataset));
      return HttpResponse.json({ dataset_id: id, status: 'processing' }, { status: 202 });
    }),

    http.get(api('/datasets/:id/events'), ({ params, request }) => {
      const id = String(params.id);
      const frames = profilingFrames(id);
      // Persist the outcome before the terminal event causes the client to reload the list.
      return eventStream(frames, tick, request.signal, (event) => {
        if (event === 'done' || event === 'error') finishProfiling(id, event === 'done');
      });
    }),

    http.get(api('/chats'), async ({ request }) => {
      await slow();
      const dataset = new URL(request.url).searchParams.get('dataset_id');
      const chats = scenario() === 'empty' ? [] : db.chats.filter((c) => c.dataset_id === dataset);
      return HttpResponse.json(chats.map(({ messages: _messages, ...chat }) => chat));
    }),

    http.post(api('/chats'), async ({ request }) => {
      const body = await request.json() as Schemas['ChatCreate'];
      if (!db.datasets.some((dataset) => dataset.dataset_id === body.dataset_id)) return notFound('Датасет');
      const chat: Schemas['ChatDetail'] = { chat_id: `c_${crypto.randomUUID()}`, dataset_id: body.dataset_id, title: 'Новый чат', created_at: new Date().toISOString(), messages: [] };
      db.chats.unshift(chat);
      return HttpResponse.json({ chat_id: chat.chat_id }, { status: 201 });
    }),

    http.get(api('/chats/:id'), async ({ params }) => {
      await slow();
      const chat = db.chats.find((item) => item.chat_id === params.id);
      return chat ? HttpResponse.json(chat) : notFound('Чат');
    }),

    http.post(api('/chats/:id/messages'), async ({ params, request }) => {
      const chat = db.chats.find((item) => item.chat_id === params.id);
      if (!chat) return notFound('Чат');
      const { text } = await request.json() as Schemas['SendMessageRequest'];
      const messageId = `m_${crypto.randomUUID()}`;
      chat.messages.push({ message_id: `u_${crypto.randomUUID()}`, role: 'user', created_at: new Date().toISOString(), text });
      if (chat.title === 'Новый чат') chat.title = text.slice(0, 80);
      const blocks: Schemas['Block'][] = [
        { type: 'text', markdown: 'Выручка стабильно растёт: пик в июле — **3,4 млн ₽**.' },
        { type: 'chart', chart: { chart_id: 'chart_revenue', result_id: 'r_rev', type: 'line', title: 'Выручка по месяцам, ₽', x: { field: 'month', kind: 'time' }, y: [amount] } },
      ];
      const frames: SseFrame[] = [
        ['status', { node: 'understand', text: 'Определил колонки: «Сумма с НДС, руб», «Дата оплаты»' }],
        ['status', { node: 'execute', text: 'Анализирую данные…' }],
        ['tool_call', { tool: 'time_aggregate', params: { table: 'orders', freq: 'month' } }],
        ['tool_result', { result_id: 'r_rev', ok: true }],
        ...blocks.map((block) => ['block', block] as const),
        ['done', { message_id: messageId }],
      ];
      const timeout: SseFrame = ['error', { message: 'Запрос превысил таймаут 30 сек.', detail: 'Попробуйте уточнить период.' }];
      let persisted = false;
      return eventStream(scenario() === 'error' ? [...frames.slice(0, 1), timeout] : frames, tick, request.signal, (event) => {
        if (event === 'block' && !persisted) {
          persisted = true;
          chat.messages.push({ message_id: messageId, role: 'assistant', created_at: new Date().toISOString(), blocks, result_ids: ['r_rev'] });
        }
      });
    }),

    http.get(api('/results/:id'), async ({ params }) => {
      await slow();
      const result = db.results[String(params.id)];
      return result ? HttpResponse.json(result) : notFound('Результат');
    }),

    http.get(api('/results/:id/data'), async ({ params }) => {
      await slow();
      const data = db.resultData[String(params.id)];
      return data ? HttpResponse.json(data) : notFound('Результат');
    }),

    http.get(api('/dashboards'), async ({ request }) => {
      await slow();
      const dataset = new URL(request.url).searchParams.get('dataset_id');
      const list = scenario() === 'empty' ? [] : db.dashboards.filter((d) => d.dataset_id === dataset);
      return HttpResponse.json(list.map(({ dashboard_id, dataset_id, title, refreshed_at }) => ({ dashboard_id, dataset_id, title, refreshed_at: refreshed_at ?? null })));
    }),

    http.get(api('/dashboards/:id'), async ({ params }) => {
      await slow();
      const dashboard = db.dashboards.find((d) => d.dashboard_id === params.id);
      if (!dashboard) return notFound('Дашборд');
      if (scenario() !== 'error') return HttpResponse.json(dashboard);
      return HttpResponse.json({
        ...dashboard,
        widgets: dashboard.widgets.map((w) => {
          if (w.widget_id !== 'w_cat') return w;
          const chart: Schemas['ChartTemplate'] & { result_id?: string } = { ...w.chart };
          delete chart.result_id;
          return { ...w, chart, status: 'error', result_id: null, error: { code: 'timeout', message: 'Ошибка обновления данных', suggestion: 'Источник не ответил за 30 сек.' } };
        }),
      });
    }),

    http.post(api('/dashboards/:id/refresh'), async ({ params }) => {
      if (tick > 0) await delay(tick * 3);
      const dashboard = db.dashboards.find((d) => d.dashboard_id === params.id);
      if (!dashboard) return notFound('Дашборд');
      dashboard.refreshed_at = new Date().toISOString();
      dashboard.widgets = dashboard.widgets.map((w) => ({ ...w, status: 'ok', error: null }));
      return HttpResponse.json(dashboard);
    }),

    http.patch(api('/dashboards/:id'), async ({ params, request }) => {
      const dashboard = db.dashboards.find((d) => d.dashboard_id === params.id);
      if (!dashboard) return notFound('Дашборд');
      const patch = (await request.json()) as Schemas['DashboardPatch'];
      if (patch.title) dashboard.title = patch.title;
      if (patch.remove_widget_ids) dashboard.widgets = dashboard.widgets.filter((w) => !patch.remove_widget_ids?.includes(w.widget_id));
      return HttpResponse.json(dashboard);
    }),

    http.post(api('/dashboards'), async ({ request }) => {
      const body = (await request.json()) as Schemas['DashboardCreate'];
      const result = db.results[body.result_id];
      let dashboard = db.dashboards.find((d) => d.dashboard_id === body.dashboard_id);
      if (!dashboard) {
        dashboard = { dashboard_id: `d_${db.dashboards.length + 1}`, dataset_id: body.dataset_id, title: body.title ?? 'Новый дашборд', widgets: [], created_at: new Date().toISOString(), refreshed_at: null };
        db.dashboards.push(dashboard);
      }
      dashboard.widgets.push({
        widget_id: `w_${Date.now()}`, tool_call: { tool: result?.tool ?? 'unknown', params: result?.params ?? {} },
        layout: { x: 0, y: 0, w: 6, h: 4 },
        chart: body.chart, caption: null, result_id: body.result_id, status: 'ok', error: null,
      });
      return HttpResponse.json(dashboard);
    }),
  ];
}

const amount = { field: 'revenue', label: 'Сумма с НДС, руб', format: 'money' } as const;
