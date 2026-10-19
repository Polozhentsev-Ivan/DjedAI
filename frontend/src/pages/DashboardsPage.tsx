import { Plus, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { PageContainer, PageTitle } from '@/app/PageContainer';
import { useFixtureState } from '@/app/useFixtureState';
import { useWorkspace } from '@/app/workspaceContext';
import { DemoChart } from '@/charts/DemoChart';
import { Button } from '@/components/Button';
import { Icon } from '@/components/Icon';
import { EmptyState } from '@/components/StateBlock';
import { WidgetCard, type WidgetState } from '@/components/WidgetCard';
import { DASHBOARD_META, DASHBOARDS, FAILING_WIDGET, WIDGETS } from '@/fixtures/dashboards';
import { cn } from '@/lib/cn';

export function DashboardsPage() {
  const navigate = useNavigate();
  const { dashboardId } = useParams();
  const { state, set, simulate } = useFixtureState();
  const { activeDataset } = useWorkspace();
  const [removed, setRemoved] = useState<ReadonlySet<string>>(new Set());
  const dashboard = DASHBOARDS.find((d) => d.id === dashboardId) ?? DASHBOARDS[0];
  const widgets = WIDGETS.filter((w) => !removed.has(w.id));
  const empty = state === 'empty' || widgets.length === 0 || !dashboard;
  const loading = state === 'loading';
  const toChat = () => { void navigate('/chat'); };

  const widgetState = (id: string): WidgetState => {
    if (loading) return 'loading';
    return state === 'error' && id === FAILING_WIDGET ? 'error' : 'default';
  };

  return (
    <PageContainer>
      <div className="flex flex-col gap-5 text-sm leading-normal tracking-sm">
        {empty ? (
          <>
            <PageTitle>Дашборды</PageTitle>
            <div className="rounded-lg border border-border bg-card">
              <EmptyState title="У вас ещё нет дашбордов" text="Создайте график в чате и нажмите «Добавить на дашборд»." actionLabel="Перейти в чат" onAction={toChat} />
            </div>
          </>
        ) : (
          <>
            <nav aria-label="Дашборды датасета" className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1.5 text-xs text-muted-foreground">Дашборды {activeDataset ?? 'orders_2026'}</span>
              {DASHBOARDS.map((d) => {
                const on = d.id === dashboard.id;
                return (
                  <Link
                    key={d.id}
                    to={`/dashboards/${d.id}`}
                    aria-current={on ? 'page' : undefined}
                    className={cn(
                      'rounded-full border px-3.5 py-1 text-sm hover:no-underline',
                      on ? 'border-inverse bg-inverse font-semibold text-inverse-foreground' : 'border-border bg-transparent font-normal text-secondary-foreground',
                    )}
                  >
                    {d.title}
                  </Link>
                );
              })}
              <Link to="/chat" className="flex items-center gap-1 px-2 py-1 text-sm">
                <Icon icon={Plus} size="sm" />
                Новый
              </Link>
            </nav>
            <div className="flex flex-wrap items-end justify-between gap-4">
              <div>
                <PageTitle>Дашборд: {dashboard.title}</PageTitle>
                <div className="mt-1.5 text-muted-foreground">{DASHBOARD_META}</div>
              </div>
              <Button variant="outline" disabled={loading} className="px-4 py-1.5 disabled:opacity-60" onClick={() => { simulate('loading', 2400); }}>
                {loading ? 'Обновляется…' : 'Обновить данные'}
                <Icon icon={RefreshCw} size="sm" spin={loading} />
              </Button>
            </div>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(var(--grid-widget-min),1fr))] gap-5">
              {widgets.map((w) => (
                <WidgetCard
                  key={w.id}
                  title={w.title}
                  subtitle={w.subtitle}
                  how={w.how}
                  kpi={w.kpi}
                  chart={w.chart && <DemoChart name={w.chart} height="md" />}
                  state={widgetState(w.id)}
                  onRetry={() => { set('default'); }}
                  onRemove={() => { setRemoved((current) => new Set(current).add(w.id)); }}
                />
              ))}
            </div>
          </>
        )}
      </div>
    </PageContainer>
  );
}
