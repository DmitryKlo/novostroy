import { expect, test, type Page } from '@playwright/test'

async function submitWithPredicate(page: Page, key: string) {
  const search = page.waitForResponse(async (response) => response.url().endsWith('/v1/search') && response.request().method() === 'POST' && (response.request().postData() ?? '').includes(key))
  const map = page.waitForResponse((response) => response.url().includes('/v1/map?') && new URL(response.url()).searchParams.has('predicates'))
  await page.getByRole('button', { name: /Показать квартиры/ }).click()
  const [searchResponse, mapResponse] = await Promise.all([search, map])
  expect(searchResponse.status()).toBe(200)
  expect(mapResponse.status()).toBe(200)
  const searchData = await searchResponse.json()
  const mapData = await mapResponse.json()
  const mapPredicates = JSON.parse(new URL(mapResponse.url()).searchParams.get('predicates')!)
  expect(mapPredicates).toEqual(searchData.applied_filters.predicates)
  expect(mapData.applied_filters.predicates).toEqual(searchData.applied_filters.predicates)
  expect(mapData.counts.offers).toBe(searchData.count)
  return searchData as { count: number; applied_filters: { predicates: { key: string; op: string; value: unknown }[] } }
}

test('registry filters keep typed search and map in sync, including unknown facts', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  await expect(page.locator('.offer-card .fact-row').first()).toContainText('Цена за м²')
  await expect(page.locator('.offer-card .fact-row').first()).toContainText('Учебный пример')
  await expect(page.getByRole('button', { name: /Сравнение/ })).toHaveCount(0)

  await page.getByRole('button', { name: /Фильтры/ }).click()
  await page.getByRole('button', { name: /Дополнительные критерии/ }).click()
  await page.getByLabel('Найти критерий').fill('Этаж')
  await expect(page.getByTestId('criterion-unit.floor')).toBeVisible()
  await page.getByLabel('Этаж от').fill('7')
  const floor = await submitWithPredicate(page, 'unit.floor')
  expect(floor.count).toBeGreaterThan(0)
  expect(floor.count).toBeLessThan(1000)
  expect(floor.applied_filters.predicates).toContainEqual({ key: 'unit.floor', op: 'min', value: 7 })
  await expect(page.locator('.offer-card .fact-row').filter({ hasText: 'Этаж' }).first()).toContainText('Учебный пример')

  await page.getByLabel('Этаж от').fill('')
  await page.getByLabel('Найти критерий').fill('Видеонаблюдение')
  await expect(page.getByTestId('criterion-building.cctv')).toBeVisible()
  await page.getByRole('combobox', { name: 'Видеонаблюдение' }).selectOption('yes')
  const unknown = await submitWithPredicate(page, 'building.cctv')
  expect(unknown.applied_filters.predicates).toEqual([{ key: 'building.cctv', op: 'equals', value: true }])
  expect(unknown.count).toBe(0)
  await expect(page.getByText('Квартиры не найдены')).toBeVisible()
  expect(errors).toEqual([])
})

test('registry multi-select sends an array and remains a hard filter', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  await page.getByRole('button', { name: /Фильтры/ }).click()
  await page.getByRole('button', { name: /Дополнительные критерии/ }).click()
  await page.getByLabel('Найти критерий').fill('Класс энергоэффективности')
  const control = page.getByTestId('criterion-building.energy_class')
  await control.getByRole('checkbox', { name: 'A' }).check()
  await control.getByRole('checkbox', { name: 'B' }).check()
  const result = await submitWithPredicate(page, 'building.energy_class')
  expect(result.applied_filters.predicates).toEqual([{ key: 'building.energy_class', op: 'in', value: ['A', 'B'] }])
  expect(result.count).toBe(0)
  await expect(page.getByText('Квартиры не найдены')).toBeVisible()
})

test('price per square metre uses clear units and the exact-ratio predicate', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  await page.getByRole('button', { name: /Фильтры/ }).click()
  await page.getByRole('button', { name: /Дополнительные критерии/ }).click()
  await page.getByLabel('Найти критерий').fill('Цена за м²')
  const control = page.getByTestId('criterion-offer.price_per_sqm_rub')
  await expect(control).toContainText('тыс ₽/м²')
  await page.getByLabel('Цена за м², ₽ до').fill('250')
  const result = await submitWithPredicate(page, 'offer.price_per_sqm_rub')
  expect(result.applied_filters.predicates).toEqual([{ key: 'offer.price_per_sqm_rub', op: 'max', value: 250_000 }])
  expect(result.count).toBeGreaterThan(0)
  expect(result.count).toBeLessThan(1000)
  await expect(page.locator('.offer-card .fact-row').filter({ hasText: 'Цена за м²' }).first()).toContainText('₽/м²')
})

test('search error clears stale results and retry recovers', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  let failOnce = true
  await page.route('**/v1/search', async (route) => {
    if (failOnce) { failOnce = false; await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"Временная ошибка каталога"}' }) }
    else await route.continue()
  })
  await page.getByRole('button', { name: /Фильтры/ }).click()
  await page.getByLabel('Бюджет до, млн ₽').fill('20')
  await page.getByRole('button', { name: /Показать квартиры/ }).click()
  await expect(page.getByRole('alert')).toContainText('Временная ошибка каталога')
  await expect(page.locator('[data-testid^="offer-"]')).toHaveCount(0)
  await page.getByRole('button', { name: 'Повторить' }).click()
  await expect(page.locator('[data-testid^="offer-"]').first()).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(errors).toEqual([])
})
