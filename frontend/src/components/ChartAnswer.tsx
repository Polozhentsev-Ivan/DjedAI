import { useState } from 'react';
import type { Schemas } from '@/api/client';
import { useDataset, useResult } from '@/api/queries';
import { calculation } from '@/api/view';
import { ResultChart } from '@/charts/ResultChart';
import { LinkButton } from './Button';
import { ChartCard } from './ChartCard';
import { HowCalculated } from './HowCalculated';
import { ErrorState, LoadingState } from './StateBlock';

/** «Как посчитано» of one saved result: read when the panel opens, never rebuilt from the chart. */
function ResultCalculation({ id }: { id: string }) {
  const meta = useResult(id);
  const dataset = useDataset(meta.data?.read?.dataset_id ?? null);
  if (meta.isPending) return <LoadingState size="compact" title="Загружаем расчёт…" />;
  if (meta.isError) return <ErrorState size="inline" title="Не удалось прочитать расчёт" text={meta.error.message} onAction={() => { void meta.refetch(); }} />;
  return <HowCalculated variant="panel" {...calculation(meta.data, dataset.data?.revision)} />;
}

/** Chart block of an answer with its «Как посчитано» under the chart. */
export function ChartAnswer({ chart }: { chart: Schemas['ChartSpec'] }) {
  const [open, setOpen] = useState(false);
  return (
    <ChartCard title={chart.title} subtitle={chart.subtitle ?? undefined}>
      <ResultChart spec={chart} />
      <div className="flex flex-col gap-2 border-t border-border px-1 pb-1 pt-2">
        <LinkButton className="self-start" aria-expanded={open} onClick={() => { setOpen((value) => !value); }}>
          {open ? 'Скрыть расчёт' : 'Как посчитано'}
        </LinkButton>
        {open && <ResultCalculation id={chart.result_id} />}
      </div>
    </ChartCard>
  );
}
