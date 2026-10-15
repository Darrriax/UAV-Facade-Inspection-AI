import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { templateCompilerOptions } from '@tresjs/core'

/**
 * Where the dev server sends `/api`.
 *
 * The default is the API started directly on the host. Inside docker compose the
 * same config runs in a container, where `localhost` is the container itself, so
 * compose sets VITE_API_PROXY_TARGET=http://api:8000 - the compose service name.
 */
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000'

/**
 * Watch by polling instead of by filesystem events.
 *
 * Docker Desktop does not forward the host's inotify events into a container
 * with a bind mount, so a save on the host produces no event there and hot
 * reload silently stops. Polling is a busy loop, so it stays off by default and
 * compose turns it on.
 */
const watchPolling = process.env.VITE_WATCH_POLLING === 'true'

export default defineConfig({
  plugins: [
    // TresJS resolves its <Tres*> components through the template compiler, so
    // its compiler options have to be merged into the Vue plugin. Without this
    // every Tres component ends up as an unresolved custom element at runtime.
    vue({ ...templateCompilerOptions }),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    watch: {
      // `npm run build` writes `dist/` inside the project root, and Vite watches
      // the root by default - so building while the dev server is running fires
      // a full page reload per emitted file, throwing away whatever state the
      // running app had. Nothing in `dist/` is ever served by the dev server.
      ignored: ['**/dist/**'],
      usePolling: watchPolling,
    },
    // The SPA talks to `/api/v1/...` on its own origin and Vite proxies it to
    // the API. Keeps the browser same-origin during development, so there is no
    // CORS preflight and no absolute host baked into the build.
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        /**
         * Keep the 3D stack out of the entry chunk, so the login, dashboard and
         * admin screens do not download and parse three.js. The three.js and
         * TresJS modules go into one chunk together: splitting them apart would
         * leave the two chunks importing each other in a circle.
         */
        manualChunks(id: string) {
          if (id.includes('/node_modules/three/') || id.includes('/node_modules/@tresjs/')) {
            return 'vendor-3d'
          }
          return undefined
        },
      },
    },
  },
})
