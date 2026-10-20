import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentSteps } from './AgentSteps';
import { Button, LinkButton } from './Button';
import { ClarifyBlock } from './ClarifyBlock';
import { Segmented } from './Segmented';
import { StatusBadge } from './StatusBadge';
import { SuggestionChips } from './SuggestionChips';
import { Toast } from './Toast';
import { TransformLogItem } from './TransformLogItem';

describe('StatusBadge', () => {
  it('shows a dot only for status badges', () => {
    const { container, rerender } = render(<StatusBadge label="Готов" tone="success" />);
    expect(screen.getByText('Готов')).toHaveClass('text-success', 'rounded-full');
    expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull();
    rerender(<StatusBadge label="money (₽)" variant="type" />);
    expect(screen.getByText('money (₽)')).toHaveClass('font-mono', 'rounded-xs');
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull();
  });

  it('hides the dot on request', () => {
    const { container } = render(<StatusBadge label="+18%" tone="success" dot={false} />);
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull();
  });
});

describe('AgentSteps', () => {
  it('renders the title, step states and progress', () => {
    render(
      <AgentSteps
        title="Ассистент работает"
        steps={[
          { label: 'Чтение файла', status: 'done' },
          { label: 'Определение типов', status: 'active', pct: 60 },
          { label: 'Профилирование', status: 'pending' },
        ]}
      />,
    );
    expect(screen.getByText('Ассистент работает')).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'Определение типов' })).toHaveAttribute('aria-valuenow', '60');
    expect(screen.getByText('60%')).toBeInTheDocument();
    expect(screen.getByText('Профилирование').parentElement).toHaveClass('text-muted-foreground');
  });

  it('drops the frame and title when unframed', () => {
    render(<AgentSteps framed={false} title="Скрыт" steps={[{ label: 'Шаг', status: 'done' }]} />);
    expect(screen.queryByText('Скрыт')).not.toBeInTheDocument();
  });
});

describe('buttons and small blocks', () => {
  it('fires button and link actions', async () => {
    const onClick = vi.fn();
    render(
      <>
        <Button onClick={onClick}>Отправить</Button>
        <LinkButton tone="danger" onClick={onClick}>Удалить</LinkButton>
      </>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await userEvent.click(screen.getByRole('button', { name: 'Удалить' }));
    expect(onClick).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'Удалить' })).toHaveClass('text-destructive');
  });

  it('switches segmented options', async () => {
    const onChange = vi.fn();
    render(
      <Segmented label="Тип базы данных" value="pg" onChange={onChange} options={[{ value: 'pg', label: 'PostgreSQL' }, { value: 'ch', label: 'ClickHouse' }]} />,
    );
    expect(screen.getByRole('radio', { name: 'PostgreSQL' })).toBeChecked();
    await userEvent.click(screen.getByRole('radio', { name: 'ClickHouse' }));
    expect(onChange).toHaveBeenCalledWith('ch');
  });

  it('passes clarify choices and suggestions back', async () => {
    const onSelect = vi.fn();
    render(
      <>
        <ClarifyBlock
          question="Какую сумму показать?"
          hint="Две колонки с суммой"
          options={[{ label: 'Сумма заказа', name: 'order_amount', hint: 'все заказы' }, { label: 'Сумма доставленных', name: 'delivered_amount' }]}
          onSelect={onSelect}
        />
        <SuggestionChips items={['Топ-10 клиентов']} onSelect={onSelect} />
      </>,
    );
    await userEvent.click(screen.getByRole('button', { name: /Сумма доставленных/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Топ-10 клиентов' }));
    expect(onSelect.mock.calls).toEqual([['delivered_amount'], ['Топ-10 клиентов']]);
    expect(screen.getByText('· все заказы', { exact: false })).toBeInTheDocument();
  });

  it('shows toast action', async () => {
    const onAction = vi.fn();
    render(<Toast message="График добавлен" actionLabel="Открыть →" onAction={onAction} />);
    await userEvent.click(screen.getByRole('button', { name: 'Открыть →' }));
    expect(onAction).toHaveBeenCalled();
  });

  it('labels log levels', () => {
    render(
      <>
        <TransformLogItem level="info" text="Распознан формат валюты" />
        <TransformLogItem level="warning" text="12 строк заменены на NULL" />
      </>,
    );
    expect(screen.getByText('Информация')).toHaveClass('text-muted-foreground');
    expect(screen.getByText('Предупреждение')).toHaveClass('text-warning');
  });
});
