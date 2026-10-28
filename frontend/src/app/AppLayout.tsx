import { Outlet, useLocation, useNavigate } from 'react-router';
import { Sidebar, type Section } from '@/components/Sidebar';
import { TopBar } from '@/components/TopBar';
import { useChats } from '@/api/queries';
import { datasetMeta } from '@/api/view';
import { formatRelativeDay } from '@/lib/format';
import { useCompactLayout } from '@/lib/useMediaQuery';
import { WorkspaceProvider } from './workspace';
import { useWorkspace } from './workspaceContext';
import { logout } from '@/api/auth';

function sectionOf(first: string | undefined): Section {
  return first === 'catalog' || first === 'chat' || first === 'dashboards' || first === 'settings' ? first : 'sources';
}

function Shell() {
  const compact = useCompactLayout();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [, first, chatId] = pathname.split('/');
  const { user, datasets, activeDataset, setActiveDataset } = useWorkspace();
  const chats = useChats(activeDataset);
  const options = datasets.map((d) => ({ id: d.dataset_id, label: d.name, meta: datasetMeta(d), disabled: d.status !== 'ready' }));
  const chatItems = (chats.data ?? []).map((c) => ({ id: c.chat_id, title: c.title, meta: formatRelativeDay(c.created_at) }));
  return (
    <div className="flex h-full flex-col overflow-hidden bg-background font-sans text-foreground">
      <TopBar
        dataset={activeDataset}
        datasets={options}
        user={user}
        compact={compact}
        onDatasetChange={(id) => {
          setActiveDataset(id);
          if (first === 'catalog') void navigate(`/catalog/${encodeURIComponent(id)}`);
          else if (first === 'chat' || first === 'dashboards') void navigate(`/${first}`);
        }}
        onAddSource={() => { void navigate('/sources'); }}
        onLogout={() => { void logout().catch(() => undefined); }}
      />
      <div className="flex min-h-0 flex-1">
        <div className="flex flex-none self-stretch">
          <Sidebar active={sectionOf(first)} chats={chatItems} activeChat={chatId} compact={compact} />
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
