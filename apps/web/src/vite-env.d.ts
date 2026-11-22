/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_NORTHSTAR_LAMBDA_URL?: string;
  readonly VITE_NORTHSTAR_DEV_MODE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
