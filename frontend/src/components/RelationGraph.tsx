import { useId, type KeyboardEvent } from 'react';
import type { RelationGraph as Graph } from '@/api/view';
import { cn } from '@/lib/cn';

interface RelationGraphProps {
  graph: Graph;
  selected: string;
  onSelect: (tableId: string) => void;
}

/** Smallest zoom of the graph: below it the 11px labels become unreadable. */
const MIN_SCALE = 0.85;

/** Longest table id that fits a node box at the graph font size. */
const MAX_LABEL = 14;

/** Backing behind an edge label: 11px mono glyphs are ~6.6 wide, plus padding. */
const labelWidth = (label: string) => label.length * 6.6 + 8;

const short = (id: string) => (id.length > MAX_LABEL ? `${id.slice(0, MAX_LABEL - 1)}…` : id);

/** Tables and their computed relations; arrows point from the many side to the unique key. */
export function RelationGraph({ graph, selected, onSelect }: RelationGraphProps) {
  const arrow = useId();
  const { width: w, height: h } = graph.node;
  const keyboard = (id: string) => (event: KeyboardEvent) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    onSelect(id);
  };
  return (
    <svg
      role="group"
      aria-label="Граф связей таблиц"
      viewBox={`0 0 ${graph.width} ${graph.height}`}
      // Small graphs fit the card; a large one keeps readable text and scrolls inside its container.
      style={{ minWidth: Math.round(graph.width * MIN_SCALE), maxWidth: Math.max(480, graph.width) }}
      className="mx-auto block h-auto w-full text-muted-foreground"
    >
      <defs>
        <marker id={arrow} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" className="fill-current" />
        </marker>
      </defs>
      {graph.edges.map((edge) => (
        <g key={edge.key} role="img" aria-label={edge.title}>
          <title>{edge.title}</title>
          <path
            d={edge.path}
            fill="none"
            className="stroke-current"
            strokeWidth={1.5}
            strokeDasharray={edge.weak ? '4 3' : undefined}
            markerEnd={edge.directed ? `url(#${arrow})` : undefined}
          />
          {edge.label && (
            <rect
              x={edge.labelX - labelWidth(edge.label) / 2}
              y={edge.labelY - 8}
              width={labelWidth(edge.label)}
              height={16}
              rx={4}
              className="fill-card"
            />
          )}
          {edge.label && (
            <text
              x={edge.labelX}
              y={edge.labelY}
              textAnchor="middle"
              dominantBaseline="middle"
              className="fill-secondary-foreground font-mono text-[11px] font-semibold"
            >
              {edge.label}
            </text>
          )}
        </g>
      ))}
      {graph.nodes.map((node) => {
        const on = node.id === selected;
        return (
          <g
            key={node.id}
            role="button"
            tabIndex={0}
            aria-label={`Таблица ${node.id}, ${node.rows}`}
            aria-pressed={on}
            onClick={() => { onSelect(node.id); }}
            onKeyDown={keyboard(node.id)}
            className="cursor-pointer outline-none [&:focus-visible>rect]:stroke-primary"
          >
            <title>{node.id}</title>
            <rect
              x={node.x - w / 2}
              y={node.y - h / 2}
              width={w}
              height={h}
              rx={8}
              strokeWidth={on ? 2 : 1}
              className={cn(on ? 'fill-inverse stroke-inverse' : 'fill-card stroke-border')}
            />
            <text x={node.x} y={node.y - 4} textAnchor="middle" className={cn('font-mono text-[13px] font-semibold', on ? 'fill-inverse-foreground' : 'fill-foreground')}>
              {short(node.id)}
            </text>
            <text x={node.x} y={node.y + 13} textAnchor="middle" className={cn('text-[11px]', on ? 'fill-inverse-foreground' : 'fill-muted-foreground')}>
              {node.rows}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
