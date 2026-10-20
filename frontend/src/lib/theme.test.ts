import { act, renderHook } from '@testing-library/react';
import { getTheme, initTheme, setTheme, useTheme } from './theme';

describe('theme', () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.classList.remove('dark');
  });

  it('follows the system preference when nothing is saved', () => {
    const spy = vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true } as MediaQueryList);
    initTheme();
    expect(getTheme()).toBe('dark');
    spy.mockRestore();
  });

  it('prefers the saved choice over the system', () => {
    window.localStorage.setItem('djedai.theme', 'light');
    const spy = vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true } as MediaQueryList);
    initTheme();
    expect(getTheme()).toBe('light');
    spy.mockRestore();
  });

  it('saves the choice and notifies subscribers', () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current).toBe('light');
    act(() => { setTheme('dark'); });
    expect(result.current).toBe('dark');
    expect(document.documentElement).toHaveClass('dark');
    expect(window.localStorage.getItem('djedai.theme')).toBe('dark');
  });

  it('still applies the theme when storage is blocked', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    setTheme('dark');
    expect(getTheme()).toBe('dark');
    spy.mockRestore();
  });
});
