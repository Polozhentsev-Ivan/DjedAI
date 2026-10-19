import { Info, TriangleAlert } from 'lucide-react';
import { cn } from '@/lib/cn';
import { Icon } from './Icon';

export type LogLevel = 'info' | 'warning';

interface TransformLogItemProps {
  level: LogLevel;
  text: string;
}

export function TransformLogItem({ level, text }: TransformLogItemProps) {
  const warn = level === 'warning';
  return (
    <div
      className={cn(
        'flex gap-3 border-b border-border-subtle px-5 py-3 text-sm leading-normal tracking-sm text-foreground',
        warn ? 'bg-warning-muted' : 'bg-transparent',
      )}
    >
      <span className={cn('flex size-(--size-log-icon) flex-none items-center justify-center', warn ? 'text-warning' : 'text-link')}>
        <Icon icon={warn ? TriangleAlert : Info} size="md" strokeWidth={2} />
      </span>
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className={cn('text-xs font-semibold', warn ? 'text-warning' : 'text-muted-foreground')}>
          {warn ? 'Предупреждение' : 'Информация'}
        </span>
        <span className="text-pretty">{text}</span>
      </div>
    </div>
  );
}
