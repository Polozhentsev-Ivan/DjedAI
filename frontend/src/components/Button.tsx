import type { ButtonHTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

export type ButtonVariant = 'primary' | 'outline' | 'inverse' | 'danger-outline' | 'link';

const variants: Record<ButtonVariant, string> = {
  primary: 'bg-primary text-primary-foreground',
  outline: 'border border-link bg-transparent text-link',
  inverse: 'bg-inverse text-inverse-foreground',
  'danger-outline': 'border border-destructive bg-transparent text-destructive',
  link: 'bg-transparent text-link',
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant | undefined;
}

/** Pill button from the mockup; padding is set by the caller because it differs per place. */
export function Button({ variant = 'primary', className, type = 'button', ...rest }: ButtonProps) {
  return (
    <button
      type={type}
      className={cn(
        'press inline-flex items-center justify-center gap-1.5 rounded-full text-sm disabled:cursor-not-allowed',
        variants[variant],
        className,
      )}
      {...rest}
    />
  );
}

interface LinkButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  tone?: 'link' | 'danger' | 'muted' | undefined;
}

const linkTones = { link: 'text-link', danger: 'text-destructive', muted: 'text-muted-foreground' };

/** Text action that looks like a link but is a button, so it is reachable from the keyboard. */
export function LinkButton({ tone = 'link', className, type = 'button', ...rest }: LinkButtonProps) {
  return <button type={type} className={cn('inline-flex items-center gap-1 leading-[inherit] hover:underline', linkTones[tone], className)} {...rest} />;
}
