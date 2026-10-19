import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { useDatasets } from '@/api/queries';
import { WorkspaceContext, type Workspace } from './workspaceContext';

const STORAGE_KEY = 'djedai.activeDataset';
/** Shown until authentication (its own task) provides the session user. */
const USER = 'ivan';

function stored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Dataset list from the API and the dataset the user works with, remembered per browser. */
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const query = useDatasets();
  const [chosen, setChosen] = useState<string | null>(stored);
  const setActiveDataset = useCallback((id: string) => {
    setChosen(id);
    try {
      window.localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // Storage blocked: the choice still lasts for this page.
    }
  }, []);
  const value = useMemo<Workspace>(() => {
    const datasets = query.data ?? [];
    const usable = (id: string | null) => datasets.some((d) => d.dataset_id === id && d.status === 'ready');
    const fallback = datasets.find((d) => d.status === 'ready')?.dataset_id ?? null;
    return {
      user: USER,
      datasets,
      ready: query.isSuccess || query.isError,
      activeDataset: usable(chosen) ? chosen : fallback,
      setActiveDataset,
    };
  }, [query.data, query.isSuccess, query.isError, chosen, setActiveDataset]);
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}
