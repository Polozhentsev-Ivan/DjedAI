import { Check, Circle, LoaderCircle } from 'lucide-react';
import { cn } from '@/lib/cn';
import { Icon } from './Icon';

export type StepStatus = 'done' | 'active' | 'pending';

export interface Step {
  label: string;
  status: StepStatus;
  /** Progress of the step in percent. */
  pct?: number | undefined;
}

interface AgentStepsProps {
  steps: readonly Step[];
  title?: string | undefined;
  framed?: boolean | undefined;
}

const icons = { done: Check, active: LoaderCircle, pending: Circle };
const iconInk = { done: 'text-success', active: 'text-primary', pending: 'text-muted-foreground' };

export function AgentSteps({ steps, title, framed = true }: AgentStepsProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'flex w-full flex-col gap-2 rounded-md text-sm leading-normal tracking-sm text-foreground',
        framed ? 'border border-border bg-secondary px-4 py-3.5' : 'p-0',
      )}
    >
      {framed && title && (
        <div className="flex items-center gap-2 font-semibold">
          <Icon icon={LoaderCircle} size="sm" spin className="text-primary" />
          <span>{title}</span>
        </div>
      )}
      {steps.map((step) => (
        <div key={step.label} className="flex flex-col gap-1.5">
          <div className={cn('flex items-center gap-2', step.status === 'pending' ? 'text-muted-foreground' : 'text-foreground')}>
            <Icon icon={icons[step.status]} size="sm" spin={step.status === 'active'} className={iconInk[step.status]} />
            <span>{step.label}</span>
            {step.pct !== undefined && <span className="tabular ml-auto text-secondary-foreground">{step.pct}%</span>}
          </div>
          {step.pct !== undefined && (
            <div
              role="progressbar"
              aria-label={step.label}
              aria-valuenow={step.pct}
              aria-valuemin={0}
              aria-valuemax={100}
              className="ml-6 h-(--size-progress) overflow-hidden rounded-full bg-muted"
            >
              <div className="h-full rounded-full bg-primary" style={{ width: `${step.pct}%` }} />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
