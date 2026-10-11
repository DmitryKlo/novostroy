import { expect, test } from '@playwright/test'

test('source footprints load as one filtered viewport tileset before selection', async ({ page }, testInfo) => {
  const failures: string[] = []
  page.on('pageerror', (error) => failures.push(error.message))
  page.on('response', (response) => {
    if (response.status() >= 400 && response.url().includes('/v1/developer-overlay/')) failures.push(`${response.status()} ${response.url()}`)
  })
  await page.goto('/?__map_test=1')
  await page.locator('.registry-card').first().waitFor()
  if (testInfo.project.name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()
  const overlay = () => page.evaluate(() => (window as unknown as { __novoMapTest: {
    overlayState: () => { loaded: boolean; shown: boolean; selectedTiles: number; featureCount: number; visibleFeatureCount: number; styledVisibleFeatureCount: number; selectedStyledFeatureCount: number; eligibleCount: number; url: string | null; selectedContent: (string | null)[] }
  } }).__novoMapTest.overlayState())
  await expect.poll(async () => (await overlay()).loaded).toBe(true)
  await expect.poll(async () => (await overlay()).selectedTiles > 0).toBe(true)
  await expect.poll(async () => (await overlay()).featureCount > 0).toBe(true)
  const state = await overlay()
  expect(state.url).toContain('/v1/developer-overlay/')
  expect(state.visibleFeatureCount).toBeGreaterThan(0)
  expect(state.visibleFeatureCount).toBeLessThanOrEqual(state.featureCount)
  expect(state.eligibleCount).toBeGreaterThan(0)
  expect(failures).toEqual([])
  await page.screenshot({ path: `test-results/${testInfo.project.name}-developer-overlay-no-selection.png`, fullPage: true })

  const pointsResponse = await page.request.get('/v1/map/points')
  expect(pointsResponse.ok()).toBe(true)
  const points = await pointsResponse.json() as { items: { registry_id: string; point_id: string; location: [number, number] }[] }
  const cityBay = points.items.find((point) => point.registry_id === 'registry-894956d965f21910a8f33117')
  expect(cityBay).toBeTruthy()
  await page.evaluate(([longitude, latitude]) => (window as unknown as { __novoMapTest: {
    focusAtForTest: (lon: number, lat: number, height?: number) => void
  } }).__novoMapTest.focusAtForTest(longitude, latitude, 440), cityBay!.location)
  await expect.poll(() => page.evaluate(() => (window as unknown as { __novoMapTest: {
    cameraHeight: () => number
  } }).__novoMapTest.cameraHeight())).toBeLessThan(1_000)
  try { await expect.poll(async () => (await overlay()).selectedStyledFeatureCount > 0, { timeout: 20_000 }).toBe(true) }
  catch (error) {
    console.log('near overlay diagnostic', testInfo.project.name, await overlay(), await page.evaluate(() => (window as unknown as { __novoMapTest: {
      adaptiveQuality: () => unknown; cityRange: () => unknown; tileSelection: () => unknown
    } }).__novoMapTest.adaptiveQuality()), await page.evaluate(() => (window as unknown as { __novoMapTest: { cityRange: () => unknown } }).__novoMapTest.cityRange()))
    await page.screenshot({ path: `test-results/${testInfo.project.name}-developer-overlay-city-bay-near-failure.png`, fullPage: true })
    throw error
  }
  const near = await overlay()
  expect(near.selectedStyledFeatureCount).toBeGreaterThan(0)
  expect(failures).toEqual([])
  await page.screenshot({ path: `test-results/${testInfo.project.name}-developer-overlay-city-bay-near.png`, fullPage: true })

  const pixel = await page.evaluate((id) => (window as unknown as { __novoMapTest: {
    project: (id: string, height?: number) => [number, number] | null
  } }).__novoMapTest.project(id, 36), cityBay!.point_id)
  expect(pixel).toBeTruthy()
  const canvas = page.locator('.cesium-widget canvas').first()
  const bounds = await canvas.boundingBox()
  expect(bounds).toBeTruthy()
  await page.mouse.click(bounds!.x + pixel![0], bounds!.y + pixel![1])
  await expect(page.locator('.map-point-card')).toBeVisible()
  await expect(page.locator('.map-point-card')).toContainText('Реконструкция фасада по изображениям ЖК', { timeout: 20_000 })
  await expect.poll(async () => (await overlay()).selectedStyledFeatureCount > 0).toBe(true)
  const selectedModel = await page.evaluate(() => (window as unknown as { __novoMapTest: {
    selectedModelState: () => { primitiveShown: boolean }
  } }).__novoMapTest.selectedModelState())
  expect(selectedModel.primitiveShown).toBe(false)
  expect(failures).toEqual([])
  await page.screenshot({ path: `test-results/${testInfo.project.name}-developer-overlay-city-bay-selected.png`, fullPage: true })
})
