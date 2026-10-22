import { Navigate, Outlet, type RouteObject } from 'react-router';
import { CatalogPage } from '@/pages/CatalogPage';
import { LoginPage } from '@/pages/LoginPage';
import { NotFoundPage } from '@/pages/NotFoundPage';
import { SourcesPage } from '@/pages/SourcesPage';
import { LoadingState } from '@/components/StateBlock';
import { AppLayout } from './AppLayout';
import { AuthGate, AuthProvider } from './auth';

export const routes: RouteObject[] = [
  {
    element: <AuthProvider><Outlet /></AuthProvider>,
    children: [
      { path: '/login', element: <LoginPage key="login" tab="login" /> },
      { path: '/register', element: <LoginPage key="register" tab="register" /> },
      {
        element: <AuthGate />,
        hydrateFallbackElement: <LoadingState />,
        children: [{
          element: <AppLayout />,
          children: [
            { index: true, element: <Navigate to="/sources" replace /> },
            { path: 'sources', element: <SourcesPage /> },
            { path: 'catalog/:datasetId?', element: <CatalogPage /> },
            // ECharts screens load on demand and stay out of the first bundle.
            { path: 'chat/:chatId?', lazy: async () => ({ Component: (await import('@/pages/ChatPage')).ChatPage }) },
            { path: 'dashboards/:dashboardId?', lazy: async () => ({ Component: (await import('@/pages/DashboardsPage')).DashboardsPage }) },
            { path: '*', element: <NotFoundPage /> },
          ],
        }],
      },
    ],
  },
];
