import { chromium, devices } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

const center = [37.531456, 55.670429]
const browser = await chromium.launch({ headless: true })
const report = {}

for (const [name, device] of [
  ['desktop', { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } }],
  ['mobile', { ...devices['iPhone 13'], browserName: 'chromium' }],
]) {
  const context = await browser.newContext(device)
  const page = await context.newPage()
  const errors = []
  const cityGlbs = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()) })
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname
    if (path.startsWith('/assets/public-citywide-v4/') && path.endsWith('.glb') && response.ok()) cityGlbs.push(path)
  })
  await page.route('**/v1/map*', async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    body.center = center
    await route.fulfill({ response, body: JSON.stringify(body) })
  })
  await page.goto('http://127.0.0.1:5173/?__map_test=1')
  await page.getByRole('heading', { name: 'Стоит посмотреть' }).waitFor()
  if (name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()
  await page.locator('.map-top').getByText('7/7 слоёв').waitFor()
  await page.getByRole('button', { name: 'К центру Москвы' }).click()
  await page.waitForTimeout(1200)
  const views = []
  for (let zoom = 1; zoom <= 6; zoom++) {
    await page.getByRole('button', { name: 'Приблизить карту' }).click()
    if (zoom < 5) continue
    await page.waitForTimeout(4500)
    const state = await page.evaluate(() => ({
      camera: window.__novoMapTest.cameraPose(),
      city: window.__novoMapTest.tileSelection().find((item) => item.url.includes('/public-citywide-v4/')),
      layers: document.querySelector('.map-top')?.textContent,
    }))
    await page.screenshot({ path: new URL(`../artifacts/public-v4-parent-${name}-zoom${zoom}.png`, import.meta.url).pathname, fullPage: true })
    views.push({ zoom, ...state, cityGlbResponses: cityGlbs.length })
  }
  report[name] = { center, views, cityGlbPaths: [...new Set(cityGlbs)], errors }
  await context.close()
}
await browser.close()
await writeFile(new URL('../artifacts/public-v4-parent-smoke.json', import.meta.url), JSON.stringify(report, null, 2))
console.log(JSON.stringify(report, null, 2))
