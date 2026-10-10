import { expect, test } from '@playwright/test'

test('public city and roads, parks, water load as actual tiles with a bounded cache', async ({ page }, testInfo) => {
  const kinds = ['roads', 'parks', 'water']
  const loaded = new Set<string>()
  const errors: string[] = []
  page.on('response', (response) => {
    const pathname = new URL(response.url()).pathname
    if (!pathname.endsWith('.glb') || !response.ok()) return
    for (const kind of kinds) if (new RegExp(`/public-${kind}-v\\d+/`).test(pathname)) loaded.add(kind)
  })
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()) })
  await page.goto('/?__map_test=1')
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  if (testInfo.project.name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()
  await expect(page.locator('.map-top')).toContainText(/Подключено \d+\/\d+ наборов/)
  await page.getByRole('button', { name: 'К центру Москвы' }).click()
  await expect.poll(() => loaded.size, { timeout: 20_000 }).toBe(3)
  await page.waitForTimeout(1000)
  for (let index = 0; index < 3; index++) await page.getByRole('button', { name: 'Приблизить карту' }).click()
  await expect.poll(() => page.evaluate(() => {
    const city = window.__novoMapTest.tileSelection().find((item) => item.url.includes('/public-citywide-'))
    return city ? { budget: city.cacheBytes, adjustedSse: city.memoryAdjustedScreenSpaceError, hasDetail: city.selectedContent.some((url) => url.includes('/cell_')) } : null
  }), { timeout: 20_000 }).toMatchObject({ budget: expect.any(Number), adjustedSse: 24, hasDetail: true })
  const cityBudget = await page.evaluate(() => window.__novoMapTest.tileSelection().find((item) => item.url.includes('/public-citywide-'))?.cacheBytes ?? 0)
  expect(cityBudget).toBeGreaterThan(0)
  const map = await page.request.get('/v1/map')
  const descriptors = (await map.json()).tilesets as { kind: string; url: string }[]
  expect(descriptors.find((item) => item.kind === 'city_volumes')?.url).toMatch(/\/assets\/public-citywide-v\d+\/tileset\.json/)
  for (const kind of kinds) expect(descriptors.find((item) => item.kind === kind)?.url).toMatch(new RegExp(`/assets/public-${kind}-v\\d+/tileset\\.json`))
  const budget = await page.evaluate(() => window.__novoMapTest.cacheBudget())
  expect(budget.reduce((sum, item) => sum + item.cacheBytes + item.maximumCacheOverflowBytes, 0)).toBeLessThanOrEqual((testInfo.project.name === 'mobile' ? 128 : 160) * 1024 * 1024)
  expect(errors).toEqual([])
})
