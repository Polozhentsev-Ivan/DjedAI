import { Inbox, LoaderCircle, TriangleAlert } from 'lucide-react';
import { cn } from '@/lib/cn';
import { AgentSteps, type Step } from './AgentSteps';
import { Button, LinkButton } from './Button';
import { Icon } from './Icon';

export type StateKind = 'loading' | 'empty' | 'error';
export type StateSize = 'page' | 'inline' | 'compact';

export interface StateBlockProps {
  kind: StateKind;
  title: string;
  size?: StateSize | undefined;
  text?: string | undefined;
  steps?: readonly Step[] | undefined;
  actionLabel?: string | undefined;
  onAction?: (() => void) | undefined;
  secondaryLabel?: string | undefined;
  onSecondary?: (() => void) | undefined;
}

const layout: Record<StateSize, string> = {
  page: 'flex-col items-center text-center px-6 py-14 rounded-lg',
  inline: 'flex-row items-start text-left px-4 py-3.5 rounded-md border',
  compact: 'flex-col items-center text-center p-4 rounded-md',
};
const badge: Record<StateSize, string> = { page: 'size-(--size-state-lg)', inline: 'size-(--size-state-sm)', compact: 'size-(--size-state-md)' };
const glyph = { page: 'xl', inline: 'sm', compact: 'md' } as const;
const titleSize: Record<StateSize, string> = { page: 'text-lg', inline: 'text-sm', compact: 'text-sm' };
const icons = { loading: LoaderCircle, empty: Inbox, error: TriangleAlert };

/** Shared look of loading, empty and error states; only the copy differs between screens. */
export function StateBlock({ kind, title, size = 'page', text, steps, actionLabel, onAction, secondaryLabel, onSecondary }: StateBlockProps) {
  const error = kind === 'error';
  const inline = size === 'inline';
  const primaryAction = !(inline && error);
  const centered = size !== 'inline';
  return (
    <div
      role={error ? 'alert' : 'status'}
      className={cn(
        'box-border flex w-full gap-3 text-sm leading-normal tracking-sm text-foreground',
        layout[size],
        inline && (error ? 'border-destructive-border bg-destructive-muted' : 'border-border bg-secondary'),
      )}
    >
      <div
        className={cn(
          'flex flex-none items-center justify-center rounded-full',
          badge[size],
          error ? 'text-destructive' : kind === 'loading' ? 'text-primary' : 'text-muted-foreground',
          error ? (inline ? 'bg-card' : 'bg-destructive-muted') : kind === 'loading' ? 'bg-info-muted' : 'bg-surface-subtle',
        )}
      >
        <Icon icon={icons[kind]} size={glyph[size]} spin={kind === 'loading'} />
      </div>
      <div className={cn('flex min-w-0 flex-1 flex-col gap-1.5', centered ? 'items-center' : 'items-start')}>
        <div className={cn('font-semibold leading-snug', titleSize[size], error && inline ? 'text-destructive' : 'text-foreground')}>{title}</div>
        {text && <div className="max-w-(--text-max) text-pretty text-secondary-foreground">{text}</div>}
        {steps && steps.length > 0 && (
          <div className="mt-1 w-full max-w-(--text-max)">
            <AgentSteps framed={false} steps={steps} />
          </div>
        )}
        {(actionLabel ?? secondaryLabel) && (
          <div className={cn('mt-1.5 flex flex-wrap gap-2', centered ? 'justify-center' : 'justify-start')}>
            {actionLabel && (
              <Button variant={primaryAction ? 'primary' : 'danger-outline'} className="px-4 py-1.5" onClick={onAction}>
                {actionLabel}
              </Button>
            )}
            {secondaryLabel && (
              <LinkButton className="px-2 py-1.5 text-sm hover:no-underline" onClick={onSecondary}>
                {secondaryLabel}
              </LinkButton>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

type StateProps = Omit<StateBlockProps, 'kind' | 'title'> & { title?: string | undefined };

export function LoadingState({ title = 'Загрузка…', ...rest }: StateProps) {
  return <StateBlock kind="loading" title={title} {...rest} />;
}

export function EmptyState({ title = 'Нет данных', ...rest }: StateProps) {
  return <StateBlock kind="empty" title={title} {...rest} />;
}

export function ErrorState({ title = 'Что-то пошло не так', actionLabel = 'Повторить', ...rest }: StateProps) {
  return <StateBlock kind="error" title={title} actionLabel={actionLabel} {...rest} />;
}
