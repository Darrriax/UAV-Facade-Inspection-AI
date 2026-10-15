/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Base URL of the REST API, without a trailing slash.
   *
   * Defaults to `/api/v1`, which the Vite dev server proxies and which a
   * reverse proxy serves in production - so the SPA stays same-origin and needs
   * no CORS configuration. Override it only when the API lives on another host.
   */
  readonly VITE_API_BASE_URL?: string

  /**
   * Where NodeODM listens, for the 3D page's own reconstructions.
   *
   * The page talks to ODM straight from the browser, so this is an absolute URL
   * and cannot go through the dev server's `/api` proxy. Defaults to
   * `http://localhost:3000`, which is the port `docker-compose.yml` publishes.
   */
  readonly VITE_NODEODM_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
