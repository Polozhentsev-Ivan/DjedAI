/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Public backend base URL; defaults to /api. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
