import type { LucideIcon } from 'lucide-react';
import { cn } from '@/lib/cn';

export type IconSize = 'xs' | 'sm' | 'md' | 'lg' | 'xl';

const sizes: Record<IconSize, string> = {
  xs: 'size-(--size-icon-xs)',
  sm: 'size-(--size-icon-sm)',
  md: 'size-(--size-icon)',
  lg: 'size-(--size-icon-lg)',
  xl: 'size-(--size-icon-xl)',
};

interface IconProps {
  icon: LucideIcon;
  size?: IconSize | undefined;
  spin?: boolean | undefined;
  strokeWidth?: number | undefined;
  className?: string | undefined;
}

/** lucide-react glyph with the mockup stroke (1.75) and token sizes. */
export function Icon({ icon: Glyph, size = 'md', spin = false, strokeWidth = 1.75, className }: IconProps) {
  return (
    <Glyph
      aria-hidden="true"
      strokeWidth={strokeWidth}
      className={cn('flex-none', sizes[size], spin && 'animate-spin', className)}
    />
  );
}
