import { cn } from '@/lib/cn';

export type BadgeTone = 'success' | 'info' | 'danger' | 'warning' | 'neutral' | 'accent';
export type BadgeVariant = 'status' | 'tag' | 'type';

const tones: Record<BadgeTone, string> = {
  success: 'bg-success-muted text-success',
  info: 'bg-info-muted text-link',
  danger: 'bg-destructive-muted text-destructive',
  warning: 'bg-warning-muted text-warning',
  neutral: 'bg-muted text-secondary-foreground',
  accent: 'bg-accent text-accent-foreground',
};

interface StatusBadgeProps {
  label: string;
  tone?: BadgeTone | undefined;
  variant?: BadgeVariant | undefined;
  dot?: boolean | undefined;
}

export function StatusBadge({ label, tone = 'neutral', variant = 'status', dot = true }: StatusBadgeProps) {
  const status = variant === 'status';
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 whitespace-nowrap py-0.5 text-xs leading-normal tracking-none',
        tones[tone],
        status ? 'rounded-full px-2.5 font-semibold' : 'px-2 font-normal',
        variant === 'tag' && 'rounded-full',
        variant === 'type' && 'rounded-xs font-mono',
      )}
    >
      {status && dot && <span aria-hidden="true" className="size-(--size-dot) rounded-full bg-current" />}
      {label}
    </span>
  );
}
