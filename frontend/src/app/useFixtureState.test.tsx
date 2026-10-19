import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router';
import { useFixtureState } from './useFixtureState';

const at = (url: string) => ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={[url]}>{children}</MemoryRouter>;

describe('useFixtureState', () => {
  afterEach(() => { vi.useRealTimers(); });

  it('reads the state from the URL and ignores unknown values', () => {
    expect(renderHook(() => useFixtureState(), { wrapper: at('/x?state=error') }).result.current.state).toBe('error');
    expect(renderHook(() => useFixtureState(), { wrapper: at('/x?state=weird') }).result.current.state).toBe('default');
  });

  it('overrides and simulates a short phase', () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useFixtureState(), { wrapper: at('/x') });
    act(() => { result.current.set('error'); });
    expect(result.current.state).toBe('error');
    act(() => { result.current.simulate('loading', 1000); });
    expect(result.current.state).toBe('loading');
    act(() => { vi.advanceTimersByTime(1000); });
    expect(result.current.state).toBe('default');
  });
});
