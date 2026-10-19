/// <reference types="vite/client" />
/// <reference types="msw/vite/client" />

interface ImportMetaEnv {
  /** Public backend base URL; defaults to /api. */
  readonly VITE_API_BASE_URL?: string;
  /** Dev only: `false` turns the mock API off and uses the backend behind the proxy. */
  readonly VITE_API_MOCKS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
