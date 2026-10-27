import { Database, LayoutDashboard, MessageSquare, Plus, Settings, Table2, type LucideIcon } from 'lucide-react';
import { Link } from 'react-router';
import { cn } from '@/lib/cn';
import { Icon } from './Icon';

export type Section = 'sources' | 'catalog' | 'chat' | 'dashboards' | 'settings';

export interface ChatItem {
  id: string;
  title: string;
  meta: string;
}

interface SidebarProps {
  active: Section;
  chats: readonly ChatItem[];
  activeChat?: string | undefined;
  compact?: boolean | undefined;
}

const nav: readonly { section: Section; label: string; icon: LucideIcon; to: string }[] = [
  { section: 'sources', label: 'Источники', icon: Database, to: '/sources' },
  { section: 'catalog', label: 'Каталог', icon: Table2, to: '/catalog' },
  { section: 'chat', label: 'Чат', icon: MessageSquare, to: '/chat' },
  { section: 'dashboards', label: 'Дашборды', icon: LayoutDashboard, to: '/dashboards' },
  { section: 'settings', label: 'Настройки', icon: Settings, to: '/settings' },
];

/** Section navigation; in the chat section it also lists chat history. */
export function Sidebar({ active, chats, activeChat, compact = false }: SidebarProps) {
  const inChat = active === 'chat';
  return (
    <aside
      className={cn(
        'box-border flex min-h-full flex-none flex-col gap-0.5 self-stretch overflow-auto border-r border-sidebar-border bg-sidebar text-sm tracking-sm text-sidebar-foreground',
        compact ? 'w-(--sidebar-width-collapsed) items-center px-2.5 py-3' : 'w-(--sidebar-width) items-stretch px-3 py-4',
      )}
    >
      <nav aria-label="Разделы" className={cn('flex flex-col gap-0.5', compact ? 'items-center' : 'items-stretch')}>
        {nav.map((item) => {
          const on = item.section === active;
          return (
            <Link
              key={item.section}
              to={item.to}
              title={item.label}
              aria-label={compact ? item.label : undefined}
              aria-current={on ? 'page' : undefined}
              className={cn(
                'box-border flex items-center gap-2.5 text-sm hover:no-underline',
                compact ? 'size-(--size-control) justify-center rounded-md p-0' : 'h-(--size-control-md) w-full justify-start rounded-sm px-2.5',
                on ? 'bg-sidebar-accent font-semibold text-sidebar-accent-foreground' : 'bg-transparent font-normal text-sidebar-foreground',
              )}
            >
              <Icon icon={item.icon} size={compact ? 'lg' : 'md'} />
              {!compact && <span>{item.label}</span>}
            </Link>
          );
        })}
      </nav>
      {inChat && !compact && (
        <>
          <div className="mx-0.5 my-3 h-px self-stretch bg-sidebar-border" />
          <Link
            to="/chat"
            className="press mb-2 flex h-(--size-control-md) items-center justify-center gap-1.5 rounded-full border border-link bg-transparent text-sm text-link hover:no-underline"
          >
            <Icon icon={Plus} size="sm" />
            Новый чат
          </Link>
          <div className="px-2.5 py-1 text-xs text-muted-foreground">История чатов</div>
          {chats.map((chat) => {
            const on = chat.id === activeChat;
            return (
              <Link
                key={chat.id}
                to={`/chat/${chat.id}`}
                aria-current={on ? 'page' : undefined}
                className={cn(
                  'flex flex-col items-start gap-0.5 rounded-sm px-2.5 py-2 text-left hover:no-underline',
                  on ? 'bg-sidebar-accent text-sidebar-accent-foreground' : 'bg-transparent text-foreground',
                )}
              >
                <span className={cn('text-sm', on ? 'font-semibold' : 'font-normal')}>{chat.title}</span>
                <span className="text-xs text-muted-foreground">{chat.meta}</span>
              </Link>
            );
          })}
        </>
      )}
      {inChat && compact && (
        <>
          <div className="my-2 h-px w-(--size-control-sm) bg-sidebar-border" />
          <Link
            to="/chat"
            title="Новый чат"
            aria-label="Новый чат"
            className="flex size-(--size-control) items-center justify-center rounded-full border border-link bg-transparent text-link hover:no-underline"
          >
            <Icon icon={Plus} size="lg" />
          </Link>
        </>
      )}
    </aside>
  );
}
