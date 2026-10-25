import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw/http';
import { createDb } from '@/mocks/db';
import { renderRoute } from '@/test/renderRoute';
import { server, useScenario } from '@/test/server';

const api = (path: string) => `http://localhost:3000/api${path}`;
const csv = (name = 'sales.csv') => new File(['date,amount\n01.01.2026,10'], name, { type: 'text/csv' });

describe('LoginPage', () => {
  it('validates empty fields and enters the app', async () => {
    const { router } = await renderRoute('/login');
    await userEvent.click(screen.getByRole('button', { name: 'Войти в систему' }));
    expect(screen.getByLabelText('Пароль')).toHaveAccessibleDescription('Введите пароль');
    await userEvent.type(screen.getByLabelText('Имя пользователя'), 'ivan');
    await userEvent.type(screen.getByLabelText('Пароль'), 'secret-123');
    await userEvent.click(screen.getByRole('button', { name: 'Войти в систему' }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/sources'); });
  });

  it('switches to registration', async () => {
    const { router } = await renderRoute('/login');
    await userEvent.click(screen.getByRole('radio', { name: 'Регистрация' }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/register'); });
    expect(screen.getByRole('button', { name: 'Зарегистрироваться' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: 'Войти' }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/login'); });
  });

  it('shows the login error', async () => {
    await renderRoute('/login');
    await userEvent.type(screen.getByLabelText('Имя пользователя'), 'ivan');
    await userEvent.type(screen.getByLabelText('Пароль'), 'wrong-password');
    await userEvent.click(screen.getByRole('button', { name: 'Войти в систему' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Неверный логин или пароль');
  });

  it('shows the registration error', async () => {
    await renderRoute('/register');
    await userEvent.type(screen.getByLabelText('Имя пользователя'), 'ivan');
    await userEvent.type(screen.getByLabelText('Пароль'), 'secret-123');
    await userEvent.click(screen.getByRole('button', { name: 'Зарегистрироваться' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Пользователь с таким именем уже существует');
  });
});

describe('SourcesPage', () => {
  it('lists datasets from the API and selects them', async () => {
    const { router } = await renderRoute('/sources');
    const table = await screen.findByRole('table', { name: 'Ваши датасеты' });
    expect(within(table).getByText('CSV (2)')).toBeInTheDocument();
    expect(within(table).getByText('Ошибки')).toBeInTheDocument();
    await userEvent.click(within(table).getByRole('button', { name: 'Выбрать для анализа' }));
    expect(screen.getByRole('button', { name: 'Активный датасет' })).toHaveTextContent('retail_crm_prod');
    await userEvent.click(within(table).getAllByRole('button', { name: 'Посмотреть каталог' })[0]!);
    await waitFor(() => { expect(router.state.location.pathname).toBe('/catalog/ds_orders'); });
  });

  it('uploads a CSV and follows profiling to the end', async () => {
    await renderRoute('/sources');
    await screen.findByRole('table');
    fireEvent.change(screen.getByTestId('csv-input'), { target: { files: [csv(), csv('more.csv')] } });
    expect(await screen.findByText(/Загрузка sales\.csv и ещё 1/)).toBeInTheDocument();
    await waitFor(() => { expect(screen.queryByText(/Загрузка sales\.csv/)).not.toBeInTheDocument(); });
    const row = (await within(screen.getByRole('table')).findByText('sales')).closest('[role="row"]') as HTMLElement;
    await waitFor(() => { expect(within(row).getByText('Готов')).toBeInTheDocument(); });
    expect(within(row).getByText('CSV (2)')).toBeInTheDocument();
  });

  it('shows the parse error from the event stream and retries', async () => {
    useScenario('error');
    await renderRoute('/sources');
    await screen.findByRole('table');
    fireEvent.change(screen.getByTestId('csv-input'), { target: { files: [csv()] } });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Файл «sales.csv»: не удалось разобрать строку 3 (разное число колонок)');
    const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => undefined);
    await userEvent.click(within(alert).getByRole('button', { name: 'Загрузить заново' }));
    expect(click).toHaveBeenCalled();
    click.mockRestore();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('reconnects a dropped progress GET without uploading the files again', async () => {
    server.use(http.get(api('/datasets/:id/events'), () => new HttpResponse('event: progress\ndata: {"stage":"read","progress":0.5}\n\n', { headers: { 'Content-Type': 'text/event-stream' } }), { once: true }));
    await renderRoute('/sources');
    await screen.findByRole('table');
    fireEvent.change(screen.getByTestId('csv-input'), { target: { files: [csv()] } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Соединение прервано до конца ответа');
    await userEvent.click(screen.getByRole('button', { name: 'Повторить получение прогресса' }));
    await waitFor(() => { expect(screen.queryByRole('alert')).not.toBeInTheDocument(); });
    const row = (await within(screen.getByRole('table')).findByText('sales')).closest('[role="row"]') as HTMLElement;
    await waitFor(() => { expect(within(row).getByText('Готов')).toBeInTheDocument(); });
    expect(within(screen.getByRole('table')).getAllByText('sales')).toHaveLength(1);
  });

  it('closes progress while accepted processing continues and can be observed again', async () => {
    server.use(http.get(api('/datasets/:id/events'), () => new HttpResponse(new ReadableStream({ start() { /* never ends */ } }), { headers: { 'Content-Type': 'text/event-stream' } })));
    await renderRoute('/sources');
    await screen.findByRole('table');
    fireEvent.change(screen.getByTestId('csv-input'), { target: { files: [csv()] } });
    expect(await within(screen.getByRole('table')).findByText('Обработка')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Скрыть прогресс' }));
    expect(within(screen.getByRole('table')).getByText('sales')).toBeInTheDocument();
    expect(screen.getByText(/Перетащите CSV файлы сюда/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Следить за обработкой' }));
    expect(await screen.findByText('Обработка sales')).toBeInTheDocument();
  });

  it('connects a database and reports a bad DSN', async () => {
    await renderRoute('/sources');
    await userEvent.click(screen.getByRole('tab', { name: 'Подключить БД' }));
    await userEvent.click(screen.getByRole('radio', { name: 'ClickHouse' }));
    expect(screen.getByLabelText('Строка подключения (DSN)')).toHaveAttribute('placeholder', expect.stringContaining('host:8123'));
    await userEvent.click(screen.getByRole('button', { name: 'products' }));
    expect(screen.getByRole('button', { name: 'products' })).toHaveAttribute('aria-pressed', 'true');
    await userEvent.click(screen.getByRole('button', { name: 'orders' }));
    expect(screen.getByRole('button', { name: 'orders' })).toHaveAttribute('aria-pressed', 'false');
    await userEvent.type(screen.getByLabelText('Строка подключения (DSN)'), 'mysql://x');
    await userEvent.click(screen.getByRole('button', { name: 'Подключить' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Строка подключения не распознана');
    await userEvent.click(screen.getByRole('button', { name: 'Закрыть' }));
    await userEvent.clear(screen.getByLabelText('Строка подключения (DSN)'));
    await userEvent.type(screen.getByLabelText('Строка подключения (DSN)'), 'http://u:p@h:8123/db');
    await userEvent.click(screen.getByRole('button', { name: 'Подключить' }));
    expect(await within(screen.getByRole('table')).findByText('clickhouse_db')).toBeInTheDocument();
  });

  it('shows the empty state and switches tabs from it', async () => {
    useScenario('empty');
    await renderRoute('/sources');
    expect(await screen.findByText('У вас пока нет загруженных данных')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Активный датасет' })).toHaveTextContent('не выбран');
    await userEvent.click(screen.getByRole('button', { name: 'Подключить БД' }));
    expect(screen.getByLabelText('Строка подключения (DSN)')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Загрузить CSV' }));
    expect(screen.getByText(/Перетащите CSV файлы сюда/)).toBeInTheDocument();
  });

  it('shows list loading and errors', async () => {
    useScenario('slow');
    const { unmount } = await renderRoute('/sources');
    expect(screen.getByText('Загружаем список датасетов…')).toBeInTheDocument();
    unmount();
    useScenario('normal');
    server.use(http.get(api('/datasets'), () => HttpResponse.json({ error: { code: 'db_down', message: 'База приложения недоступна' } }, { status: 503 })));
    await renderRoute('/sources');
    expect(await screen.findByText('База приложения недоступна')).toBeInTheDocument();
    server.resetHandlers();
  });

  it('cancels the slow list on unmount, retries after 503 and ignores the late answer', async () => {
    let answerLate: () => void = () => undefined;
    const late = new Promise<void>((resolve) => { answerLate = resolve; });
    const requests: Request[] = [];
    server.use(http.get(api('/datasets'), async ({ request }) => {
      requests.push(request);
      if (requests.length === 1) {
        await late;
        return HttpResponse.json([{ ...createDb().datasets[0]!, dataset_id: 'ds_late', name: 'late_list' }]);
      }
      if (requests.length === 2) return HttpResponse.json({ error: { code: 'db_down', message: 'База приложения недоступна' } }, { status: 503 });
      return undefined;
    }));
    const { unmount } = await renderRoute('/sources');
    expect(screen.getByText('Загружаем список датасетов…')).toBeInTheDocument();
    await waitFor(() => { expect(requests).toHaveLength(1); });
    unmount();
    expect(requests[0]!.signal.aborted).toBe(true);

    await renderRoute('/sources');
    expect(await screen.findByText('База приложения недоступна')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    const table = await screen.findByRole('table', { name: 'Ваши датасеты' });
    expect(within(table).getByText('retail_crm_prod')).toBeInTheDocument();

    answerLate();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('late_list')).not.toBeInTheDocument();
    expect(within(table).getByText('retail_crm_prod')).toBeInTheDocument();
    expect(requests).toHaveLength(3);
  });
});

describe('CatalogPage', () => {
  it('shows the catalog from the API and switches tables', async () => {
    const { router } = await renderRoute('/catalog/ds_orders');
    expect(await screen.findByText(/orders · 6 колонок · 120\s000 строк/)).toBeInTheDocument();
    expect(screen.getByText('money (₽)')).toBeInTheDocument();
    expect(screen.getByText('Связь с customers.id')).toBeInTheDocument();
    expect(screen.getByText('3 информации')).toBeInTheDocument();
    expect(screen.getByText('1 предупреждение')).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'Совпадение ключей' })).toHaveAttribute('aria-valuenow', '99.8');
    await userEvent.click(screen.getByRole('tab', { name: /customers/ }));
    expect(screen.getByText('registered_at')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Задать вопрос в чате/ }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/chat'); });
  });

  it('falls back to the active dataset', async () => {
    await renderRoute('/catalog');
    expect(await screen.findByRole('heading', { name: 'Каталог датасета: orders_2026' })).toBeInTheDocument();
  });

  it('shows skeletons while loading', async () => {
    useScenario('slow');
    await renderRoute('/catalog/ds_orders');
    expect(screen.getByText('Загружаем каталог…')).toBeInTheDocument();
  });

  it('shows the empty state for an unknown dataset', async () => {
    const { router } = await renderRoute('/catalog/missing');
    expect(await screen.findByText('Датасет не выбран')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Добавить источник' }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/sources'); });
  });

  it('shows the profiling error of a dataset', async () => {
    await renderRoute('/catalog/ds_marketing');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Не удалось построить профиль таблицы');
    expect(alert).toHaveTextContent('marketing.csv');
    await userEvent.click(within(alert).getByRole('button', { name: 'Повторить' }));
  });

  it('shows request errors with retry', async () => {
    server.use(http.get(api('/datasets/:id'), () => HttpResponse.json({ error: { code: 'boom', message: 'Сервер упал' } }, { status: 500 }), { once: true }));
    await renderRoute('/catalog/ds_orders');
    expect(await screen.findByText('Сервер упал')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByText('Структура таблицы')).toBeInTheDocument();
  });

  it('waits while a dataset is still processing', async () => {
    server.use(http.get(api('/datasets/:id'), () => HttpResponse.json({
      dataset_id: 'ds_new', name: 'new', source: { kind: 'csv_upload' }, status: 'processing',
      table_count: null, row_count: null, created_at: '2026-10-19T10:00:00', catalog: null, error: null,
    })));
    await renderRoute('/catalog/ds_new');
    expect(await screen.findByText('Строим профиль таблиц…')).toBeInTheDocument();
  });

  it('reconnects a dropped catalog progress stream and refetches terminal detail', async () => {
    const dataset = createDb().datasets[0]!;
    let ready = false;
    let subscriptions = 0;
    server.use(
      http.get(api('/datasets/ds_orders'), () => HttpResponse.json(ready ? dataset : { ...dataset, status: 'processing', catalog: null })),
      http.get(api('/datasets/ds_orders/events'), () => {
        subscriptions += 1;
        ready = subscriptions > 1;
        return new HttpResponse(ready ? 'event: done\ndata: {"dataset_id":"ds_orders"}\n\n' : 'event: progress\ndata: {"stage":"read","progress":0.5}\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
      }),
    );
    await renderRoute('/catalog/ds_orders');
    expect(await screen.findByRole('alert')).toHaveTextContent('Соединение прервано до конца ответа');
    await userEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByText('Структура таблицы')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(subscriptions).toBe(2);
  });
});

describe('DashboardsPage', () => {
  it('renders widgets from results and switches dashboards', async () => {
    await renderRoute('/dashboards');
    expect(await screen.findByRole('heading', { name: 'Дашборд: Квартальная статистика продаж' })).toBeInTheDocument();
    expect(await screen.findByText(/12,4\sмлн\s₽/)).toBeInTheDocument();
    expect(screen.getByText('+18% к цели')).toBeInTheDocument();
    expect(await screen.findByText('Санкт-Петербург')).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole('button', { name: 'Как посчитано' })[0]!);
    expect(await screen.findByText("time_aggregate(table='orders', freq='month')")).toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: 'Маркетинг Q3' }));
    expect(await screen.findByRole('heading', { name: 'Дашборд: Маркетинг Q3' })).toBeInTheDocument();
  });

  it('refreshes and removes widgets', async () => {
    await renderRoute('/dashboards/d_q');
    await screen.findByText(/12,4\sмлн\s₽/);
    await userEvent.click(screen.getByRole('button', { name: /Обновить данные/ }));
    await waitFor(() => { expect(screen.queryByText('Обновление данных…')).not.toBeInTheDocument(); });
    for (const button of screen.getAllByRole('button', { name: 'Удалить' })) await userEvent.click(button);
    expect(await screen.findByText('На дашборде нет виджетов')).toBeInTheDocument();
  });

  it('keeps other widgets when one fails and retries by refreshing', async () => {
    useScenario('error');
    await renderRoute('/dashboards/d_q');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Ошибка обновления данных');
    expect(alert).toHaveTextContent('Источник не ответил за 30 сек.');
    expect(await screen.findByText(/12,4\sмлн\s₽/)).toBeInTheDocument();
    useScenario('normal');
    await userEvent.click(within(alert).getByRole('button', { name: 'Повторить' }));
    await waitFor(() => { expect(screen.queryByRole('alert')).not.toBeInTheDocument(); });
  });

  it('points to the chat when there are no dashboards', async () => {
    useScenario('empty');
    const { router } = await renderRoute('/dashboards');
    await userEvent.click(await screen.findByRole('button', { name: 'Перейти в чат' }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/chat'); });
  });

  it('shows loading and request errors', async () => {
    useScenario('slow');
    const { unmount } = await renderRoute('/dashboards');
    expect(screen.getByText('Загружаем дашборды…')).toBeInTheDocument();
    unmount();
    useScenario('normal');
    server.use(http.get(api('/dashboards/:id'), () => HttpResponse.json({ error: { code: 'x', message: 'Дашборд сломан' } }, { status: 500 })));
    await renderRoute('/dashboards/d_q');
    expect(await screen.findByText('Дашборд сломан')).toBeInTheDocument();
  });
});
