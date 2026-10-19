import { Outlet, useLocation, useNavigate } from 'react-router';
import { Sidebar, type Section } from '@/components/Sidebar';
import { TopBar } from '@/components/TopBar';
import { CHATS } from '@/fixtures/workspace';
import { useCompactLayout } from '@/lib/useMediaQuery';
import { WorkspaceProvider } from './workspace';
import { useWorkspace } from './workspaceContext';

function sectionOf(first: string | undefined): Section {
  return first === 'catalog' || first === 'chat' || first === 'dashboards' ? first : 'sources';
}

function Shell() {
  const compact = useCompactLayout();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [, first, chatId] = pathname.split('/');
  const { user, datasets, activeDataset, setActiveDataset } = useWorkspace();
  const options = datasets.map((d) => ({
    id: d.id,
    meta: d.status === 'ready' ? `${d.source} · ${d.rows} строк` : 'Ошибки загрузки',
    disabled: d.status !== 'ready',
  }));
  return (
    <div className="flex h-full flex-col overflow-hidden bg-background font-sans text-foreground">
      <TopBar
        dataset={activeDataset}
        datasets={options}
        user={user}
        compact={compact}
        onDatasetChange={setActiveDataset}
        onAddSource={() => { void navigate('/sources'); }}
        onLogout={() => { void navigate('/login'); }}
      />
      <div className="flex min-h-0 flex-1">
        <div className="flex flex-none self-stretch">
          <Sidebar active={sectionOf(first)} chats={CHATS} activeChat={chatId} compact={compact} />
        </div>
        <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-background">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export function AppLayout() {
  return (
    <WorkspaceProvider>
      <Shell />
    </WorkspaceProvider>
  );
}
