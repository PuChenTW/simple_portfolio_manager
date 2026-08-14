import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// `base: './'` is load-bearing, not cosmetic. Tailscale Serve strips its path prefix before
// forwarding and sends no X-Forwarded-Prefix, so a proxied request is byte-identical to a direct
// one at the app. Relative asset URLs are the only thing that lets one build serve both
// `/static/v2/` and `/portfolio/static/v2/`. See docs/ARCHITECTURE.md, "Sub-path deployment".
export default defineConfig({
  plugins: [svelte()],
  base: './',
  build: {
    outDir: '../src/portfolio_manager/static/v2',
    emptyOutDir: true,
  },
  server: {
    // Dev-only. The built app calls the API through relative paths and needs no proxy.
    proxy: {
      '/api': {
        target: process.env.PORTFOLIO_API_URL ?? 'http://127.0.0.1:8003',
        changeOrigin: true,
      },
    },
  },
})
