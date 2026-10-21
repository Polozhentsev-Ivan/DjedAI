import '@fontsource/inter/400.css';
import '@fontsource/inter/600.css';
import '@fontsource/inter/700.css';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/600.css';
import './styles/globals.css';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './app/App';
import { initTheme } from './lib/theme';

initTheme();

// Real backend by default. The prototype API is an explicit development option.
if (import.meta.env.DEV && import.meta.env.VITE_API_MOCKS === 'true') {
  const { startMocks } = await import('./mocks/browser');
  await startMocks();
}

const root = document.getElementById('root');
if (!root) throw new Error('В index.html нет элемента #root');

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
