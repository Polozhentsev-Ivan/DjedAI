import { Navigate, type RouteObject } from 'react-router';
import { CatalogPage } from '@/pages/CatalogPage';
import { LoginPage } from '@/pages/LoginPage';
import { NotFoundPage } from '@/pages/NotFoundPage';
import { SourcesPage } from '@/pages/SourcesPage';
import { LoadingState } from '@/components/StateBlock';
import { AppLayout } from './AppLayout';

export const routes: RouteObject[] = [
  { path: '/login', element: <LoginPage tab="login" /> },
  { path: '/register', element: <LoginPage tab="register" /> },
  {
    element: <AppLayout />,
    // Shown while a lazy screen loads on the first visit.
    hydrateFallbackElement: <LoadingState />,
    children: [
      { index: true, element: <Navigate to="/sources" replace /> },
      { path: 'sources', element: <SourcesPage /> },
      { path: 'catalog/:datasetId?', element: <CatalogPage /> },
      // Chart screens load on demand so ECharts stays out of the first bundle.
      { path: 'chat/:chatId?', lazy: async () => ({ Component: (await import('@/pages/ChatPage')).ChatPage }) },
      { path: 'dashboards/:dashboardId?', lazy: async () => ({ Component: (await import('@/pages/DashboardsPage')).DashboardsPage }) },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];
