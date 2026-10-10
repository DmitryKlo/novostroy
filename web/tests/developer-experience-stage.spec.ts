// @ts-ignore The browser-only tsconfig omits Node declarations used by the Playwright runner.
import { existsSync, readFileSync } from 'node:fs'
// @ts-ignore The browser-only tsconfig omits Node declarations used by the Playwright runner.
import { dirname, resolve } from 'node:path'
// @ts-ignore The browser-only tsconfig omits Node declarations used by the Playwright runner.
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const fixturePath = resolve(repo, '.runtime/research/building-experience-20261010/developer-experience-ui-fixture.json')

test('staged source-plan regions and selected model keep one-step Escape and city volumes', async ({ page }, testInfo) => {
  test.skip(!existsSync(fixturePath), 'Staged developer experience fixture is not present')
  const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'))
  const registryId = fixture.building.registry_id as string
  const floorId = fixture.floor_detail.floor_id as string
  const planSha = fixture.floor_detail.plan.asset_sha256 as string
  const modelSha = fixture.model.asset_sha256 as string
  const planPath = resolve(repo, `data/property/developer_units/runs/${fixture.floors.unit_run_id}/assets/${planSha}.png`)
  const modelPath = resolve(repo, `data/property/developer_models/runs/${fixture.model.model_run_id}/assets/${modelSha}.glb`)
  expect(existsSync(planPath)).toBe(true)
  expect(existsSync(modelPath)).toBe(true)
  expect(fixture.floor_detail.plan_regions).toHaveLength(5)
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route(`**/v1/developer-unit-assets/${planSha}`, (route) => route.fulfill({ contentType: 'image/png', body: readFileSync(planPath) }))
  await page.route(`**/v1/developer-model-assets/${modelSha}`, (route) => route.fulfill({ contentType: 'model/gltf-binary', body: readFileSync(modelPath) }))
  await page.route(`**/v1/developer-buildings/${registryId}/**`, async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/model')) return route.fulfill({ json: fixture.model })
    if (url.pathname.endsWith('/floors')) return route.fulfill({ json: fixture.floors })
    if (url.pathname.endsWith(`/floors/${floorId}`)) return route.fulfill({ json: fixture.floor_detail })
    if (url.pathname.endsWith('/units')) {
      const items = fixture.units.items.filter((unit: { floor_id: string }) => !url.searchParams.has('floor_id') || unit.floor_id === url.searchParams.get('floor_id'))
      return route.fulfill({ json: { ...fixture.units, total: items.length, limit: 24, offset: 0, items } })
    }
    return route.continue()
  })
  await page.goto('/?__map_test=1')
  await page.getByRole('textbox', { name: 'Поиск зданий' }).fill('Лэйк')
  await page.getByRole('button', { name: 'Найти здания' }).click()
  const card = page.getByTestId(`building-${registryId}`)
  await card.waitFor()
  await card.locator('button').click()
  await expect.poll(() => page.evaluate(() => window.__novoMapTest?.selectedModelState()), { timeout: 30_000 })
    .toMatchObject({ loaded: true, texturesReady: true, visible: true })
  if (testInfo.project.name === 'mobile') expect(await page.locator('.map-point-card').evaluate((element) => getComputedStyle(element).clipPath)).toContain('inset')
  const whileSelected = await page.evaluate(() => window.__novoMapTest.selectedModelState())
  expect(whileSelected.cityVolumes.every((tile: { shown: boolean; expectedWithoutSelection: boolean }) => tile.shown === tile.expectedWithoutSelection)).toBe(true)
  await page.screenshot({ path: `test-results/${testInfo.project.name}-developer-selected-model.png`, fullPage: true })

  const opener = page.locator('.map-point-card-item > button').filter({ hasText: 'Этажи и квартиры' }).first()
  await opener.click()
  const dialog = page.getByRole('dialog')
  await dialog.locator(`[data-floor-id="${floorId}"]`).click()
  const volume = dialog.getByRole('region', { name: 'Схема этажа по плану источника' })
  await expect(volume).toBeVisible()
  await expect(volume.locator('polygon[role="button"]')).toHaveCount(5)
  const region = volume.locator('polygon[role="button"]').first()
  await region.click()
  await expect(volume.getByText('Соответствие квартире из списка не подтверждено.')).toBeVisible()
  await expect(volume.locator('.developer-floor-region-detail')).toContainText('2 комнаты · 72,3 м²')
  await expect(volume.locator('.developer-floor-region-detail')).not.toContainText('rooms')
  await page.screenshot({ path: `test-results/${testInfo.project.name}-developer-floor-volume.png`, fullPage: true })
  await page.keyboard.press('Escape')
  await expect(dialog.getByRole('heading', { name: '5 этаж' })).toBeVisible()
  await expect(volume.locator('.developer-floor-region-detail')).toHaveCount(0)
  await expect(region).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog.getByRole('heading', { name: /Клубный дом «Лэйк»/ })).toBeVisible()
  await expect(dialog.locator(`[data-floor-id="${floorId}"]`)).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(opener).toBeFocused()
  await page.getByRole('button', { name: 'Закрыть сведения о точке' }).click()
  await expect.poll(() => page.evaluate(() => window.__novoMapTest?.selectedModelState()?.visible), { timeout: 10_000 }).toBe(false)
  const afterDeselection = await page.evaluate(() => window.__novoMapTest.selectedModelState())
  expect(afterDeselection.cityVolumes.every((tile: { shown: boolean; expectedWithoutSelection: boolean }) => tile.shown === tile.expectedWithoutSelection)).toBe(true)
  expect(errors).toEqual([])
})
