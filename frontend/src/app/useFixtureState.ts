import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';

export type PageState = 'default' | 'loading' | 'empty' | 'error';

const STATES: readonly PageState[] = ['default', 'loading', 'empty', 'error'];

function parse(value: string | null): PageState {
  return STATES.find((state) => state === value) ?? 'default';
}

/**
 * Screen state for fixture mode. `?state=` opens any frame of docs/ui-design/Frames.dc.html
 * by URL; `simulate` plays a short loading phase the way the prototype does. Replaced by real
 * request state when screens connect to the API.
 */
export function useFixtureState() {
  const [params] = useSearchParams();
  const fromUrl = parse(params.get('state'));
  // An override belongs to the URL state it was made on; changing ?state= drops it.
  const [override, setOverride] = useState<{ base: PageState; state: PageState } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => { clearTimeout(timer.current); }, []);

  const set = useCallback((state: PageState) => {
    clearTimeout(timer.current);
    setOverride({ base: fromUrl, state });
  }, [fromUrl]);

  const simulate = useCallback((state: PageState, ms: number, then: PageState = 'default') => {
    clearTimeout(timer.current);
    setOverride({ base: fromUrl, state });
    timer.current = setTimeout(() => { setOverride({ base: fromUrl, state: then }); }, ms);
  }, [fromUrl]);

  const current = override?.base === fromUrl ? override.state : fromUrl;
  return { state: current, set, simulate };
}
