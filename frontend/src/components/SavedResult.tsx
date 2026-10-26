import { apiUrl } from '@/api/client';
import { useDataset, useResult, useResultData } from '@/api/queries';
import { calculation, formatMeasure } from '@/api/view';
import { HowCalculated } from './HowCalculated';
import { ErrorState, LoadingState } from './StateBlock';

/** The saved result, rather than reconstructed numbers from the assistant's prose. */
export function SavedResult({ id }: { id: string }) {
  const meta = useResult(id);
  const data = useResultData(id);
  // The current revision tells a result computed on an older catalog apart (B1-11).
  const dataset = useDataset(meta.data?.read?.dataset_id ?? null);
  return (
    <details className="rounded-md border border-border text-sm" data-result-id={id}>
      <summary className="cursor-pointer px-3.5 py-2.5 font-semibold">Результат расчёта {id}</summary>
      <div className="flex flex-col gap-3 border-t border-border p-3.5">
        <div className="flex flex-wrap gap-4">
          <a href={apiUrl(`/results/${encodeURIComponent(id)}`)} target="_blank" rel="noreferrer">Метаданные JSON</a>
          <a href={apiUrl(`/results/${encodeURIComponent(id)}/data`)} target="_blank" rel="noreferrer">Данные JSON</a>
        </div>
        {meta.isPending && <LoadingState size="compact" title="Загружаем сохранённый результат…" />}
        {meta.isError && <ErrorState size="inline" title="Не удалось прочитать результат" text={meta.error.message} onAction={() => { void meta.refetch(); }} />}
        {meta.data && <>
          <HowCalculated variant="panel" {...calculation(meta.data, dataset.data?.revision)} />
          {meta.data.source_result_id && <div>Исходный результат: <a href={apiUrl(`/results/${encodeURIComponent(meta.data.source_result_id)}`)}>{meta.data.source_result_id}</a></div>}
          {meta.data.error && <ErrorState size="inline" title={meta.data.error.message} text={meta.data.error.suggestion ?? undefined} actionLabel="" />}
        </>}
        {data.isPending && <LoadingState size="compact" title="Загружаем данные результата…" />}
        {data.isError && <ErrorState size="inline" title="Не удалось прочитать данные результата" text={data.error.message} onAction={() => { void data.refetch(); }} />}
        {data.data && <div className="overflow-auto">
          <table aria-label={`Данные результата ${id}`} className="w-full border-collapse text-left">
            <thead><tr>{(data.data.columns ?? []).map((column) => <th key={typeof column === 'string' ? column : column.name} className="border-b border-border px-3 py-2">{typeof column === 'string' ? column : column.label ?? column.name}</th>)}</tr></thead>
            <tbody>{(data.data.rows ?? []).slice(0, 20).map((row, index) => <tr key={index}>{row.map((value, cell) => <td key={cell} className="border-b border-border px-3 py-2">{formatMeasure(value, undefined)}</td>)}</tr>)}</tbody>
          </table>
          {(data.data.rows ?? []).length === 0 && <p className="text-muted-foreground">Результат не содержит строк</p>}
          {((data.data.rows ?? []).length > 20 || data.data.truncated) && <p className="text-muted-foreground">Показаны первые 20 строк сохранённого результата. Полная доступная выборка — по ссылке «Данные JSON».</p>}
        </div>}
      </div>
    </details>
  );
}
