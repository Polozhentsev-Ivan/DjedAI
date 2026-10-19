import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderRoute } from '@/test/renderRoute';

afterEach(() => { vi.useRealTimers(); });

describe('LoginPage', () => {
  it('validates empty fields and enters the app', async () => {
    const { router } = await renderRoute('/login');
    await userEvent.click(screen.getByRole('button', { name: 'Войти в систему' }));
    expect(screen.getByLabelText('Пароль')).toHaveAccessibleDescription('Введите пароль');
    await userEvent.type(screen.getByLabelText('Имя пользователя / Email'), 'ivan');
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

  it('shows loading and both error texts', async () => {
    await renderRoute('/login?state=loading');
    expect(screen.getByRole('button', { name: 'Входим…' })).toBeDisabled();
  });

  it('shows the login error', async () => {
    await renderRoute('/login?state=error');
    expect(screen.getByRole('alert')).toHaveTextContent('Неверный логин или пароль');
  });

  it('shows the registration error', async () => {
    await renderRoute('/register?state=error');
    expect(screen.getByRole('alert')).toHaveTextContent('Пользователь с таким именем уже существует');
  });
});

describe('SourcesPage', () => {
  it('manages datasets in the table', async () => {
    const { router } = await renderRoute('/sources');
    const table = screen.getByRole('table', { name: 'Ваши датасеты' });
    await userEvent.click(within(table).getByRole('button', { name: 'Выбрать для анализа' }));
    expect(screen.getByRole('button', { name: 'Активный датасет' })).toHaveTextContent('retail_crm_prod');
    const rows = within(table).getAllByRole('row');
    await userEvent.click(within(rows[3]!).getByRole('button', { name: 'Удалить' }));
    expect(within(table).queryByText('marketing_q3')).not.toBeInTheDocument();
    await userEvent.click(within(table).getAllByRole('button', { name: 'Посмотреть каталог' })[0]!);
    await waitFor(() => { expect(router.state.location.pathname).toBe('/catalog/orders_2026'); });
  });

  it('shows the empty state after removing everything', async () => {
    await renderRoute('/sources');
    for (const button of screen.getAllByRole('button', { name: 'Удалить' })) await userEvent.click(button);
    expect(screen.getByText('У вас пока нет загруженных данных')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Активный датасет' })).toHaveTextContent('не выбран');
    await userEvent.click(screen.getByRole('button', { name: 'Подключить БД' }));
    expect(screen.getByLabelText('Строка подключения (DSN)')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Загрузить CSV' }));
    expect(screen.getByText(/Перетащите CSV файлы сюда/)).toBeInTheDocument();
  });

  it('fills the database form', async () => {
    await renderRoute('/sources');
    await userEvent.click(screen.getByRole('tab', { name: 'Подключить БД' }));
    await userEvent.click(screen.getByRole('radio', { name: 'ClickHouse' }));
    expect(screen.getByLabelText('Строка подключения (DSN)')).toHaveAttribute('placeholder', expect.stringContaining('clickhouse://'));
    const products = screen.getByRole('button', { name: 'products' });
    await userEvent.click(products);
    expect(products).toHaveAttribute('aria-pressed', 'true');
    await userEvent.click(screen.getByRole('button', { name: 'orders' }));
    expect(screen.getByRole('button', { name: 'orders' })).toHaveAttribute('aria-pressed', 'false');
    await userEvent.click(screen.getByRole('button', { name: 'Подключить' }));
  });

  it('plays the upload and lets it be cancelled', async () => {
    await renderRoute('/sources');
    vi.useFakeTimers();
    const input = screen.getByTestId('csv-input');
    fireEvent.change(input, { target: { files: [new File(['a,b'], 'sales.csv', { type: 'text/csv' })] } });
    expect(screen.getByText('Загрузка sales.csv · 18,4 МБ')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Отменить' }));
    expect(screen.queryByText('Загрузка sales.csv · 18,4 МБ')).not.toBeInTheDocument();
    fireEvent.change(input, { target: { files: [new File(['a,b'], 'sales.csv')] } });
    act(() => { vi.advanceTimersByTime(3200); });
    expect(screen.getByText(/Перетащите CSV файлы сюда/)).toBeInTheDocument();
  });

  it('shows the parse error with a retry', async () => {
    await renderRoute('/sources?state=error');
    expect(screen.getByRole('alert')).toHaveTextContent('не удалось разобрать строку 3');
    const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => undefined);
    await userEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Загрузить заново' }));
    expect(click).toHaveBeenCalledTimes(1);
    await userEvent.click(within(screen.getByRole('table')).getByRole('button', { name: 'Загрузить заново' }));
    expect(click).toHaveBeenCalledTimes(2);
    click.mockRestore();
  });

  it('opens the empty frame by URL', async () => {
    await renderRoute('/sources?state=empty');
    expect(screen.getByText('У вас пока нет загруженных данных')).toBeInTheDocument();
  });
});

describe('CatalogPage', () => {
  it('switches tables and asks in chat', async () => {
    const { router } = await renderRoute('/catalog/orders_2026');
    expect(screen.getByText('orders · 6 колонок · 120 000 строк')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: /customers/ }));
    expect(screen.getByText('registered_at')).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'Уверенность связи' })).toHaveAttribute('aria-valuenow', '99.8');
    await userEvent.click(screen.getByRole('button', { name: /Задать вопрос в чате/ }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/chat'); });
  });

  it('falls back to the active dataset', async () => {
    await renderRoute('/catalog');
    expect(screen.getByRole('heading', { name: 'Каталог датасета: orders_2026' })).toBeInTheDocument();
  });

  it('shows skeletons while profiling', async () => {
    await renderRoute('/catalog/orders_2026?state=loading');
    expect(screen.getByText('Строим профиль таблиц…')).toBeInTheDocument();
  });

  it('shows the empty state for an unknown dataset', async () => {
    const { router } = await renderRoute('/catalog/missing');
    expect(screen.getByText('Датасет не выбран')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Добавить источник' }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/sources'); });
  });

  it('retries after a profiling error', async () => {
    await renderRoute('/catalog/orders_2026?state=error');
    expect(screen.getByRole('alert')).toHaveTextContent('Не удалось построить профиль таблицы');
    await userEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(screen.getByText('Структура таблицы')).toBeInTheDocument();
  });
});

describe('ChatPage', () => {
  it('shows the answer and adds the chart to a dashboard', async () => {
    const { router } = await renderRoute('/chat/q1');
    expect(screen.getByText('25,4 млн ₽')).toBeInTheDocument();
    expect(screen.getByText(/SELECT date_trunc/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'В дашборд' }));
    expect(screen.getByText(/График добавлен на дашборд/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Открыть →' }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/dashboards/q'); });
  });

  it('hides the toast after a while', async () => {
    await renderRoute('/chat/q1');
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole('button', { name: 'В дашборд' }));
    act(() => { vi.advanceTimersByTime(4500); });
    expect(screen.queryByText(/График добавлен на дашборд/)).not.toBeInTheDocument();
  });

  it('streams after sending and can be stopped', async () => {
    await renderRoute('/chat/q1');
    const input = screen.getByRole('textbox', { name: 'Вопрос к данным' });
    await userEvent.type(input, 'Где больше возвратов?{Enter}');
    expect(screen.getByText('Ассистент работает')).toBeInTheDocument();
    expect(input).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Остановить' }));
    expect(screen.getByRole('button', { name: 'Отправить' })).toBeInTheDocument();
  });

  it('ignores an empty question', async () => {
    await renderRoute('/chat/q1');
    await userEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    expect(screen.queryByText('Ассистент работает')).not.toBeInTheDocument();
  });

  it('greets in a new chat and opens one from a starter', async () => {
    const { router } = await renderRoute('/chat');
    expect(screen.getByText('Здравствуйте, ivan')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Где больше возвратов/ }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/chat/q1'); });
  });

  it('shows clarification and timeout', async () => {
    await renderRoute('/chat/q1?state=error');
    expect(screen.getByText('Какую сумму показать?')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Запрос превысил таймаут 30 сек.');
    await userEvent.click(screen.getByRole('button', { name: /Сумма заказа/ }));
    expect(screen.getByText('Ассистент работает')).toBeInTheDocument();
  });
});

describe('DashboardsPage', () => {
  it('switches dashboards and removes widgets', async () => {
    await renderRoute('/dashboards');
    expect(screen.getByRole('heading', { name: 'Дашборд: Квартальная статистика продаж' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: 'Маркетинг Q3' }));
    expect(screen.getByRole('heading', { name: 'Дашборд: Маркетинг Q3' })).toBeInTheDocument();
    for (const button of screen.getAllByRole('button', { name: 'Удалить' })) await userEvent.click(button);
    expect(screen.getByText('У вас ещё нет дашбордов')).toBeInTheDocument();
  });

  it('refreshes widgets', async () => {
    await renderRoute('/dashboards/q');
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole('button', { name: /Обновить данные/ }));
    expect(screen.getAllByText('Обновление данных…')).toHaveLength(4);
    act(() => { vi.advanceTimersByTime(2400); });
    expect(screen.queryByText('Обновление данных…')).not.toBeInTheDocument();
  });

  it('keeps other widgets when one fails', async () => {
    await renderRoute('/dashboards/q?state=error');
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(screen.getByText('12,4 млн ₽')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('points to the chat when empty', async () => {
    const { router } = await renderRoute('/dashboards?state=empty');
    await userEvent.click(screen.getByRole('button', { name: 'Перейти в чат' }));
    await waitFor(() => { expect(router.state.location.pathname).toBe('/chat'); });
  });
});
