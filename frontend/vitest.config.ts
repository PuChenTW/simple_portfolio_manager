import { defineConfig } from 'vitest/config'

// Deliberately separate from `vite.config.ts` rather than a `test` block inside it. That config
// writes its build output into `../src/portfolio_manager/static`, which the API serves; a runner
// sharing it can empty the served dashboard as a side effect of running tests. Nothing here is
// referenced by `vite build`, so the test runner ships nothing into the bundle.
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node',
  },
})
