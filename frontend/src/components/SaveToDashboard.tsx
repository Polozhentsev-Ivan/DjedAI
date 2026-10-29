import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { LayoutDashboard, Plus } from 'lucide-react';
import { useState } from 'react';
import type { Schemas } from '@/api/client';
import { useDashboards, useSaveToDashboard } from '@/api/queries';
import { Icon } from './Icon';

/** Where a chart of an answer is saved: the chat's dataset, and who is told about the saved dashboard. */
export interface SaveTarget {
  datasetId: string;
  onSaved: (dashboard: Schemas['Dashboard']) => void;
}

const item = 'flex cursor-pointer items-center gap-2 rounded-sm px-2.5 py-2 text-left outline-none data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50 data-[highlighted]:bg-accent';

/**
 * «+ В дашборд»: the chart goes to an existing dashboard of the dataset or to a new one.
 * Only the result id and the chart are sent; the server rebuilds the reproducible recipe from the result.
 */
export function SaveToDashboard({ chart, datasetId, onSaved }: SaveTarget & { chart: Schemas['ChartSpec'] }) {
  const [open, setOpen] = useState(false);
  // The list is read when the menu opens, so an answer with several charts does not fetch it per chart.
  const dashboards = useDashboards(open ? datasetId : null);
  const save = useSaveToDashboard();
  const add = (dashboardId?: string) => {
    save.mutate({ dataset_id: datasetId, result_id: chart.result_id, chart, ...(dashboardId ? { dashboard_id: dashboardId } : {}) }, { onSuccess: onSaved });
  };
  return (
    <div className="flex flex-col items-end gap-1">
      <DropdownMenu.Root open={open} onOpenChange={setOpen}>
        <DropdownMenu.Trigger disabled={save.isPending}
          className="press inline-flex items-center justify-center gap-1 rounded-full bg-primary px-3.5 py-1.5 text-sm text-primary-foreground disabled:cursor-not-allowed disabled:opacity-70">
          <Icon icon={Plus} size="sm" />
          {save.isPending ? 'Сохраняем…' : 'В дашборд'}
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content align="end" sideOffset={6}
            className="z-40 flex w-(--popover-width) flex-col gap-0.5 rounded-md border border-border bg-popover p-1.5 font-sans text-sm text-popover-foreground shadow-(--shadow-popover)">
            <DropdownMenu.Label className="px-2.5 py-1 text-xs text-muted-foreground">Добавить на дашборд</DropdownMenu.Label>
            {dashboards.isPending && <DropdownMenu.Item disabled className={item}>Загружаем дашборды…</DropdownMenu.Item>}
            {dashboards.isError && <DropdownMenu.Item disabled className={item}>Не удалось загрузить дашборды</DropdownMenu.Item>}
            {dashboards.data?.map((dashboard) => (
              <DropdownMenu.Item key={dashboard.dashboard_id} className={item} onSelect={() => { add(dashboard.dashboard_id); }}>
                <Icon icon={LayoutDashboard} size="sm" className="text-muted-foreground" />
                {dashboard.title}
              </DropdownMenu.Item>
            ))}
            <DropdownMenu.Separator className="my-1 h-px bg-border-subtle" />
            <DropdownMenu.Item className={`${item} text-link`} onSelect={() => { add(); }}>
              <Icon icon={Plus} size="sm" />
              Новый дашборд
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
      {save.isError && <p role="alert" className="m-0 text-xs text-destructive">Не удалось сохранить: {save.error.message}</p>}
    </div>
  );
}
