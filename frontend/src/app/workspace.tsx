import { useMemo, useState, type ReactNode } from 'react';
import { DATASETS, USER, type DatasetSummary } from '@/fixtures/workspace';
import { WorkspaceContext, type Workspace } from './workspaceContext';

/** Session-wide choices shared by the top bar and pages. Backed by fixtures until the API lands. */
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [datasets, setDatasets] = useState<readonly DatasetSummary[]>(DATASETS);
  const [activeDataset, setActiveDataset] = useState<string | null>(DATASETS[0]?.id ?? null);
  const value = useMemo<Workspace>(() => ({
    user: USER,
    datasets,
    activeDataset,
    setActiveDataset,
    removeDataset: (id) => {
      setDatasets((list) => list.filter((d) => d.id !== id));
      setActiveDataset((current) => (current === id ? null : current));
    },
  }), [datasets, activeDataset]);
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}
