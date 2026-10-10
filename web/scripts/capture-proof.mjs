import { chromium, devices } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'

const output = new URL('../artifacts/', import.meta.url)
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true })
const report = {}

for (const [name, device] of [
  ['desktop', { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } }],
  ['mobile', { ...devices['iPhone 13'], browserName: 'chromium' }],
]) {
  const context = await browser.newContext(device)
  const page = await context.newPage()
  await page.addInitScript(() => performance.setResourceTimingBufferSize(10000))
  const errors = []
  const glbs = new Map()
  page.on('pageerror', (error) => errors.push(`page: ${error.message}`))
  page.on('console', (message) => { if (message.type() === 'error') errors.push(`console: ${message.text()}`) })
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname
    if (!path.endsWith('.glb') || !response.ok()) return
    const layer = path.split('/')[2]
    glbs.set(layer, (glbs.get(layer) ?? 0) + 1)
  })

  await page.goto('http://127.0.0.1:5173/?__map_test=1')
  await page.getByRole('heading', { name: 'Стоит посмотреть' }).waitFor()
  if (name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()
  await page.locator('.map-top').getByText('7/7 слоёв').waitFor()
  await page.getByRole('button', { name: 'К центру Москвы' }).click()
  await page.waitForTimeout(1200)
  await page.evaluate(() => window.__novoMapTest.clearFrameTimes())
  for (let index = 0; index < 3; index++) await page.getByRole('button', { name: 'Приблизить карту' }).click()
  await page.waitForTimeout(5500)
  await page.screenshot({ path: new URL(`final-${name}-map-v3.png`, output).pathname, fullPage: true })
  const mapMetrics = await page.evaluate(() => {
    const entries = performance.getEntriesByType('resource')
    const frames = window.__novoMapTest.frameTimes()
    const intervals = frames.slice(1).map((time, index) => time - frames[index]).sort((a, b) => a - b)
    const p = (fraction) => intervals[Math.min(intervals.length - 1, Math.floor(intervals.length * fraction))] ?? null
    return {
      resourceCount: entries.length,
      encodedBodyBytes: entries.reduce((sum, item) => sum + item.encodedBodySize, 0),
      decodedBodyBytes: entries.reduce((sum, item) => sum + item.decodedBodySize, 0),
      transferBytes: entries.reduce((sum, item) => sum + item.transferSize, 0),
      renderedFrames: frames.length,
      frameIntervalP50Ms: p(.5), frameIntervalP95Ms: p(.95),
      cameraHeightM: window.__novoMapTest.cameraHeight(),
      cache: window.__novoMapTest.cacheBudget(),
      dpr: window.devicePixelRatio,
      canvasPixelRatio: (() => { const canvas = document.querySelector('.cesium-widget canvas'); return canvas.width / canvas.clientWidth })(),
      horizontalOverflow: document.documentElement.scrollWidth > innerWidth,
    }
  })

  if (name === 'mobile') await page.getByRole('tab', { name: 'Квартиры' }).click()
  await page.getByRole('button', { name: /Демо ЖК 001/ }).first().click()
  if (name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()
  await page.waitForTimeout(2800)
  await page.screenshot({ path: new URL(`final-${name}-facade-v3.png`, output).pathname, fullPage: true })
  const mobileControls = name === 'mobile' ? await page.evaluate(() => {
    return [...document.querySelectorAll('.map-controls button')].map((button) => {
      const box = button.getBoundingClientRect()
      const top = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)
      return { label: button.ariaLabel, tappable: button === top || button.contains(top) }
    })
  }) : null

  await page.getByRole('button', { name: 'Данные', exact: true }).click()
  await page.getByText('1 000', { exact: true }).first().waitFor()
  await page.screenshot({ path: new URL(`final-${name}-data-v3.png`, output).pathname, fullPage: true })
  await page.locator('.distribution-panel').scrollIntoViewIfNeeded()
  await page.screenshot({ path: new URL(`final-${name}-data-charts-v3.png`, output).pathname, fullPage: true })
  report[name] = { ...mapMetrics, glbResponsesByLayer: Object.fromEntries(glbs), mobileControls, errors }
  await context.close()
}

await browser.close()
await writeFile(new URL('final-browser-metrics-v3.json', output), JSON.stringify(report, null, 2))
console.log(JSON.stringify(report, null, 2))
