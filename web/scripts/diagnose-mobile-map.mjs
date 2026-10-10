import { chromium, devices } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

const browser = await chromium.launch({ headless: true })
const report = {}
for (const [name, zooms, cityUrl, extraKinds] of [
  ['v3-all', 3, null, null],
]) {
  const context = await browser.newContext({ ...devices['iPhone 13'], browserName: 'chromium' })
  const page = await context.newPage()
  const cityResponses = []
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname
    if (response.ok() && path.includes('citywide-v3') && path.endsWith('.glb')) cityResponses.push(path)
  })
  if (cityUrl || extraKinds) await page.route('**/v1/map*', async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    if (extraKinds) body.tilesets = body.tilesets.filter((item) => item.kind === 'city_volumes' || extraKinds.includes(item.kind))
    if (cityUrl) body.tilesets.find((item) => item.kind === 'city_volumes').url = cityUrl
    await route.fulfill({ response, body: JSON.stringify(body) })
  })
  await page.goto('http://127.0.0.1:5173/?__map_test=1')
  await page.getByRole('heading', { name: 'Стоит посмотреть' }).waitFor()
  await page.getByRole('tab', { name: 'Карта' }).click()
  await page.getByRole('button', { name: 'К центру Москвы' }).click()
  await page.waitForTimeout(1200)
  for (let index = 0; index < zooms; index++) await page.getByRole('button', { name: 'Приблизить карту' }).click()
  await page.waitForTimeout(5500)
  const state = await page.evaluate(() => ({ pose: window.__novoMapTest.cameraPose(), selection: window.__novoMapTest.tileSelection() }))
  await page.screenshot({ path: new URL(`../artifacts/diagnose-${name}.png`, import.meta.url).pathname, fullPage: true })
  report[name] = { ...state, cityResponses }
  console.log(name, state.pose, state.selection.find((item) => item.url.includes('citywide-v3')))
  await context.close()
}
await browser.close()
await writeFile(new URL('../artifacts/diagnose-mobile-map.json', import.meta.url), JSON.stringify(report, null, 2))
