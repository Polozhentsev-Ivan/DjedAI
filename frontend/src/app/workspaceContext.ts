import { createContext, useContext } from 'react';
import type { Schemas } from '@/api/client';

export interface Workspace {
  user: string;
  datasets: readonly Schemas['DatasetSummary'][];
  /** False until the dataset list has loaded once; pages wait instead of showing «не выбран». */
  ready: boolean;
  activeDataset: string | null;
  setActiveDataset: (id: string) => void;
}

export const WorkspaceContext = createContext<Workspace | null>(null);

export function useWorkspace(): Workspace {
  const workspace = useContext(WorkspaceContext);
  if (!workspace) throw new Error('useWorkspace вызван вне WorkspaceProvider');
  return workspace;
}
