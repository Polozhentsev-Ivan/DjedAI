import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

export interface TextPart {
  t: string;
  strong?: boolean | undefined;
  tone?: 'success' | 'danger' | undefined;
}

export interface Fact {
  label: string;
  value: string;
}

interface ChatMessageProps {
  role: 'user' | 'assistant';
  text?: string | undefined;
  parts?: readonly TextPart[] | undefined;
  facts?: readonly Fact[] | undefined;
  streaming?: boolean | undefined;
  children?: ReactNode;
}

const toneInk = { success: 'text-success', danger: 'text-destructive' };

export function ChatMessage({ role, text = '', parts, facts, streaming = false, children }: ChatMessageProps) {
  if (role === 'user') {
    return (
      <div className="flex justify-end" data-role="user">
        <div className="max-w-(--bubble-max) rounded-lg bg-surface-subtle px-4 py-2.5 text-base leading-relaxed tracking-body text-foreground">
          {text}
        </div>
      </div>
    );
  }
  const shown = parts ?? (text ? [{ t: text }] : []);
  return (
    <div className="flex items-start gap-3.5 text-base leading-relaxed tracking-body text-foreground" data-role="assistant">
      <div
        aria-hidden="true"
        className="flex size-(--size-avatar) flex-none items-center justify-center rounded-full bg-inverse text-xs font-semibold tracking-none text-inverse-foreground"
      >
        D
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-3.5">
        {shown.length > 0 && (
          <div className="text-pretty">
            {shown.map((part, index) => (
              <span
                key={index}
                className={cn(part.strong || part.tone ? 'font-semibold' : 'font-normal', part.tone && toneInk[part.tone])}
              >
                {part.t}
              </span>
            ))}
            {streaming && (
              <span
                aria-hidden="true"
                data-testid="stream-caret"
                className="ml-0.5 inline-block h-[1.1em] w-(--size-caret) animate-blink bg-foreground align-text-bottom"
              />
            )}
          </div>
        )}
        {facts && facts.length > 0 && (
          <div className="grid grid-cols-[repeat(auto-fit,minmax(var(--grid-fact-min),1fr))] gap-2.5">
            {facts.map((fact) => (
              <div key={fact.label} className="rounded-md bg-surface-subtle px-3.5 py-2.5">
                <div className="text-xs tracking-xs text-muted-foreground">{fact.label}</div>
                <div className="tabular font-semibold">{fact.value}</div>
              </div>
            ))}
          </div>
        )}
        {children}
      </div>
    </div>
  );
}
