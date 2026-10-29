import { ChevronRight, LayoutDashboard } from 'lucide-react';
import { Link } from 'react-router';
import type { Schemas } from '@/api/client';
import { useResultData } from '@/api/queries';
import { ResultTable } from '@/charts/ResultTable';
import { plural } from '@/lib/format';
import { ChartAnswer } from './ChartAnswer';
import { ChartCard } from './ChartCard';
import { ClarifyBlock } from './ClarifyBlock';
import { Icon } from './Icon';
import { Markdown } from './Markdown';
import type { SaveTarget } from './SaveToDashboard';
import { ErrorState, LoadingState, StateBlock } from './StateBlock';
import { SuggestionChips } from './SuggestionChips';

type Block = Schemas['Block'];

const DEFAULT_TABLE_LIMIT = 20;

/** Rows of a saved result, projected to the block's columns and cut to its limit; numbers come from the result only. */
function TableBlock({ block }: { block: Schemas['TableBlock'] }) {
  const data = useResultData(block.result_id);
  const limit = block.limit ?? DEFAULT_TABLE_LIMIT;
  const title = 'Таблица результата';
  if (data.isPending) return <LoadingState size="compact" title="Загружаем таблицу…" />;
  if (data.isError) return <ErrorState size="compact" title="Не удалось загрузить таблицу" text={data.error.message} onAction={() => { void data.refetch(); }} />;
  const rows = data.data.rows ?? [];
  const shown = { ...data.data, rows: rows.slice(0, limit) };
  const total = data.data.row_count ?? rows.length;
  return (
    <ChartCard title={title} subtitle={`${block.result_id} · ${total.toLocaleString('ru-RU')} ${plural(total, ['строка', 'строки', 'строк'])}`}>
      <div className="pb-2 pt-3">
        {rows.length === 0 ? <p className="px-1 text-muted-foreground">Результат не содержит строк</p>
          : <ResultTable spec={{ chart_id: block.result_id, type: 'table', title }} data={shown} fields={block.columns ?? []} />}
        {(total > shown.rows.length || data.data.truncated) && rows.length > 0 && (
          <p className="px-1 pt-2 text-xs text-muted-foreground">Показаны первые {shown.rows.length} из {total.toLocaleString('ru-RU')} строк.</p>
        )}
      </div>
    </ChartCard>
  );
}

function DashboardLink({ block }: { block: Schemas['DashboardLinkBlock'] }) {
  return (
    <Link to={`/dashboards/${encodeURIComponent(block.dashboard_id)}`}
      className="press flex items-center gap-3 rounded-lg border border-border bg-card px-4 py-3 text-foreground no-underline hover:border-link">
      <span className="flex text-link"><Icon icon={LayoutDashboard} size="md" /></span>
      <span className="min-w-0 flex-1">
        <span className="block font-semibold">{block.title}</span>
        <span className="block text-xs text-muted-foreground">Открыть на экране дашбордов</span>
      </span>
      <Icon icon={ChevronRight} size="sm" className="text-muted-foreground" />
    </Link>
  );
}

/** A block this build cannot draw: say so instead of breaking the answer or silently dropping it. */
function Unsupported({ type }: { type: string }) {
  return <StateBlock kind="empty" size="inline" title="Этот тип ответа пока не поддерживается" text={`Блок «${type}» не отображается в этой версии интерфейса. Остальная часть ответа показана полностью.`} />;
}

/** The wire may carry a block newer than these types (or a broken one), so read its type defensively. */
function typeName(block: unknown): string {
  const type = (block as { type?: unknown } | null)?.type;
  return typeof type === 'string' && type ? type : 'без типа';
}

interface ChatBlocksProps {
  blocks: readonly Block[];
  /** Sends a clarification option or a suggestion as the next question. */
  onAsk: (text: string) => void;
  /** Buttons stay visible but inactive while a stream runs or when the answer is not the latest. */
  disabled: boolean;
  /** Charts offer «+ В дашборд» only when the chat's dataset is known. */
  save?: SaveTarget | undefined;
}

export function ChatBlocks({ blocks, onAsk, disabled, save }: ChatBlocksProps) {
  return blocks.map((block, index) => {
    const key = `${index}:${typeName(block)}`;
    switch (block.type) {
      case 'text':
        return typeof block.markdown === 'string' ? <Markdown key={key} text={block.markdown} /> : <Unsupported key={key} type="text" />;
      case 'chart':
        if (typeof block.chart !== 'object' || typeof block.chart.result_id !== 'string') return <Unsupported key={key} type="chart" />;
        return <ChartAnswer key={key} chart={block.chart} save={save} />;
      case 'table':
        if (typeof block.result_id !== 'string') return <Unsupported key={key} type="table" />;
        return <TableBlock key={key} block={block} />;
      case 'clarification':
        if (typeof block.question !== 'string') return <Unsupported key={key} type="clarification" />;
        if (!Array.isArray(block.options) || block.options.length === 0) return <Markdown key={key} text={block.question} />;
        return <ClarifyBlock key={key} question={block.question} disabled={disabled} onSelect={onAsk}
          options={block.options.map((option) => ({ label: option, name: option }))} />;
      case 'dashboard_link':
        if (typeof block.dashboard_id !== 'string') return <Unsupported key={key} type="dashboard_link" />;
        return <DashboardLink key={key} block={block} />;
      case 'suggestions':
        return block.items?.length ? <SuggestionChips key={key} items={block.items} disabled={disabled} onSelect={onAsk} /> : null;
      default: {
        const unknown: never = block;
        return <Unsupported key={key} type={typeName(unknown)} />;
      }
    }
  });
}
