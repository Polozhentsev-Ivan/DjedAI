import { createContext, useContext } from 'react';
import type { DatasetSummary } from '@/fixtures/workspace';

export interface Workspace {
  user: string;
  datasets: readonly DatasetSummary[];
  activeDataset: string | null;
  setActiveDataset: (id: string) => void;
  removeDataset: (id: string) => void;
}

export const WorkspaceContext = createContext<Workspace | null>(null);

export function useWorkspace(): Workspace {
  const workspace = useContext(WorkspaceContext);
  if (!workspace) throw new Error('useWorkspace вызван вне WorkspaceProvider');
  return workspace;
}
