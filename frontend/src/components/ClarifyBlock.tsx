import { CircleQuestionMark } from 'lucide-react';
import { Icon } from './Icon';

export interface ClarifyOption {
  label: string;
  name: string;
  hint?: string | undefined;
}

interface ClarifyBlockProps {
  question: string;
  options: readonly ClarifyOption[];
  onSelect: (name: string) => void;
  hint?: string | undefined;
}

/** Choice offered instead of an error when the question is ambiguous. */
export function ClarifyBlock({ question, hint, options, onSelect }: ClarifyBlockProps) {
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border bg-card px-5 py-4 text-sm leading-normal tracking-sm text-foreground">
      <div className="flex items-start gap-2.5">
        <span className="flex pt-0.5 text-link">
          <Icon icon={CircleQuestionMark} size="md" />
        </span>
        <div>
          <div className="text-base font-semibold tracking-body">{question}</div>
          {hint && <div className="text-secondary-foreground">{hint}</div>}
        </div>
      </div>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(var(--grid-option-min),1fr))] gap-2">
        {options.map((option) => (
          <button
            key={option.name}
            type="button"
            onClick={() => { onSelect(option.name); }}
            className="press flex flex-col items-start gap-0.5 rounded-md border border-border bg-secondary px-3.5 py-2.5 text-left text-foreground hover:border-link"
          >
            <span className="text-sm font-semibold">{option.label}</span>
            <span className="text-xs text-muted-foreground">
              <span className="font-mono">{option.name}</span>
              {option.hint && ` · ${option.hint}`}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
