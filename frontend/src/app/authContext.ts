import { createContext, useContext } from 'react';
import type { Schemas } from '@/api/client';
import type { SessionNotice } from '@/api/session';

export interface AuthState {
  user: Schemas['UserRead'] | null;
  pending: boolean;
  error: string | null;
  notice: SessionNotice;
  version: number;
  retry: () => void;
}

export const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth requires AuthProvider');
  return value;
}
