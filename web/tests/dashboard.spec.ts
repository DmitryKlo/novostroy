import { expect, test } from '@playwright/test'

test('data tab separates demo, real and city counts and recovers from a failed aggregate request', async ({ page, request }) => {
  const health = await (await request.get('/v1/health')).json()
  const field = (key: string, total: number, kind: 'illustrative' | 'observed' | 'estimated') => ({
    key, unit: key === 'offer.price_total_rub' ? 'RUB' : key === 'unit.area_sqm' ? 'm²' : null,
    populated_count: total, unknown_count: 0, stale_count: 0,
    quality_counts: { observed: kind === 'observed' ? total : 0, derived: 0, estimated: kind === 'estimated' ? total : 0, illustrative: kind === 'illustrative' ? total : 0, render_fallback: 0 },
  })
  const report = {
    snapshot_id: health.snapshot_id, data_mode: health.data_mode, generated_at: health.snapshot_at, criteria_total: 55,
    datasets: [
      { dataset_id: 'demo_offers', data_mode: 'demo', status: 'loaded', snapshot_at: health.snapshot_at, records_total: 2, complexes_total: 1, fields: [field('offer.price_total_rub', 2, 'illustrative'), field('unit.area_sqm', 2, 'illustrative'), { key: 'building.cctv', unit: 'boolean', populated_count: 0, unknown_count: 2, stale_count: 0, quality_counts: { observed: 0, derived: 0, estimated: 0, illustrative: 0, render_fallback: 0 } }], distributions: [
        { key: 'offer.price_total_rub', unit: 'RUB', basis: 'asking_total', population_count: 2, sample_count: 1, missing_count: 0, excluded_count: 1, bins: [{ lower: 10_000_000, upper: 15_000_000, upper_inclusive: true, count: 1 }] },
        { key: 'unit.area_sqm', unit: 'm²', basis: 'total_area', population_count: 2, sample_count: 2, missing_count: 0, excluded_count: 0, bins: [{ lower: 30, upper: 50, upper_inclusive: true, count: 2 }] },
      ] },
      { dataset_id: 'real_current_offers', data_mode: 'real', status: 'no_licensed_feed', snapshot_at: null, records_total: 0, complexes_total: 0, fields: [], distributions: [] },
      { dataset_id: 'city_geometry', data_mode: 'geospatial', status: 'loaded', snapshot_at: health.snapshot_at, records_total: 10, complexes_total: null, fields: [field('city.footprint_geometry', 10, 'observed'), field('city.render_height_m', 10, 'estimated')], distributions: [] },
    ],
  }
  let failOnce = true
  await page.route('**/v1/data-coverage', async (route) => {
    if (failOnce) { failOnce = false; await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Отчёт временно недоступен"}' }) }
    else await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(report) })
  })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await page.getByRole('button', { name: 'Данные' }).click()
  await expect(page.getByRole('alert')).toContainText('Отчёт временно недоступен')
  await page.getByRole('button', { name: 'Повторить' }).click()
  await expect(page.getByRole('heading', { name: 'Что есть в данных' })).toBeVisible()
  await expect(page.getByTestId('coverage-offer.price_total_rub')).toContainText('2 из 2')
  await page.locator('.coverage-more summary').click()
  await expect(page.getByTestId('coverage-building.cctv')).toContainText('нет данных: 2')
  await expect(page.getByTestId('distribution-offer.price_total_rub')).toContainText('Исключено: 1')
  await expect(page.getByTestId('distribution-unit.area_sqm')).toContainText('2 из 2')

  await page.getByRole('tab', { name: 'Реальные предложения' }).click()
  await expect(page.getByText('Нет подтверждённых актуальных предложений')).toBeVisible()
  await expect(page.locator('.distribution-card')).toHaveCount(0)
  await page.getByRole('tab', { name: 'Геометрия города' }).click()
  await expect(page.getByTestId('coverage-city.render_height_m')).toContainText('10 оценочных')
  await expect(page.locator('.distribution-card')).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await page.getByRole('button', { name: /К карте/ }).click()
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  expect(errors).toEqual([])
})

test('live data report keeps datasets separate and reuses its small aggregate response', async ({ page, request }) => {
  const report = await (await request.get('/v1/data-coverage')).json()
  const demo = report.datasets.find((item: { dataset_id: string }) => item.dataset_id === 'demo_offers')
  const city = report.datasets.find((item: { dataset_id: string }) => item.dataset_id === 'city_geometry')
  let calls = 0
  page.on('request', (item) => { if (item.url().endsWith('/v1/data-coverage')) calls += 1 })
  await page.goto('/?__map_test=1')
  await expect(page.getByRole('heading', { name: 'Стоит посмотреть' })).toBeVisible()
  await page.waitForTimeout(1500)
  const cameraBefore = await page.evaluate(() => window.__novoMapTest.cameraHeight())
  await page.getByRole('button', { name: 'Данные' }).click()
  await expect(page.getByRole('heading', { name: 'Что есть в данных' })).toBeVisible()
  await expect(page.locator('.data-stat').first()).toContainText(new Intl.NumberFormat('ru-RU').format(demo.records_total))
  await expect(page.getByTestId('distribution-offer.price_total_rub')).toBeVisible()
  await page.getByRole('tab', { name: 'Реальные предложения' }).click()
  await expect(page.getByText('Нет подтверждённых актуальных предложений')).toBeVisible()
  await expect(page.locator('.distribution-card')).toHaveCount(0)
  await page.getByRole('tab', { name: 'Геометрия города' }).click()
  await expect(page.locator('.data-stat').first()).toContainText(new Intl.NumberFormat('ru-RU').format(city.records_total))
  await expect(page.getByTestId('coverage-city.render_height_m')).toBeVisible()
  await page.getByRole('button', { name: /К карте/ }).click()
  await page.getByRole('button', { name: 'Данные' }).click()
  await expect(page.getByRole('heading', { name: 'Что есть в данных' })).toBeVisible()
  expect(calls).toBe(1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await page.getByRole('button', { name: /К карте/ }).click()
  await expect.poll(() => page.evaluate(() => window.__novoMapTest?.cameraHeight())).toBeGreaterThan(0)
  const cameraAfter = await page.evaluate(() => window.__novoMapTest.cameraHeight())
  expect(Math.abs(cameraAfter - cameraBefore)).toBeLessThan(2)
})
