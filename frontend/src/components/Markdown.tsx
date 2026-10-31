import type { ReactNode } from 'react';

// Agent text is untrusted: it becomes React text nodes only, never HTML.
const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\s][^*\n]*\*)/g;

function inline(text: string): ReactNode[] {
  return text.split(INLINE).filter(Boolean).map((part, index) => {
    if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) return <code key={index} className="rounded-sm bg-secondary px-1 font-mono text-xs">{part.slice(1, -1)}</code>;
    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) return <strong key={index} className="font-semibold">{part.slice(2, -2)}</strong>;
    if (part.length > 2 && part.startsWith('*') && part.endsWith('*')) return <em key={index}>{part.slice(1, -1)}</em>;
    return part;
  });
}

const BULLET = /^\s*[-*•]\s+/;
const NUMBERED = /^\s*\d+[.)]\s+/;
const HEADING = /^#{1,6}\s+/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

function tableCells(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/(?<!\\)\|$/, '').split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, '|'));
}

function table(key: number, head: string[], rows: string[][]): ReactNode {
  return (
    <div key={key} className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead><tr>{head.map((cell, index) => <th key={index} className="border-b border-border px-2 py-1 text-left font-semibold">{inline(cell)}</th>)}</tr></thead>
        <tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{head.map((_, index) => <td key={index} className="border-b border-border px-2 py-1">{inline(row[index] ?? '')}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

/** Paragraphs, lists, headings, tables, bold, italic and inline code — the subset the agent writes. */
export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  let paragraph: string[] = [];
  // The cast keeps TypeScript from narrowing to null: flush() resets it from a closure.
  let list = null as { ordered: boolean; items: string[] } | null;
  const flush = () => {
    const key = blocks.length;
    if (paragraph.length > 0) {
      blocks.push(<p key={key}>{paragraph.flatMap((line, index) => index === 0 ? inline(line) : [<br key={`br${index}`} />, ...inline(line)])}</p>);
      paragraph = [];
    }
    if (list) {
      const items = list.items.map((item, index) => <li key={index}>{inline(item)}</li>);
      blocks.push(list.ordered ? <ol key={key} className="list-decimal pl-5">{items}</ol> : <ul key={key} className="list-disc pl-5">{items}</ul>);
      list = null;
    }
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const next = lines[index + 1];
    if (line.includes('|') && next?.includes('|') && TABLE_SEPARATOR.test(next)) {
      flush();
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lines[index].includes('|')) {
        rows.push(tableCells(lines[index]));
        index += 1;
      }
      index -= 1;
      blocks.push(table(blocks.length, tableCells(line), rows));
      continue;
    }
    const ordered = NUMBERED.test(line);
    if (BULLET.test(line) || ordered) {
      if (paragraph.length > 0 || list?.ordered !== ordered) flush();
      list ??= { ordered, items: [] };
      list.items.push(line.replace(ordered ? NUMBERED : BULLET, ''));
    } else if (HEADING.test(line)) {
      flush();
      blocks.push(<p key={blocks.length} className="text-base font-semibold tracking-body">{inline(line.replace(HEADING, ''))}</p>);
    } else if (line.trim() === '') {
      flush();
    } else {
      if (list) flush();
      paragraph.push(line.trim());
    }
  }
  flush();
  return <div className="flex flex-col gap-2">{blocks}</div>;
}
