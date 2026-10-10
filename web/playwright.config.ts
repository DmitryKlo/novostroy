import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  timeout: 45_000,
  expect: { timeout: 15_000 },
  retries: 0,
  reporter: 'list',
  outputDir: 'test-results',
  use: { baseURL: 'http://127.0.0.1:5173', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [
    { name: 'desktop', testIgnore: '**/webkit.spec.ts', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    { name: 'mobile', testIgnore: '**/webkit.spec.ts', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
    { name: 'webkit-mobile', testMatch: ['**/webkit.spec.ts', '**/developer-explorer.spec.ts', '**/developer-experience-stage.spec.ts'], use: { ...devices['iPhone 13'], browserName: 'webkit' } },
  ],
  webServer: { command: 'rtk npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: true, timeout: 30_000 },
})
