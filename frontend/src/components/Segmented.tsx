import { cn } from '@/lib/cn';

interface SegmentedProps<T extends string> {
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  stretch?: boolean | undefined;
  itemClassName?: string | undefined;
}

/** Pill switch on a muted track: login tabs and database type. */
export function Segmented<T extends string>({ options, value, onChange, label, stretch = false, itemClassName }: SegmentedProps<T>) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cn('rounded-full bg-muted p-0.5', stretch ? 'grid grid-cols-2' : 'inline-flex self-start')}
    >
      {options.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => { onChange(option.value); }}
            className={cn(
              'rounded-full text-sm',
              on ? 'bg-card font-semibold text-foreground' : 'bg-transparent text-muted-foreground',
              itemClassName,
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
