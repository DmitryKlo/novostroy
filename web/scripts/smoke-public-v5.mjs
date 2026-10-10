import { chromium, devices } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

const targets = [
  { id: 'known-parent', center: [37.531456, 55.670429], zoom: 6, partTopsM: [6, 66] },
  { id: 'new-five-part-parent', center: [37.4894868457, 55.7696515155], zoom: 8, partTopsM: [5, 52, 55] },
]
const variants = [
  ['desktop', { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } }],
  ['mobile', { ...devices['iPhone 13'], browserName: 'chromium' }],
]
const browser = await chromium.launch({ headless: true })
const report = { targets, runs: [] }

for (const target of targets) for (const [name, device] of variants) {
  const context = await browser.newContext(device)
  const page = await context.newPage()
  await page.addInitScript(() => performance.setResourceTimingBufferSize(10000))
  const errors = []
  const badResponses = []
  const cityGlbs = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()) })
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname
    if (response.status() >= 400) badResponses.push({ status: response.status(), path })
    if (response.ok() && path.startsWith('/assets/public-citywide-v5/') && path.endsWith('.glb')) cityGlbs.push(path)
  })
  await page.route('**/v1/map*', async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    body.center = target.center
    await route.fulfill({ response, body: JSON.stringify(body) })
  })
  await page.goto('http://127.0.0.1:5173/?__map_test=1')
  await page.getByRole('heading', { name: 'Стоит посмотреть' }).waitFor()
  const catalogOfferVisible = await page.locator('[data-testid^="offer-"]').first().isVisible()
  if (name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()
  await page.locator('.map-top').getByText('7/7 слоёв').waitFor()
  await page.getByRole('button', { name: 'К центру Москвы' }).click()
  await page.waitForTimeout(1200)
  await page.evaluate(() => window.__novoMapTest.clearFrameTimes())
  for (let index = 0; index < target.zoom; index++) await page.getByRole('button', { name: 'Приблизить карту' }).click()
  await page.waitForTimeout(5000)
  const state = await page.evaluate(() => ({
    camera: window.__novoMapTest.cameraPose(),
    city: window.__novoMapTest.tileSelection().find((item) => item.url.includes('/public-citywide-v5/')),
    cache: window.__novoMapTest.cacheBudget(),
    layers: document.querySelector('.map-top')?.textContent,
    horizontalOverflow: document.documentElement.scrollWidth > innerWidth,
  }))
  const performanceMetrics = await page.evaluate(() => {
    const resources = performance.getEntriesByType('resource')
    const frames = window.__novoMapTest.frameTimes()
    const intervals = frames.slice(1).map((time, index) => time - frames[index]).sort((a, b) => a - b)
    const percentile = (fraction) => intervals[Math.min(intervals.length - 1, Math.floor(intervals.length * fraction))] ?? null
    return {
      resourceCount: resources.length,
      encodedBodyBytes: resources.reduce((sum, item) => sum + item.encodedBodySize, 0),
      decodedBodyBytes: resources.reduce((sum, item) => sum + item.decodedBodySize, 0),
      transferBytes: resources.reduce((sum, item) => sum + item.transferSize, 0),
      renderedFrameCount: frames.length,
      renderedFrameIntervalP50Ms: percentile(.5),
      renderedFrameIntervalP95Ms: percentile(.95),
      devicePixelRatio: window.devicePixelRatio,
      canvasPixelRatio: (() => { const canvas = document.querySelector('.cesium-widget canvas'); return canvas.width / canvas.clientWidth })(),
    }
  })
  await page.screenshot({ path: new URL(`../artifacts/final-v5-${target.id}-${name}.png`, import.meta.url).pathname, fullPage: true })

  let data = null
  let sourceRoutes = null
  if (target.id === 'new-five-part-parent') {
    await page.getByRole('button', { name: 'Данные', exact: true }).click()
    await page.getByText('1 000', { exact: true }).first().waitFor()
    const demoCount = (await page.getByText('1 000', { exact: true }).first().innerText()).replace(/\s/g, ' ')
    await page.getByRole('tab', { name: 'Реальные предложения' }).click()
    const realEmpty = await page.getByText('Нет подтверждённых актуальных предложений').isVisible()
    await page.getByRole('tab', { name: 'Геометрия города' }).click()
    const cityCount = (await page.getByText('564 031', { exact: true }).first().innerText()).replace(/\s/g, ' ')
    await page.screenshot({ path: new URL(`../artifacts/final-v5-data-city-${name}.png`, import.meta.url).pathname, fullPage: true })
    await page.getByRole('button', { name: 'К карте' }).click()
    await page.getByTestId('map').waitFor()
    await page.waitForFunction(() => window.__novoMapTest?.cameraHeight() > 0)
    const cameraRestored = Math.abs((await page.evaluate(() => window.__novoMapTest.cameraHeight())) - state.camera.height) < 2
    if (name === 'mobile') await page.getByRole('tab', { name: 'Квартиры' }).click()
    const catalogRestored = await page.getByRole('heading', { name: 'Стоит посмотреть' }).isVisible()
    data = { demoCount, realEmpty, cityCount, catalogRestored, cameraRestored }
    sourceRoutes = {
      publicCity: (await page.request.get('http://127.0.0.1:5173/assets/public-citywide-v5/tileset.json')).status(),
      rawCity: (await page.request.get('http://127.0.0.1:5173/assets/citywide-v5/tileset.json')).status(),
      rawRoads: (await page.request.get('http://127.0.0.1:5173/assets/roads-v1/tileset.json')).status(),
    }
  }
  const city = state.city
  const cacheTotal = state.cache.reduce((sum, item) => sum + item.cacheBytes + item.maximumCacheOverflowBytes, 0)
  const run = { target: target.id, viewport: name, catalogOfferVisible, ...state, performanceMetrics, cacheTotal, cityGlbCount: cityGlbs.length, cityGlbPaths: [...new Set(cityGlbs)], data, sourceRoutes, badResponses, errors }
  report.runs.push(run)
  if (!catalogOfferVisible || !state.layers?.includes('7/7') || !city?.selectedContent.some((url) => url.includes('/cell_')) || city.memoryAdjustedScreenSpaceError !== 24 || cacheTotal > (name === 'mobile' ? 128 : 160) * 1024 * 1024 || errors.length || badResponses.length || state.horizontalOverflow) throw new Error(`Visual smoke failed: ${target.id}/${name}`)
  if (data) console.log('content-check', name, JSON.stringify({ data, sourceRoutes }))
  if (data && (data.demoCount !== '1 000' || !data.realEmpty || data.cityCount !== '564 031' || !data.catalogRestored || !data.cameraRestored || sourceRoutes.publicCity !== 200 || sourceRoutes.rawCity !== 404 || sourceRoutes.rawRoads !== 404)) throw new Error(`Content/privacy smoke failed: ${name}`)
  console.log(`${target.id}/${name}: camera ${Math.round(state.camera.height)}m, detail ${city.selectedCount}, SSE ${city.memoryAdjustedScreenSpaceError}, city GLBs ${cityGlbs.length}, errors ${errors.length}`)
  await context.close()
}

await browser.close()
await writeFile(new URL('../artifacts/final-v5-browser-smoke.json', import.meta.url), JSON.stringify(report, null, 2))
