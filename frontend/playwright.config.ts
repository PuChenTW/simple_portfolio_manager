import { defineConfig } from '@playwright/test'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const database = join(mkdtempSync(join(tmpdir(), 'portfolio-timezones-')), 'test.db')

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8017',
    timezoneId: 'Asia/Taipei',
    channel: 'chrome',
  },
  webServer: {
    command: 'uv run alembic upgrade head && uv run uvicorn portfolio_manager.api:app --host 127.0.0.1 --port 8017',
    cwd: '..',
    env: { PORTFOLIO_DB_PATH: database, PORTFOLIO_REDIS_URL: '' },
    url: 'http://127.0.0.1:8017/health',
    reuseExistingServer: false,
  },
})
