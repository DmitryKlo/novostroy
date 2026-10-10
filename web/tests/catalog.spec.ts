import { expect, test } from '@playwright/test'

type MapTest = { project: (id: string, height?: number) => [number, number] | null; cameraHeight: () => number; markerCount: () => number; cacheBudget: () => { cacheBytes: number; maximumCacheOverflowBytes: number; totalMemoryUsageInBytes: number }[]; tileSelection: () => { url: string; cacheBytes: number; memoryAdjustedScreenSpaceError: number; selectedCount: number; selectedContent: string[] }[]; frameTimes: () => number[]; clearFrameTimes: () => void; inspectPick: (x: number, y: number) => { primitiveType?: string; complexId?: string } | null; selectedModelState: () => { loaded: boolean; visible: boolean; cityVolumes: { loaded: boolean; shown: boolean; expectedWithoutSelection: boolean }[] } }
declare global { interface Window { __novoMapTest: MapTest } }

test('catalog, tiles, search and favorites', async ({ page }, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/?__map_test=1')
  await expect(page.getByText('Демо-каталог')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  await expect(page.locator('[data-testid^="offer-"]').first()).toBeVisible()
  await expect(page.getByText(/Показано 100 из 1000 совпадений/)).toBeVisible()
  if (testInfo.project.name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()
  await expect(page.getByTestId('map')).toBeVisible()
  await expect.poll(async () => {
    const value = await page.locator('.map-top').innerText()
    return Number(value.match(/· (\d+)\//)?.[1] ?? 0)
  }).toBeGreaterThan(0)
  await expect(page.locator('.cesium-widget canvas')).toBeVisible()
  const cache = await page.evaluate(() => window.__novoMapTest.cacheBudget())
  expect(cache.length).toBeGreaterThan(0)
  expect(cache.reduce((sum, item) => sum + item.cacheBytes + item.maximumCacheOverflowBytes, 0)).toBeLessThanOrEqual((testInfo.project.name === 'mobile' ? 128 : 160) * 1024 * 1024)
  await page.screenshot({ path: `test-results/${testInfo.project.name}-map.png`, fullPage: true })
  if (testInfo.project.name === 'mobile') await page.getByRole('tab', { name: 'Квартиры' }).click()

  await page.getByRole('button', { name: /Фильтры/ }).click()
  const beforeFilterHeight = await page.evaluate(() => window.__novoMapTest.cameraHeight())
  await page.getByLabel('Бюджет до, млн ₽').fill('1')
  await page.getByRole('button', { name: /Показать квартиры/ }).click()
  await expect(page.getByText('Квартиры не найдены')).toBeVisible()
  await expect.poll(() => page.evaluate(() => window.__novoMapTest.markerCount())).toBe(0)
  const afterFilterHeight = await page.evaluate(() => window.__novoMapTest.cameraHeight())
  expect(Math.abs(afterFilterHeight - beforeFilterHeight)).toBeLessThan(2)
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click()
  await expect(page.locator('[data-testid^="offer-"]').first()).toBeVisible()
  const oneRoom = page.locator('.room-choices button').filter({ hasText: /^1$/ })
  await oneRoom.click()
  await expect(oneRoom).toHaveAttribute('aria-pressed', 'true')
  await oneRoom.click()
  await expect(oneRoom).toHaveAttribute('aria-pressed', 'false')
  await page.getByRole('button', { name: /Показать квартиры/ }).click()
  await expect(page.locator('[data-testid^="offer-"]').first()).toBeVisible()

  await page.getByRole('button', { name: 'В избранное' }).first().click()
  await page.getByRole('button', { name: /Избранное/ }).first().click()
  await expect(page.getByRole('heading', { name: 'Избранные квартиры' })).toBeVisible()
  await expect(page.locator('[data-testid^="offer-"]')).toHaveCount(1)
  await page.getByRole('button', { name: /Каталог/ }).click()
  await expect(page.getByRole('button', { name: 'Добавить к сравнению' })).toHaveCount(0)
  expect(errors).toEqual([])
})

test('facade model loads in the map near a selected complex', async ({ page }, testInfo) => {
  const loadedGlbs: string[] = []
  const errors: string[] = []
  page.on('response', (response) => { if (response.url().includes('.glb') && response.ok()) loadedGlbs.push(response.url()) })
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/?__map_test=1')
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  await page.getByRole('button', { name: /Демо ЖК 001/ }).first().click()
  if (testInfo.project.name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()
  await expect(page.locator('.results h2')).toHaveText('Демо ЖК 001')
  await expect.poll(() => loadedGlbs.some((url) => url.includes('/facades/demo-complex-001/lod2.glb'))).toBeTruthy()
  const model = await page.evaluate(() => {
    const testMap = window.__novoMapTest
    const [x, y] = testMap.project('demo-complex-001', 20)!
    for (let dy = -25; dy <= 25; dy += 3) for (let dx = -25; dx <= 25; dx += 3) {
      const hit = testMap.inspectPick(x + dx, y + dy)
      if (hit?.complexId === 'demo-complex-001') return { x: x + dx, y: y + dy, hit }
    }
    return { x, y, hit: testMap.inspectPick(x, y) }
  })
  expect(model.hit?.complexId).toBe('demo-complex-001')
  if (testInfo.project.name === 'mobile') await page.getByRole('tab', { name: 'Квартиры' }).click()
  await page.getByRole('button', { name: 'Показать все ЖК' }).click()
  if (testInfo.project.name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()
  await page.locator('.cesium-widget canvas').click({ position: { x: model.x, y: model.y } })
  await expect(page.locator('.results h2')).toHaveText('Демо ЖК 001')
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `test-results/${testInfo.project.name}-facade.png`, fullPage: true })
  expect(errors).toEqual([])
})

test('map marker opens a complex beyond the first search page', async ({ page }, testInfo) => {
  await page.goto('/?__map_test=1')
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  await expect(page.locator('[data-testid^="offer-"]')).toHaveCount(100)
  await expect(page.locator('.offer-card h3').filter({ hasText: 'Демо ЖК 091' })).toHaveCount(0)
  if (testInfo.project.name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()
  await page.waitForTimeout(1500)
  const point = await page.evaluate(() => window.__novoMapTest.project('demo-complex-091'))
  expect(point).not.toBeNull()
  const canvas = page.locator('.cesium-widget canvas')
  const box = await canvas.boundingBox()
  expect(point![0]).toBeGreaterThan(0)
  expect(point![0]).toBeLessThan(box!.width)
  expect(point![1]).toBeGreaterThan(0)
  expect(point![1]).toBeLessThan(box!.height)
  await canvas.click({ position: { x: point![0], y: point![1] } })
  await expect(page.locator('.results h2')).toHaveText('Демо ЖК 091')
  await expect(page.locator('[data-testid^="offer-"]').first()).toBeAttached()
})

test('ambiguous Russian district query asks for a choice without map guesses', async ({ page }) => {
  await page.goto('/?__map_test=1')
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  await page.getByRole('textbox', { name: 'Поиск квартир' }).fill('в Соколе')
  await page.getByRole('button', { name: 'Найти квартиры' }).click()
  await expect(page.getByText('Уточните запрос')).toBeVisible()
  await expect.poll(() => page.evaluate(() => window.__novoMapTest.markerCount())).toBe(0)
  await page.getByRole('button', { name: 'Выбрать район' }).click()
  await page.getByLabel('Район').selectOption({ label: 'Сокол' })
  await page.getByRole('button', { name: /Показать квартиры/ }).click()
  await expect(page.locator('[data-testid^="offer-"]').first()).toBeVisible()
  await expect.poll(() => page.evaluate(() => window.__novoMapTest.markerCount())).toBeGreaterThan(0)
  await expect(page.getByRole('textbox', { name: 'Поиск квартир' })).toHaveValue('')
})
