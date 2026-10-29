import { Plus, RefreshCw } from 'lucide-react';
import { useEffect } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import type { Schemas } from '@/api/client';
import { useDashboard, useDashboards, useRefreshDashboard, useRemoveWidget, useResult, useResultData } from '@/api/queries';
import { calculation, callParams, dashboardMeta, kpiView, widgetRecipe } from '@/api/view';
import { PageContainer, PageTitle } from '@/app/PageContainer';
import { useWorkspace } from '@/app/workspaceContext';
import { SpecChart } from '@/charts/SpecChart';
import { Button } from '@/components/Button';
import { Icon } from '@/components/Icon';
import { EmptyState, ErrorState, LoadingState } from '@/components/StateBlock';
import { WidgetCard, type WidgetState } from '@/components/WidgetCard';
import { cn } from '@/lib/cn';

interface WidgetProps {
  widget: Schemas['Widget'];
  refreshing: boolean;
  onRefresh: () => void;
  onRemove: () => void;
}

/** One widget: its result metadata feeds «Как посчитано», its rows feed the chart or KPI. */
function DashboardWidget({ widget, refreshing, onRefresh, onRemove }: WidgetProps) {
  const result = useResult(widget.result_id);
  const data = useResultData(widget.result_id);
  const failed = widget.status === 'error' || widget.result_id === null || result.isError || data.isError;
  const state: WidgetState = refreshing || (!failed && (result.isPending || data.isPending)) ? 'loading' : failed ? 'error' : 'default';
  const error = widget.error ?? (result.isError ? result.error : data.isError ? data.error : null);
  const call = widget.tool_call ?? widget.recipe?.steps.find((step) => step.step_id === widget.recipe?.output_step_id)?.call;
  const how = { ...result.data ? calculation(result.data) : { tool: call?.tool ?? 'Вычисление виджета', params: callParams(call?.params ?? {}), columns: [], sql: '—' }, recipe: widgetRecipe(widget) };
  const kpi = widget.chart.type === 'kpi' && data.data ? kpiView(widget.chart, data.data, result.data) : null;
  return (
    <WidgetCard
      title={widget.chart.title}
      subtitle={widget.caption ?? widget.chart.subtitle ?? ''}
      how={how}
      kpi={kpi ?? undefined}
      chart={data.data && widget.chart.type !== 'kpi' && <SpecChart spec={widget.chart} data={data.data} height="md" />}
      state={state}
      errorTitle={error?.message ?? 'Ошибка обновления данных'}
      errorText={error?.suggestion ?? undefined}
      onRetry={onRefresh}
      onRemove={onRemove}
    />
  );
}

function DashboardView({ id }: { id: string }) {
  const dashboard = useDashboard(id);
  const { setActiveDataset } = useWorkspace();
  const refresh = useRefreshDashboard(id);
  const remove = useRemoveWidget(id);
  const datasetId = dashboard.data?.dataset_id;
  useEffect(() => {
    if (datasetId) setActiveDataset(datasetId);
  }, [datasetId, setActiveDataset]);

  if (dashboard.isPending) return <LoadingState title="Загружаем дашборд…" />;
  if (dashboard.isError) {
    return (
      <div className="rounded-lg border border-border bg-card">
        <ErrorState title="Не удалось открыть дашборд" text={dashboard.error.message} onAction={() => { void dashboard.refetch(); }} />
      </div>
    );
  }
  const d = dashboard.data;
  return (
    <>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <PageTitle>Дашборд: {d.title}</PageTitle>
          <div className="mt-1.5 text-muted-foreground">{dashboardMeta(d)}</div>
        </div>
        <Button variant="outline" disabled={refresh.isPending} className="px-4 py-1.5 disabled:opacity-60" onClick={() => { refresh.mutate(); }}>
          {refresh.isPending ? 'Обновляется…' : 'Обновить данные'}
          <Icon icon={RefreshCw} size="sm" spin={refresh.isPending} />
        </Button>
      </div>
      {refresh.isError && <ErrorState size="inline" title="Не удалось обновить дашборд" text={refresh.error.message} onAction={() => { refresh.mutate(); }} />}
      {d.widgets.length === 0 ? (
        <div className="rounded-lg border border-border bg-card">
          <EmptyState title="На дашборде нет виджетов" text="Создайте график в чате и нажмите «Добавить на дашборд»." />
        </div>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(var(--grid-widget-min),1fr))] gap-5">
          {d.widgets.map((w) => (
            <DashboardWidget
              key={w.widget_id}
              widget={w}
              refreshing={refresh.isPending}
              onRefresh={() => { refresh.mutate(); }}
              onRemove={() => { remove.mutate(w.widget_id); }}
            />
          ))}
        </div>
      )}
    </>
  );
}

export function DashboardsPage() {
  const navigate = useNavigate();
  const { dashboardId } = useParams();
  const { ready, activeDataset, datasets } = useWorkspace();
  const list = useDashboards(activeDataset);
  const datasetName = datasets.find((d) => d.dataset_id === activeDataset)?.name ?? '';
  const dashboards = list.data ?? [];
  const selected = dashboardId ?? dashboards[0]?.dashboard_id ?? null;
  const toChat = () => { void navigate('/chat'); };
  const empty = !dashboardId && ready && (activeDataset === null || (list.isSuccess && dashboards.length === 0));

  return (
    <PageContainer>
      <div className="flex flex-col gap-5 text-sm leading-normal tracking-sm">
        {empty && (
          <>
            <PageTitle>Дашборды</PageTitle>
            <div className="rounded-lg border border-border bg-card">
              <EmptyState title="У вас ещё нет дашбордов" text="Создайте график в чате и нажмите «Добавить на дашборд»." actionLabel="Перейти в чат" onAction={toChat} />
            </div>
          </>
        )}
        {!dashboardId && !empty && (list.isPending || !ready) && <LoadingState title="Загружаем дашборды…" />}
        {!dashboardId && list.isError && (
          <div className="rounded-lg border border-border bg-card">
            <ErrorState title="Не удалось получить список дашбордов" text={list.error.message} onAction={() => { void list.refetch(); }} />
          </div>
        )}
        {!empty && selected && (
          <>
            <nav aria-label="Дашборды датасета" className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1.5 text-xs text-muted-foreground">Дашборды {datasetName}</span>
              {dashboards.map((d) => {
                const on = d.dashboard_id === selected;
                return (
                  <Link
                    key={d.dashboard_id}
                    to={`/dashboards/${d.dashboard_id}`}
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
            <DashboardView key={selected} id={selected} />
          </>
        )}
      </div>
    </PageContainer>
  );
}
