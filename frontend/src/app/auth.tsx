import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';
import { Navigate, Outlet } from 'react-router';
import { me } from '@/api/auth';
import type { Schemas } from '@/api/client';
import { isCurrentSession, sessionSnapshot, subscribeSession } from '@/api/session';
import { ErrorState, LoadingState } from '@/components/StateBlock';
import { AuthContext, useAuth } from './authContext';

interface CheckedSession {
  version: number;
  user: Schemas['UserRead'] | null;
  error: string | null;
}

/** Validate restored credentials before mounting any personal resource queries. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const cache = useQueryClient();
  const subscribe = useCallback((changed: () => void) => subscribeSession(() => {
    // Run before React mounts the next workspace. Old late responses are also
    // rejected by the session signal in the common API/SSE transport.
    void cache.cancelQueries();
    cache.clear();
    try { window.localStorage.removeItem('djedai.activeDataset'); } catch { /* Storage may be blocked. */ }
    changed();
  }), [cache]);
  const session = useSyncExternalStore(subscribe, sessionSnapshot);
  const [checked, setChecked] = useState<CheckedSession | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!session.accessToken) return;
    let active = true;
    void me().then((user) => {
      if (active && isCurrentSession(session)) setChecked({ version: session.version, user, error: null });
    }).catch((error: unknown) => {
      if (active && isCurrentSession(session)) setChecked({ version: session.version, user: null, error: error instanceof Error ? error.message : 'Не удалось проверить сессию' });
    });
    return () => { active = false; };
  }, [session, attempt]);
  const current = session.accessToken && checked?.version === session.version ? checked : null;
  const retry = useCallback(() => { setChecked(null); setAttempt((value) => value + 1); }, []);
  return (
    <AuthContext.Provider value={{ user: current?.user ?? null, pending: Boolean(session.accessToken && !current), error: current?.error ?? null, notice: session.notice, version: session.version, retry }}>
      {children}
    </AuthContext.Provider>
  );
}

export function AuthGate() {
  const auth = useAuth();
  if (auth.pending) return <LoadingState title="Проверяем сессию…" />;
  if (auth.error) return <ErrorState title="Не удалось проверить сессию" text={auth.error} onAction={auth.retry} />;
  if (!auth.user) return <Navigate to="/login" replace />;
  return <Outlet key={auth.version} />;
}
