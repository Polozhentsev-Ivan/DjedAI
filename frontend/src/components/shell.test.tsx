import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { DatasetSelect } from './DatasetSelect';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';

const options = [
  { id: 'orders_2026', meta: 'CSV (2) · 120 450 строк' },
  { id: 'retail_crm_prod', meta: 'Postgres · 1 420 000 строк' },
  { id: 'marketing_q3', meta: 'Ошибки загрузки', disabled: true },
];
const chats = [{ id: 'q1', title: 'Продажи Q1', meta: 'Сегодня, 14:05' }, { id: 'reg', title: 'Регионы', meta: 'Вчера' }];

describe('DatasetSelect', () => {
  it('selects a dataset and offers to add a source', async () => {
    const onChange = vi.fn();
    const onAdd = vi.fn();
    render(<DatasetSelect value="orders_2026" options={options} onChange={onChange} onAdd={onAdd} />);
    const trigger = screen.getByRole('button', { name: 'Активный датасет' });
    expect(trigger).toHaveTextContent('orders_2026');
    await userEvent.click(trigger);
    expect(screen.getByRole('menuitem', { name: /marketing_q3/ })).toHaveAttribute('data-disabled');
    await userEvent.click(screen.getByRole('menuitem', { name: /retail_crm_prod/ }));
    expect(onChange).toHaveBeenCalledWith('retail_crm_prod');
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole('menuitem', { name: /Добавить источник/ }));
    expect(onAdd).toHaveBeenCalled();
  });

  it('shows a placeholder without a dataset', () => {
    render(<DatasetSelect value={null} options={[]} onChange={vi.fn()} onAdd={vi.fn()} showLabel={false} />);
    expect(screen.getByRole('button')).toHaveTextContent('не выбран');
  });
});

describe('TopBar', () => {
  it('toggles the theme and logs out', async () => {
    document.documentElement.classList.remove('dark');
    const onLogout = vi.fn();
    render(
      <MemoryRouter>
        <TopBar dataset="orders_2026" datasets={options} user="ivan" onDatasetChange={vi.fn()} onAddSource={vi.fn()} onLogout={onLogout} />
      </MemoryRouter>,
    );
    expect(screen.getByText('ivan')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Включить тёмную тему' }));
    expect(document.documentElement).toHaveClass('dark');
    await userEvent.click(screen.getByRole('button', { name: 'Включить светлую тему' }));
    expect(document.documentElement).not.toHaveClass('dark');
    await userEvent.click(screen.getByRole('button', { name: /Выйти/ }));
    expect(onLogout).toHaveBeenCalled();
  });
});

describe('Sidebar', () => {
  it('marks the active section and links to sections', () => {
    render(<MemoryRouter><Sidebar active="catalog" chats={chats} /></MemoryRouter>);
    expect(screen.getByRole('link', { name: 'Каталог' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Дашборды' })).toHaveAttribute('href', '/dashboards');
    expect(screen.queryByText('История чатов')).not.toBeInTheDocument();
  });

  it('lists chat history in the chat section', () => {
    render(<MemoryRouter><Sidebar active="chat" chats={chats} activeChat="reg" /></MemoryRouter>);
    expect(screen.getByText('История чатов')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Регионы/ })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: /Новый чат/ })).toHaveAttribute('href', '/chat');
  });

  it('collapses to icons on tablets', () => {
    render(<MemoryRouter><Sidebar active="chat" chats={chats} compact /></MemoryRouter>);
    expect(screen.getByRole('link', { name: 'Источники' })).not.toHaveTextContent('Источники');
    expect(screen.queryByText('История чатов')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Новый чат' })).toBeInTheDocument();
  });
});
