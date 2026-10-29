import { http, HttpResponse } from 'msw/http';
import { API_BASE, type Schemas } from '@/api/client';
import { createDb, ENV_MODEL, ordersCatalog, uploadedDataset, type MockDb } from './db';

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
function eventStream(frames: readonly SseFrame[], tick: number, signal: AbortSignal, beforeSend?: (event: string) => void, isCancelled = () => false): HttpResponse<ReadableStream> {
  let cancelled = false;
  const stopped = () => signal.aborted || cancelled || isCancelled();
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

const MAX_RECIPE_STEPS = 5;

/** Like the server: one call, or the source_result_id chain from its root as recipe steps (no temporary ids). */
function widgetSource(db: MockDb, result: Schemas['ToolResultMeta']): Pick<Schemas['Widget'], 'tool_call' | 'recipe'> | { error: string } {
  const chain = [result];
  for (let step = result; step.source_result_id;) {
    if (chain.length >= MAX_RECIPE_STEPS) return { error: `Глубина зависимостей превышает лимит (максимум ${MAX_RECIPE_STEPS} шагов)` };
    const parent = db.results[step.source_result_id];
    if (!parent) return { error: 'Невозможно сохранить виджет: исходный шаг вычисления не найден или недоступен' };
    if (chain.includes(parent)) return { error: 'Обнаружен цикл в зависимостях результатов' };
    chain.push(parent);
    step = parent;
  }
  const calls = chain.reverse().map((step) => {
    const { source_result_id: _parent, ...params } = step.params ?? {};
    return { tool: step.tool ?? 'run_sql', params };
  });
  const [single, ...rest] = calls;
  if (single && rest.length === 0) return { tool_call: single };
  const steps = calls.map((call, i) => ({ step_id: `step_${i + 1}`, call, source_step_id: i > 0 ? `step_${i}` : null }));
  return { recipe: { schema_version: 1, steps, output_step_id: `step_${steps.length}` } };
}

function summary({ catalog: _catalog, error: _error, ...rest }: Schemas['DatasetDetail']): Schemas['DatasetSummary'] {
  return rest;
}

export function createHandlers({ db = createDb(), scenario = () => 'normal', tick = 700 }: Options = {}) {
  const api = (path: string) => `${API_BASE}${path}`;
  const pending = new Map<string, Schemas['DatasetDetail']>();
  let uploads = 0;
  const account = (username: string): Schemas['UserRead'] => ({ user_id: `u_${username}`, username, is_active: true, created_at: '2026-10-22T00:00:00Z' });
  const accounts = new Map([['ivan', { password: 'secret-123', user: account('ivan') }]]);
  const sessions = new Map([['test-session', account('ivan')]]);

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
    http.post(api('/auth/register'), async ({ request }) => {
      const body = await request.json() as Schemas['UserCreate'];
      if (accounts.has(body.username)) return errorJson(409, { code: 'username_taken', message: 'Пользователь с таким именем уже существует' });
      const user = account(body.username);
      accounts.set(body.username, { password: body.password, user });
      return HttpResponse.json(user, { status: 201 });
    }),
    http.post(api('/auth/login'), async ({ request }) => {
      const body = await request.json() as Schemas['UserLogin'];
      const found = accounts.get(body.username);
      if (!found || found.password !== body.password) return errorJson(401, { code: 'unauthorized', message: 'Неверный логин или пароль' });
      const access_token = `mock-${crypto.randomUUID()}`;
      sessions.set(access_token, found.user);
      return HttpResponse.json({ access_token, expires_in: 86400, token_type: 'bearer' });
    }),
    http.get(api('/auth/me'), ({ request }) => {
      const user = sessions.get(request.headers.get('Authorization')?.replace(/^Bearer /, '') ?? '');
      return user ? HttpResponse.json(user) : errorJson(401, { code: 'unauthorized', message: 'Требуется действующая сессия' });
    }),
    http.post(api('/auth/logout'), ({ request }) => {
      sessions.delete(request.headers.get('Authorization')?.replace(/^Bearer /, '') ?? '');
      return new HttpResponse(null, { status: 204 });
    }),
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

    http.post(api('/datasets/:id/refresh'), async ({ params }) => {
      await slow();
      const dataset = db.datasets.find((d) => d.dataset_id === params.id);
      if (!dataset) return notFound('Датасет');
      if (dataset.status !== 'ready' || !dataset.catalog?.revision) return errorJson(409, { code: 'conflict', message: 'Датасет ещё не готов к запросам' });
      // The mock publishes the next revision at once; the real service builds it in the background.
      const revision = { ...dataset.catalog.revision, number: dataset.catalog.revision.number + 1, profiled_at: new Date().toISOString() };
      dataset.catalog = { ...dataset.catalog, revision };
      Object.assign(dataset, { revision, freshness: { state: 'fresh', profiled_at: revision.profiled_at, checked_at: revision.profiled_at, changes: [], reason: null }, refresh: null });
      return HttpResponse.json(dataset, { status: 202 });
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

    http.post(api('/datasets/connect/check'), async ({ request }) => {
      const body = (await request.json()) as Schemas['SourceCheckRequest'];
      if (!/^(postgresql|clickhouse|https?):\/\//.test(body.dsn)) {
        return errorJson(422, { code: 'http_422', message: 'Проверьте строку подключения' });
      }
      if (body.dsn.includes('wrong')) return errorJson(422, { code: 'http_422', message: 'Неверный пользователь или пароль' });
      const tables = body.kind === 'clickhouse'
        ? [{ name: 'sales', row_count: 4376 }, { name: 'stores', row_count: 10 }]
        : [{ name: 'orders', row_count: null }, { name: 'customers', row_count: null }];
      return HttpResponse.json({ kind: body.kind, source: 'reader@db:5432/demo', tables, truncated: false });
    }),

    http.get(api('/settings/model'), () => HttpResponse.json(db.modelSettings)),

    http.put(api('/settings/model'), async ({ request }) => {
      const body = (await request.json()) as Schemas['ModelSettingsUpdate'];
      const settings = db.modelSettings;
      if (!settings.editable_connection && (body.api_key != null || body.base_url != null)) {
        return errorJson(403, { code: 'forbidden', message: 'Ключ и адрес API задаёт администратор в .env' });
      }
      if (body.model_name && !settings.editable_connection && !settings.allowed_models.includes(body.model_name)) {
        return errorJson(422, { code: 'http_422', message: 'Модель не входит в список разрешённых администратором' });
      }
      if (body.model_name != null) settings.model_name = body.model_name || ENV_MODEL.model_name;
      if (body.base_url != null) settings.base_url = body.base_url || ENV_MODEL.base_url;
      if (body.api_key != null) settings.api_key_from_ui = body.api_key !== '';
      settings.matches_env = settings.model_name === ENV_MODEL.model_name && settings.base_url === ENV_MODEL.base_url;
      return HttpResponse.json(settings);
    }),

    http.post(api('/settings/model/check'), async ({ request }) => {
      const body = (await request.json()) as Schemas['ModelSettingsUpdate'];
      const result: Schemas['ModelCheckResult'] = body.api_key?.includes('wrong')
        ? { ok: false, code: 'invalid_key', message: 'Ключ API не принят. Проверьте ключ' }
        : { ok: true, code: 'ok', message: 'Модель ответила' };
      return HttpResponse.json(result);
    }),

    http.post(api('/datasets/connect'), async ({ request }) => {
      const body = (await request.json()) as Schemas['ConnectRequest'];
      if (!/^(postgresql|clickhouse|https?):\/\//.test(body.dsn)) {
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
      const { text, request_id: requestId } = await request.json() as Schemas['SendMessageRequest'];
      const messageId = `m_${crypto.randomUUID()}`;
      const now = new Date().toISOString();
      const turn: Schemas['ChatTurnState'] = { run_id: `run_${crypto.randomUUID()}`, status: 'running', node: 'understand', phase: 'stage', message: 'Разбираю вопрос…', started_at: now, deadline_at: new Date(Date.now() + 30000).toISOString() };
      const userMessage: Schemas['ChatMessage'] = { message_id: `m_${requestId?.replaceAll('-', '') ?? crypto.randomUUID()}`, role: 'user', created_at: now, text, turn };
      chat.messages.push(userMessage);
      if (chat.title === 'Новый чат') chat.title = text.slice(0, 80);
      const blocks: Schemas['Block'][] = [
        { type: 'text', markdown: 'Выручка стабильно растёт: пик в июле — **3,4 млн ₽**.' },
        { type: 'chart', chart: { chart_id: 'chart_revenue', result_id: 'r_rev', type: 'line', title: 'Выручка по месяцам, ₽', x: { field: 'month', kind: 'time' }, y: [amount] } },
      ];
      const frames: SseFrame[] = [
        ['status', { node: 'understand', text: turn.message, message_id: userMessage.message_id, run_id: turn.run_id }],
        ['status', { node: 'execute', text: 'Анализирую данные…', message_id: userMessage.message_id, run_id: turn.run_id }],
        ['tool_call', { tool: 'time_aggregate', params: { table: 'orders', freq: 'month' } }],
        ['tool_result', { result_id: 'r_rev', ok: true }],
        ...blocks.map((block) => ['block', block] as const),
        ['done', { message_id: messageId }],
      ];
      const timeout: SseFrame = ['error', { code: 'model_timeout', message: 'Модель не ответила за отведённое время', detail: 'Отправьте вопрос снова, если нужно.' }];
      let persisted = false;
      return eventStream(scenario() === 'error' ? [...frames.slice(0, 1), timeout] : frames, tick, request.signal, (event) => {
        if (event === 'block' && !persisted) {
          persisted = true;
          Object.assign(turn, { status: 'success', message: 'Ответ готов', finished_at: new Date().toISOString(), assistant_message_id: messageId });
          chat.messages.push({ message_id: messageId, role: 'assistant', created_at: new Date().toISOString(), blocks, result_ids: ['r_rev'] });
        }
        if (event === 'error') Object.assign(turn, { status: 'timeout', code: 'model_timeout', message: 'Модель не ответила за отведённое время', suggestion: 'Отправьте вопрос снова, если нужно.', finished_at: new Date().toISOString() });
      }, () => turn.status === 'cancelled');
    }),

    http.post(api('/chats/:id/messages/:messageId/cancel'), ({ params }) => {
      const message = db.chats.find((item) => item.chat_id === params.id)?.messages.find((item) => item.message_id === params.messageId);
      if (!message?.turn) return notFound('Вопрос');
      if (message.turn.status === 'running') Object.assign(message.turn, { status: 'cancelled', code: 'cancelled', message: 'Получение ответа остановлено', suggestion: 'Отправьте вопрос снова, если нужно.', finished_at: new Date().toISOString() });
      return HttpResponse.json(message.turn);
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
      if (!result) return notFound('Результат');
      const source = widgetSource(db, result);
      if ('error' in source) return errorJson(422, { code: 'invalid_recipe', message: source.error });
      let dashboard = db.dashboards.find((d) => d.dashboard_id === body.dashboard_id);
      if (body.dashboard_id && dashboard?.dataset_id !== body.dataset_id) return notFound('Дашборд');
      if (!dashboard) {
        dashboard = { dashboard_id: `d_${db.dashboards.length + 1}`, dataset_id: body.dataset_id, title: body.title ?? body.chart.title, widgets: [], created_at: new Date().toISOString(), refreshed_at: null };
        db.dashboards.push(dashboard);
      }
      const y = Math.max(0, ...dashboard.widgets.map((w) => w.layout.y + w.layout.h));
      dashboard.widgets.push({
        widget_id: `w_${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`, ...source,
        layout: { x: 0, y, w: 6, h: 4 },
        chart: body.chart, caption: body.chart.subtitle ?? null, result_id: body.result_id, status: 'ok', error: null,
      });
      return HttpResponse.json(dashboard);
    }),
  ];
}

const amount = { field: 'revenue', label: 'Сумма с НДС, руб', format: 'money' } as const;
