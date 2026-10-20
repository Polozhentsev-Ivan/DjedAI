import { cn } from './cn';

describe('cn', () => {
  it('keeps font size and color utilities together', () => {
    expect(cn('text-xs text-muted-foreground', 'text-link')).toBe('text-xs text-link');
  });

  it('lets later padding override earlier', () => {
    expect(cn('px-4', false, 'px-5')).toBe('px-5');
  });
});
