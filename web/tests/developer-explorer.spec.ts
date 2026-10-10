import { expect, test } from '@playwright/test'
import fixture from './fixtures/developer-units.json' with { type: 'json' }

const registryId = fixture.floors.registry_id as string
const floorFive = fixture.floors.sections.flatMap((section: { floors: { floor_number: number; floor_id: string }[] }) => section.floors)
  .find((floor: { floor_number: number }) => floor.floor_number === 5)
if (!floorFive) throw new Error('Sanitized fixture has no fifth floor')

test('selected house, floor and apartment have one-step Escape navigation and focus return', async ({ page }, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/v1/developer-buildings/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (!path.includes(registryId)) return route.continue()
    if (path.endsWith('/floors')) return route.fulfill({ json: fixture.floors })
    if (path.endsWith('/units')) {
      const floorId = url.searchParams.get('floor_id')
      const items = fixture.units.items.filter((unit: { floor_id: string }) => !floorId || unit.floor_id === floorId)
      return route.fulfill({ json: { ...fixture.units, total: items.length, limit: 24, offset: 0, items } })
    }
    if (path.includes('/units/')) return route.fulfill({ json: fixture.unit_detail })
    if (path.includes('/floors/')) return route.fulfill({ json: { ...fixture.floor_detail, floor_id: floorFive.floor_id,
      floor_number: 5, section_id: fixture.unit_detail.section_id, section_label: '2' } })
    return route.continue()
  })
  await page.goto('/')
  await page.getByRole('textbox', { name: 'Поиск зданий' }).fill('Лэйк')
  await page.getByRole('button', { name: 'Найти здания' }).click()
  await page.getByTestId(`building-${registryId}`).waitFor()
  await page.getByTestId(`building-${registryId}`).locator('button').click()
  const nearby = page.locator('.nearby-panel')
  for (const label of ['Метро', 'Школы и детсады', 'Медицина']) await expect(nearby.getByRole('button', { name: label })).toBeVisible()
  if (testInfo.project.name === 'mobile') {
    await expect(nearby).toHaveClass(/nearby-panel-compact/)
    const metro = nearby.getByRole('button', { name: 'Метро' })
    const before = await metro.getAttribute('aria-pressed')
    await metro.click()
    await expect(metro).toHaveAttribute('aria-pressed', before === 'true' ? 'false' : 'true')
    await expect(nearby).toHaveClass(/nearby-panel-compact/)
  }
  const opener = page.locator('.map-point-card-item > button').filter({ hasText: 'Этажи и квартиры' }).first()
  await opener.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('heading', { name: /Клубный дом «Лэйк»/ })).toBeVisible()
  await expect(dialog.getByText('Секции и этажи')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'К карте' })).toBeFocused()
  await page.screenshot({ path: `test-results/${testInfo.project.name}-developer-house.png`, fullPage: true })

  await dialog.getByRole('button', { name: /Разделы/ }).click()
  await expect(dialog.getByRole('navigation', { name: 'Разделы дома' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog.getByRole('navigation', { name: 'Разделы дома' })).toHaveCount(0)
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: /Разделы/ })).toBeFocused()

  const floorButton = dialog.locator(`[data-floor-id="${floorFive.floor_id}"]`)
  await floorButton.click()
  await expect(dialog.getByRole('heading', { name: '5 этаж' })).toBeVisible()
  await expect(dialog.getByText('Подтверждённой схемы расположения квартир')).toBeVisible()
  const unitButton = dialog.locator(`[data-unit-id="${fixture.unit_detail.unit_id}"]`)
  await unitButton.click()
  await expect(dialog.getByRole('heading', { name: 'Квартира 107' })).toBeVisible()
  await expect(dialog.getByText(/Цена в снимке на/)).toBeVisible()
  await expect(dialog.getByText(/Наличие квартиры сейчас не подтверждено/)).toBeVisible()
  await page.screenshot({ path: `test-results/${testInfo.project.name}-developer-unit.png`, fullPage: true })

  await page.keyboard.press('Escape')
  await expect(dialog.getByRole('heading', { name: '5 этаж' })).toBeVisible()
  await expect(unitButton).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog.getByRole('heading', { name: /Клубный дом «Лэйк»/ })).toBeVisible()
  await expect(floorButton).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(opener).toBeFocused()
  expect(errors).toEqual([])
})
