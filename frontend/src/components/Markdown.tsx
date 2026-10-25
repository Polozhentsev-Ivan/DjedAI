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

/** Paragraphs, lists, headings, bold, italic and inline code — the subset the agent writes. */
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
  for (const line of lines) {
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
