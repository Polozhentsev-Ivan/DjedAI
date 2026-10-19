import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { useCompactLayout } from '@/lib/useMediaQuery';

/** Scrolling content column of every screen except the chat. */
export function PageContainer({ children }: { children: ReactNode }) {
  const compact = useCompactLayout();
  return (
    <div className="flex-1 overflow-auto">
      <div className={cn('mx-auto box-content max-w-(--content-max)', compact ? 'px-5 pb-10 pt-6' : 'px-10 pb-12 pt-8')}>{children}</div>
    </div>
  );
}

export function PageTitle({ children }: { children: ReactNode }) {
  return <h1 className="m-0 font-display text-xl font-semibold leading-tight tracking-display">{children}</h1>;
}
