import { LogOut, Moon, Sun } from 'lucide-react';
import { Link } from 'react-router';
import { setTheme, useTheme } from '@/lib/theme';
import { DatasetSelect, type DatasetOption } from './DatasetSelect';
import { Icon } from './Icon';

interface TopBarProps {
  dataset: string | null;
  datasets: readonly DatasetOption[];
  user: string;
  onDatasetChange: (id: string) => void;
  onAddSource: () => void;
  onLogout: () => void;
  compact?: boolean | undefined;
}

export function TopBar({ dataset, datasets, user, onDatasetChange, onAddSource, onLogout, compact = false }: TopBarProps) {
  const theme = useTheme();
  const next = theme === 'dark' ? 'light' : 'dark';
  return (
    <header className="relative z-30 box-border flex h-(--topbar-height) flex-none items-center gap-5 bg-topbar px-5 text-xs tracking-xs text-topbar-foreground">
      <Link to="/sources" className="font-display text-base font-semibold tracking-body text-topbar-foreground hover:no-underline">
        DjedAI
      </Link>
      <DatasetSelect value={dataset} options={datasets} showLabel={!compact} onChange={onDatasetChange} onAdd={onAddSource} />
      <div className="flex-1" />
      <button
        type="button"
        onClick={() => { setTheme(next); }}
        aria-label={next === 'dark' ? 'Включить тёмную тему' : 'Включить светлую тему'}
        className="flex text-topbar-muted hover:text-topbar-foreground"
      >
        <Icon icon={theme === 'dark' ? Sun : Moon} size="sm" />
      </button>
      <span className="text-topbar-muted">
        Пользователь: <span className="text-topbar-foreground">{user}</span>
      </span>
      <button type="button" onClick={onLogout} className="flex items-center gap-1 text-topbar-link hover:underline">
        <Icon icon={LogOut} size="xs" />
        Выйти
      </button>
    </header>
  );
}
