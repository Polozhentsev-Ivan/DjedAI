import type { Schemas } from '@/api/client';
import { useResultData } from '@/api/queries';
import { ErrorState, LoadingState } from '@/components/StateBlock';
import type { ChartHeight } from './Chart';
import { SpecChart } from './SpecChart';

interface ResultChartProps {
  spec: Schemas['ChartSpec'];
  height?: ChartHeight | undefined;
}

/** Loads the rows of a result and draws its ChartSpec, with the shared loading and error states. */
export function ResultChart({ spec, height = 'lg' }: ResultChartProps) {
  const data = useResultData(spec.result_id);
  if (data.isPending) return <LoadingState size="compact" title="Загружаем данные графика…" />;
  if (data.isError) {
    return <ErrorState size="compact" title="Не удалось загрузить данные графика" text={data.error.message} onAction={() => { void data.refetch(); }} />;
  }
  return <SpecChart spec={spec} data={data.data} height={height} />;
}
