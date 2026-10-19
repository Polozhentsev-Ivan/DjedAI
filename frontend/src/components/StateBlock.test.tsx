import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { EmptyState, ErrorState, LoadingState } from './StateBlock';

describe('state blocks', () => {
  it('announces loading with steps', () => {
    render(<LoadingState size="inline" title="Загрузка sales.csv" steps={[{ label: 'Чтение файла', status: 'done' }]} />);
    expect(screen.getAllByRole('status')[0]).toHaveTextContent('Загрузка sales.csv');
    expect(screen.getByText('Чтение файла')).toBeInTheDocument();
  });

  it('uses the default copy', () => {
    render(<LoadingState />);
    expect(screen.getByText('Загрузка…')).toBeInTheDocument();
  });

  it('runs empty-state actions', async () => {
    const onAction = vi.fn();
    const onSecondary = vi.fn();
    render(<EmptyState title="Нет данных" text="Загрузите CSV" actionLabel="Загрузить CSV" onAction={onAction} secondaryLabel="Подключить БД" onSecondary={onSecondary} />);
    await userEvent.click(screen.getByRole('button', { name: 'Загрузить CSV' }));
    await userEvent.click(screen.getByRole('button', { name: 'Подключить БД' }));
    expect(onAction).toHaveBeenCalled();
    expect(onSecondary).toHaveBeenCalled();
  });

  it('renders errors as alerts with retry by default', async () => {
    const onAction = vi.fn();
    render(<ErrorState title="Таймаут" onAction={onAction} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Таймаут');
    await userEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(onAction).toHaveBeenCalled();
  });

  it('styles inline errors as destructive with an outline action', () => {
    render(<ErrorState size="inline" title="Ошибка" />);
    expect(screen.getByText('Ошибка')).toHaveClass('text-destructive');
    expect(screen.getByRole('button', { name: 'Повторить' })).toHaveClass('border-destructive');
  });

  it('can hide the action', () => {
    render(<ErrorState size="inline" title="Неверный логин" actionLabel="" />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
