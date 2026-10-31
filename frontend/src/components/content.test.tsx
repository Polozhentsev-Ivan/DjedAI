import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChartCard } from './ChartCard';
import { ChatMessage } from './ChatMessage';
import { DataTable } from './DataTable';
import { HowCalculated } from './HowCalculated';
import { Markdown } from './Markdown';
import { WidgetCard } from './WidgetCard';

const how = { tool: 'time_aggregate', params: [{ name: 'table', value: "'orders'" }], columns: [{ label: 'Сумма', name: 'amount' }], sql: 'SELECT 1', stats: '120 000 строк' };

describe('DataTable', () => {
  it('renders every cell type', async () => {
    const onOpen = vi.fn();
    render(
      <DataTable
        label="Датасеты"
        columns={[
          { key: 'name', label: 'Название', type: 'name' },
          { key: 'src', label: 'Источник', type: 'muted' },
          { key: 'rows', label: 'Строк', type: 'number', align: 'right' },
          { key: 'sys', label: 'В системе', type: 'mono' },
          { key: 'type', label: 'Тип', type: 'type' },
          { key: 'status', label: 'Статус', type: 'badge' },
          { key: 'miss', label: 'Пропуски', type: 'strong' },
          { key: 'actions', label: 'Действия', type: 'actions' },
          { key: 'none', label: 'Пусто' },
        ]}
        rows={[{
          id: 'orders', highlight: true,
          cells: {
            name: { text: 'orders_2026', tag: 'активный' }, src: 'CSV (2)', rows: '120 450', sys: 'amount',
            type: { text: 'money (₽)' }, status: { text: 'Готов', tone: 'success' }, miss: { text: '5,2%', tone: 'warning' },
            actions: [{ label: 'Посмотреть каталог', onClick: onOpen }, { label: 'Удалить', tone: 'danger', onClick: vi.fn() }],
          },
        }]}
      />,
    );
    expect(screen.getByRole('table', { name: 'Датасеты' })).toBeInTheDocument();
    expect(screen.getAllByRole('columnheader')).toHaveLength(9);
    expect(screen.getByText('активный')).toBeInTheDocument();
    expect(screen.getByText('amount')).toHaveClass('font-mono');
    expect(screen.getByText('5,2%')).toHaveClass('text-warning', 'font-semibold');
    expect(screen.getByText('120 450').closest('[role="cell"]')).toHaveClass('text-right');
    expect(screen.getAllByRole('row')[1]).toHaveClass('bg-secondary');
    await userEvent.click(screen.getByRole('button', { name: 'Посмотреть каталог' }));
    expect(onOpen).toHaveBeenCalled();
  });
});

describe('HowCalculated', () => {
  it('toggles the collapsible body', async () => {
    render(<HowCalculated summary="1 инструмент" {...how} />);
    expect(screen.queryByText('SELECT 1')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Как посчитано/ }));
    expect(screen.getByText('SELECT 1')).toBeInTheDocument();
    expect(screen.getByText('120 000 строк')).toBeInTheDocument();
  });

  it('lists data limitations', () => {
    render(<HowCalculated variant="panel" {...how} limitations={['4 из 100 значений не распознаны']} />);
    expect(screen.getByText('Ограничения')).toBeInTheDocument();
    expect(screen.getByText('4 из 100 значений не распознаны')).toBeInTheDocument();
  });

  it('shows the panel variant without a toggle', () => {
    render(<HowCalculated variant="panel" {...how} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.getByText('time_aggregate')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Параметры' })).toHaveTextContent("table = 'orders'");
    expect(screen.queryByText('Источник результата')).not.toBeInTheDocument();
  });

  it('links the source result of a chained calculation', () => {
    render(<HowCalculated variant="panel" {...how} origin="r_parent" />);
    expect(screen.getByText('Источник результата')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'r_parent' })).toHaveAttribute('href', 'http://localhost:3000/api/results/r_parent');
  });
});

describe('ChatMessage', () => {
  it('renders the user bubble', () => {
    render(<ChatMessage role="user" text="Покажи выручку" />);
    expect(screen.getByText('Покажи выручку').parentElement).toHaveAttribute('data-role', 'user');
  });

  it('renders assistant parts, facts, caret and children', () => {
    render(
      <ChatMessage role="assistant" parts={[{ t: 'Пик — ' }, { t: '+34%', tone: 'success' }]} facts={[{ label: 'Итого', value: '25,4 млн ₽' }]} streaming>
        <span>блок</span>
      </ChatMessage>,
    );
    expect(screen.getByText('+34%')).toHaveClass('text-success', 'font-semibold');
    expect(screen.getByText('25,4 млн ₽')).toBeInTheDocument();
    expect(screen.getByTestId('stream-caret')).toBeInTheDocument();
    expect(screen.getByText('блок')).toBeInTheDocument();
  });

  it('falls back to plain text for the assistant', () => {
    render(<ChatMessage role="assistant" text="Готово" />);
    expect(screen.getByText('Готово')).toBeInTheDocument();
  });
});

describe('ChartCard', () => {
  it('shows actions only when labelled', async () => {
    const onPrimary = vi.fn();
    const { rerender } = render(<ChartCard title="Выручка" subtitle="orders" primaryLabel="В дашборд" onPrimary={onPrimary} secondaryLabel="Экспорт"><div>chart</div></ChartCard>);
    await userEvent.click(screen.getByRole('button', { name: 'В дашборд' }));
    expect(onPrimary).toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Экспорт' })).toBeInTheDocument();
    rerender(<ChartCard title="Выручка"><div>chart</div></ChartCard>);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});

describe('WidgetCard', () => {
  const kpi = {
    caption: 'Сумма за квартал', value: '12,4 млн ₽', delta: '+18% к цели', deltaTone: 'success' as const, target: 'Цель: 10 500 000 ₽',
    stats: [{ label: 'Заказов', value: '2 573', delta: '+21%', tone: 'success' as const }, { label: 'Средний чек', value: '4 820 ₽', delta: '−5%', tone: 'danger' as const }],
  };

  it('swaps the chart for the calculation and back', async () => {
    render(<WidgetCard title="Выручка" subtitle="Пик — июль" how={how} chart={<div>chart</div>} />);
    expect(screen.getByText('chart')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Как посчитано' }));
    expect(screen.queryByText('chart')).not.toBeInTheDocument();
    expect(screen.getByText('SELECT 1')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Скрыть расчёт' }));
    expect(screen.getByText('chart')).toBeInTheDocument();
  });

  it('renders KPI values', () => {
    render(<WidgetCard title="KPI" subtitle="цель" how={how} kpi={kpi} />);
    expect(screen.getByText('12,4 млн ₽')).toBeInTheDocument();
    expect(screen.getByText('−5%')).toHaveClass('text-destructive');
  });

  it('shows loading overlay and error with retry', async () => {
    const onRetry = vi.fn();
    const onRemove = vi.fn();
    const { rerender } = render(<WidgetCard title="Топ" subtitle="" how={how} state="loading" chart={<div>chart</div>} />);
    expect(screen.getByText('Обновление данных…')).toBeInTheDocument();
    rerender(<WidgetCard title="Топ" subtitle="" how={how} state="error" chart={<div>chart</div>} onRetry={onRetry} onRemove={onRemove} />);
    expect(screen.queryByText('chart')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    await userEvent.click(screen.getByRole('button', { name: 'Удалить' }));
    expect(onRetry).toHaveBeenCalled();
    expect(onRemove).toHaveBeenCalled();
  });
});

describe('Markdown', () => {
  it('renders a pipe table with inline formatting and keeps the text around it', () => {
    const { container } = render(<Markdown text={'Итоги:\n| Отдел | Заявок |\n|---|---|\n| Склад | 1 990 |\n| **Итого** | **14 184** |\nКак посчитано.'} />);
    expect([...container.querySelectorAll('th')].map((cell) => cell.textContent)).toEqual(['Отдел', 'Заявок']);
    expect([...container.querySelectorAll('tbody tr')].map((row) => [...row.children].map((cell) => cell.textContent))).toEqual([['Склад', '1 990'], ['Итого', '14 184']]);
    expect(container.querySelector('tbody strong')).toHaveTextContent('Итого');
    expect(container.querySelectorAll('p')[0]).toHaveTextContent('Итоги:');
    expect(container.querySelectorAll('p')[1]).toHaveTextContent('Как посчитано.');
  });

  it('keeps pipes as text without a table separator', () => {
    const { container } = render(<Markdown text={'a | b\nc | d'} />);
    expect(container.querySelector('table')).toBeNull();
    expect(container).toHaveTextContent('a | b');
  });

  it('keeps an escaped pipe inside a cell', () => {
    const { container } = render(<Markdown text={'| Код |\n|---|\n| a\\|b |'} />);
    expect(container.querySelector('td')).toHaveTextContent('a|b');
  });

  it('renders the agent subset as elements and keeps HTML as text', () => {
    const { container } = render(<Markdown text={'## Итог\nВыручка выросла на **12%**, *см.* `orders`.\nВторая строка\n\n- первый\n- второй\n1. шаг\n\n<b>не HTML</b>'} />);
    expect(screen.getByText('Итог')).toHaveClass('font-semibold');
    expect(screen.getByText('12%').tagName).toBe('STRONG');
    expect(screen.getByText('см.').tagName).toBe('EM');
    expect(screen.getByText('orders').tagName).toBe('CODE');
    expect(container.querySelector('p br')).not.toBeNull();
    expect(container.querySelectorAll('ul li')).toHaveLength(2);
    expect(container.querySelectorAll('ol li')).toHaveLength(1);
    expect(container.querySelector('b')).toBeNull();
    expect(screen.getByText('<b>не HTML</b>')).toBeInTheDocument();
  });
});
