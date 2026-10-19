import { useSyncExternalStore } from 'react';

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = window.matchMedia(query);
      list.addEventListener('change', onChange);
      return () => { list.removeEventListener('change', onChange); };
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}

/** Tablet layout from the mockup: compact sidebar below 1024px. */
export function useCompactLayout(): boolean {
  return useMediaQuery('(max-width: 1023px)');
}
