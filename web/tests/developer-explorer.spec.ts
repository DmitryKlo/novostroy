import { expect, test } from '@playwright/test'
import fixture from './fixtures/developer-units.json' with { type: 'json' }

const registryId = fixture.floors.registry_id as string
const floorFive = fixture.floors.sections.flatMap((section: { floors: { floor_number: number; floor_id: string }[] }) => section.floors)
  .find((floor: { floor_number: number }) => floor.floor_number === 5)
if (!floorFive) throw new Error('Sanitized fixture has no fifth floor')

test('park route load error has one manual retry and stays distinct from unavailable route', async ({ page }, testInfo) => {
  const health = await (await page.request.get('/v1/health')).json()
  let attempts = 0
  await page.route(`**/v1/developer-buildings/${registryId}/park-walk`, (route) => {
    attempts += 1
    if (attempts === 1) return route.fulfill({ status: 503, json: { detail: 'busy' } })
    const identity = { registry_snapshot_id: health.registry_snapshot_id, map_run_id: health.map_run_id,
      unit_run_id: health.unit_run_id, enrichment_run_id: health.enrichment_run_id ?? null, registry_id: registryId }
    return route.fulfill({ json: attempts === 2
      ? { ...identity, status: 'routed', park: { id: 'park_nevsky', name: 'Невский парк' }, distance_m: 849,
        estimated_minutes: 11, geometry: [[37.6, 55.7], [37.61, 55.71]], approximate: true,
        endpoint_basis: 'pedestrian_graph_near_park_boundary', boundary_gap_m: 13, access_verified: false,
        origin_access: null, osm_snapshot_at: '2026-10-10', attribution: '© OpenStreetMap contributors' }
      : { ...identity, status: 'unavailable', park: null, distance_m: null, estimated_minutes: null,
        geometry: null, approximate: true, endpoint_basis: null, boundary_gap_m: null, access_verified: false,
        origin_access: null, osm_snapshot_at: '2026-10-10', attribution: '© OpenStreetMap contributors' } })
  })
  await page.goto('/')
  await page.getByRole('textbox', { name: 'Поиск зданий' }).fill('Лэйк')
  await page.getByRole('button', { name: 'Найти здания' }).click()
  const card = page.getByTestId(`building-${registryId}`)
  await card.waitFor()
  await card.locator('button').first().click()
  await expect(page.locator('.map-point-park-error')).toContainText('Не удалось загрузить маршрут до парка')
  await expect(card.locator('.registry-card-park-error')).toContainText('Не удалось загрузить маршрут до парка')
  await page.locator('.map-point-park-error').getByRole('button', { name: 'Повторить' }).click()
  await expect(page.locator('.map-point-park-summary')).toContainText('Невский парк')
  expect(attempts).toBe(2)
  await page.getByRole('button', { name: 'Закрыть сведения о точке' }).click()
  if (testInfo.project.name === 'mobile') await page.getByRole('tab', { name: 'Здания' }).click()
  await card.locator('button').first().click()
  await expect(page.locator('.map-point-park-summary')).toContainText('пеший маршрут пока не подтверждён')
  await expect(page.locator('.map-point-park-summary').getByRole('button', { name: 'Повторить' })).toHaveCount(0)
  expect(attempts).toBe(3)
})

test('selected house, floor and apartment have one-step Escape navigation and focus return', async ({ page }, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const health = await (await page.request.get('/v1/health')).json()
  const enrichment = { enrichment_run_id: health.enrichment_run_id ?? null }
  await page.route('**/v1/developer-buildings/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (!path.includes(registryId)) return route.continue()
    if (path.endsWith('/park-walk')) return route.fulfill({ json: { ...enrichment, registry_snapshot_id: health.registry_snapshot_id,
      map_run_id: health.map_run_id, unit_run_id: health.unit_run_id, registry_id: registryId,
      status: 'routed', park: { id: 'park_nevsky', name: 'Невский парк' }, distance_m: 849, estimated_minutes: 11,
      geometry: [[37.6, 55.7], [37.61, 55.71]], approximate: true,
      endpoint_basis: 'pedestrian_graph_near_park_boundary', boundary_gap_m: 13, access_verified: false,
      origin_access: null, osm_snapshot_at: '2026-10-10', attribution: '© OpenStreetMap contributors' } })
    if (path.endsWith('/floors')) return route.fulfill({ json: { ...fixture.floors, ...enrichment } })
    if (path.endsWith('/units')) {
      const floorId = url.searchParams.get('floor_id')
      const items = fixture.units.items.filter((unit: { floor_id: string }) => !floorId || unit.floor_id === floorId)
      return route.fulfill({ json: { ...fixture.units, ...enrichment, total: items.length, limit: 24, offset: 0, items } })
    }
    if (path.includes('/units/')) return route.fulfill({ json: { ...fixture.unit_detail, ...enrichment } })
    if (path.includes('/floors/')) return route.fulfill({ json: { ...fixture.floor_detail, ...enrichment, floor_id: floorFive.floor_id,
      floor_number: 5, section_id: fixture.unit_detail.section_id, section_label: '2' } })
    return route.continue()
  })
  await page.goto('/')
  await expect(page.locator('.header-location')).toHaveText(/Москва/)
  await expect(page.locator('.header-location svg')).toHaveCount(1)
  await page.getByRole('textbox', { name: 'Поиск зданий' }).fill('Лэйк')
  await page.getByRole('button', { name: 'Найти здания' }).click()
  await page.getByTestId(`building-${registryId}`).waitFor()
  await page.getByTestId(`building-${registryId}`).locator('button').click()
  await expect(page.locator('.map-point-park-summary')).toContainText('Невский парк')
  await expect(page.locator('.map-point-park-summary')).toContainText('11 мин пешком · ≈ 849 м по маршруту')
  await expect(page.locator('.map-point-park-summary')).toContainText(/[Вв]ход не подтверждён/)
  await page.screenshot({ path: `test-results/${testInfo.project.name}-park-selected.png`, fullPage: true })
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
  await expect(dialog.getByText('Номер квартиры')).toBeVisible()
  await expect(dialog.getByRole('heading', { name: 'Планировка квартиры' })).toBeVisible()
  await expect(dialog.getByText('Подготовленной планировки именно этой квартиры пока нет.')).toBeVisible()
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

test('price and floor filters keep the floor selector, unit list and detail on one matching unit', async ({ page }) => {
  const health = await (await page.request.get('/v1/health')).json()
  const enrichment = { enrichment_run_id: health.enrichment_run_id ?? null }
  const criteria = { price_max_rub: 52_000_000, unit_floor_min: 5, unit_floor_max: 5 }
  const matching = fixture.units.items.filter((item: { floor_number: number; price_rub: number }) => item.floor_number === 5 && item.price_rub <= criteria.price_max_rub)
  expect(matching).toHaveLength(1)
  const seen = new Set<string>()
  await page.route('**/v1/developer-media/media_aaaaaaaaaaaaaaaaaaaaaaaa', (route) => route.fulfill({ contentType: 'image/png',
    // @ts-ignore Buffer is provided by the Node Playwright runner.
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9jI0sAAAAASUVORK5CYII=', 'base64') }))
  await page.route(`**/v1/developer-buildings/${registryId}/**`, async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (!path.endsWith('/floors') && !path.includes('/floors/') && !path.endsWith('/units') && !path.includes('/units/')) return route.continue()
    const filtered = url.searchParams.has('price_max_rub')
    if (filtered) {
      expect(url.searchParams.get('price_max_rub')).toBe(String(criteria.price_max_rub))
      expect(url.searchParams.get('unit_floor_min')).toBe('5')
      expect(url.searchParams.get('unit_floor_max')).toBe('5')
      seen.add(path.endsWith('/floors') ? 'floors' : path.endsWith('/units') ? 'units' : path.includes('/units/') ? 'unit_detail' : 'floor_detail')
    }
    if (path.endsWith('/floors')) {
      const sections = filtered ? fixture.floors.sections.map((section: { floors: { floor_number: number; unit_count: number }[] }) => ({ ...section,
        floors: section.floors.filter((floor) => floor.floor_number === 5).map((floor) => ({ ...floor, source_unit_count: floor.unit_count, unit_count: 1 })) })).filter((section: { floors: unknown[] }) => section.floors.length) : fixture.floors.sections
      return route.fulfill({ json: { ...fixture.floors, ...enrichment, sections, matching_units_total: filtered ? 1 : fixture.units.items.length,
        applied_filters: filtered ? criteria : {}, unassigned_unit_count: 0 } })
    }
    if (path.endsWith('/units')) {
      const items = (filtered ? matching : fixture.units.items).filter((item: { floor_id: string }) => !url.searchParams.has('floor_id') || item.floor_id === url.searchParams.get('floor_id'))
      return route.fulfill({ json: { ...fixture.units, ...enrichment, total: items.length, limit: 24, offset: 0, applied_filters: filtered ? criteria : {}, items } })
    }
    if (path.includes('/units/')) return route.fulfill({ json: { ...fixture.unit_detail, ...enrichment, applied_filters: filtered ? criteria : {},
      observed_attributes: { observed_at: fixture.unit_detail.observed_at, record_updated_at: null, finishing_label: 'без отделки',
        delivery_deadline: 'IV квартал 2028', source_sale_status: 'в продаже на дату снимка', previous_price_rub: 53_000_000,
        discount_percent: 3, availability_confirmed_current: false, payment_terms_confirmed_unconditional: false },
      media: [{ media_id: 'media_aaaaaaaaaaaaaaaaaaaaaaaa', scope: 'unit_plan', role: 'apartment_plan', label: 'Планировка квартиры',
        observed_at: fixture.unit_detail.observed_at, asset_url: '/v1/developer-media/media_aaaaaaaaaaaaaaaaaaaaaaaa', availability: 'on_demand' }] } })
    return route.continue()
  })
  await page.goto('/')
  await page.getByRole('textbox', { name: 'Поиск зданий' }).fill('Лэйк')
  await page.getByRole('spinbutton', { name: 'Цена до' }).fill('52')
  await page.getByRole('spinbutton', { name: 'Этаж квартиры от' }).fill('5')
  await page.getByRole('spinbutton', { name: 'Этаж квартиры до' }).fill('5')
  await page.getByRole('button', { name: 'Показать дома' }).click()
  const card = page.getByTestId(`building-${registryId}`)
  await card.waitFor()
  await card.locator('button').click()
  await page.locator('.map-point-card-item > button').filter({ hasText: 'Этажи и квартиры' }).first().click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: 'Этажи с подходящими квартирами' })).toBeVisible()
  await expect(dialog.locator('[data-floor-id]')).toHaveCount(1)
  await expect(dialog.locator(`[data-floor-id="${floorFive.floor_id}"]`)).toContainText('Подходят: 1')
  await expect(dialog.locator('[data-unit-id]')).toHaveCount(1)
  await dialog.locator(`[data-floor-id="${floorFive.floor_id}"]`).click()
  await expect(dialog.locator('[data-unit-id]')).toHaveCount(1)
  await dialog.locator(`[data-unit-id="${matching[0].unit_id}"]`).click()
  await expect(dialog.getByRole('heading', { name: 'Квартира 107' })).toBeVisible()
  await expect(dialog.getByText('без отделки')).toBeVisible()
  await expect(dialog.getByText('IV квартал 2028')).toBeVisible()
  await expect(dialog.getByRole('img', { name: 'Планировка квартиры из источника' })).toBeVisible()
  expect(seen).toEqual(new Set(['floors', 'units', 'floor_detail', 'unit_detail']))
  await page.keyboard.press('Escape')
  await expect(dialog.locator(`[data-unit-id="${matching[0].unit_id}"]`)).toBeFocused()
  await dialog.getByRole('button', { name: 'Показать все квартиры дома' }).click()
  await expect(dialog.getByRole('heading', { name: 'Секции и этажи' })).toBeVisible()
  await expect(dialog.locator('[data-floor-id]')).toHaveCount(3)
  await dialog.getByRole('button', { name: 'Вернуть фильтры' }).click()
  await expect(dialog.locator('[data-floor-id]')).toHaveCount(1)
})

test('okrug and optional noise limit filter both house list and map, with dated modeled air and honest project media', async ({ page }) => {
  const query = '?query=%D0%9B%D1%8D%D0%B9%D0%BA'
  const [baseList, baseMap, basePoints] = await Promise.all([
    page.request.get(`/v1/registry/buildings${query}&limit=48&offset=0`).then((response) => response.json()),
    page.request.get(`/v1/registry/map${query}`).then((response) => response.json()),
    page.request.get(`/v1/map/points${query}`).then((response) => response.json()),
  ])
  const okrug = { id: 'okrug:северный административный', name: 'Северный административный округ', count: 1 }
  const enrichmentRun = 'developer-enrichment-v1-test'
  const media = { media_id: 'media_bbbbbbbbbbbbbbbbbbbbbbbb', scope: 'project_gallery', role: 'project_rendering', label: 'Визуализация ЖК',
    observed_at: baseList.snapshot_at, asset_url: '/v1/developer-media/media_bbbbbbbbbbbbbbbbbbbbbbbb', availability: 'on_demand' }
  const known = { ...baseList.items[0], enrichment_run_id: enrichmentRun, okrug_id: okrug.id, okrug_name: okrug.name, district_name: 'Западное Дегунино',
    noise_index: 35, air_quality: { status: 'modeled_forecast', label: 'Модельная оценка', pm2_5_ug_m3: 8.4, pm10_ug_m3: 17.2,
      no2_ug_m3: 12.1, valid_at: '2026-10-11T12:00:00Z', retrieved_at: '2026-10-11T10:00:00Z',
      spatial_scope: 'sampled_grid_cell_for_district', cell_resolution_km_approx: 11, stale: false,
      measurement: false, measured_district_average: false, data_credit: 'Open-Meteo / CAMS Europe (CC BY 4.0)' }, media: [media] }
  const unknown = { ...known, registry_id: 'registry-test-unknown-noise', point_id: 'point-test-unknown-noise',
    name: `${known.name} · неизвестный шум`, noise_index: null, okrug_id: null, okrug_name: null,
    location: [known.location[0] + 0.001, known.location[1] + 0.001],
    air_quality: { status: 'unavailable', reason: 'no_district' },
    media: [{ ...media, media_id: 'media_cccccccccccccccccccccccc', asset_url: '/v1/developer-media/media_cccccccccccccccccccccccc',
      scope: 'project_gallery_single_corpus', role: 'facade_photo', label: 'Фото ЖК' }] }
  const point = { ...basePoints.items[0], enrichment_run_id: enrichmentRun, okrug_id: okrug.id, okrug_name: okrug.name, noise_index: 35, media: [media] }
  const pointUnknown = { ...point, registry_id: unknown.registry_id, point_id: unknown.point_id, name: unknown.name,
    location: unknown.location, noise_index: null, okrug_id: null, okrug_name: null, media: unknown.media }
  const seen = new Set<string>()
  await page.route('**/v1/health', async (route) => {
    const original = await route.fetch()
    return route.fulfill({ json: { ...await original.json(), enrichment_run_id: enrichmentRun } })
  })
  await page.route('**/v1/developer-media/media_bbbbbbbbbbbbbbbbbbbbbbbb', (route) => route.fulfill({ contentType: 'image/png',
    // @ts-ignore Buffer is provided by the Node Playwright runner.
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9jI0sAAAAASUVORK5CYII=', 'base64') }))
  let photoRequests = 0
  await page.route('**/v1/developer-media/media_cccccccccccccccccccccccc**', (route) => {
    photoRequests += 1
    return photoRequests === 1 ? route.fulfill({ status: 503, body: 'temporarily unavailable' })
      : route.fulfill({ contentType: 'image/png',
        // @ts-ignore Buffer is provided by the Node Playwright runner.
        body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9jI0sAAAAASUVORK5CYII=', 'base64') })
  })
  await page.route('**/v1/registry/buildings**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname !== '/v1/registry/buildings' || url.searchParams.get('query') !== 'Лэйк') return route.continue()
    const active = url.searchParams.has('okrug_id') || url.searchParams.has('max_noise_index')
    if (active) { expect(url.searchParams.get('okrug_id')).toBe(okrug.id); expect(url.searchParams.get('max_noise_index')).toBe('100'); seen.add('list') }
    return route.fulfill({ json: { ...baseList, enrichment_run_id: enrichmentRun, records_total: active ? 1 : 2, items: active ? [known] : [known, unknown],
      facets: { ...baseList.facets, okrugs: [okrug], noise_known_count: 1 } } })
  })
  await page.route('**/v1/registry/map**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname !== '/v1/registry/map' || url.searchParams.get('query') !== 'Лэйк') return route.continue()
    const active = url.searchParams.has('okrug_id') || url.searchParams.has('max_noise_index')
    if (active) { expect(url.searchParams.get('okrug_id')).toBe(okrug.id); expect(url.searchParams.get('max_noise_index')).toBe('100'); seen.add('map') }
    return route.fulfill({ json: { ...baseMap, enrichment_run_id: enrichmentRun, items: active ? [known] : [known, unknown],
      counts: { ...baseMap.counts, records: active ? 1 : 2, geocoded: active ? 1 : 2 } } })
  })
  await page.route('**/v1/map/points**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname !== '/v1/map/points' || url.searchParams.get('query') !== 'Лэйк') return route.continue()
    const active = url.searchParams.has('okrug_id') || url.searchParams.has('max_noise_index')
    if (active) { expect(url.searchParams.get('okrug_id')).toBe(okrug.id); expect(url.searchParams.get('max_noise_index')).toBe('100'); seen.add('points') }
    return route.fulfill({ json: { ...basePoints, enrichment_run_id: enrichmentRun, items: active ? [point] : [point, pointUnknown],
      counts: { ...basePoints.counts, points: active ? 1 : 2, registry_points: active ? 1 : 2 },
      group_evidence: { ...basePoints.group_evidence, groups: [] } } })
  })
  await page.goto('/')
  await page.getByRole('textbox', { name: 'Поиск зданий' }).fill('Лэйк')
  await page.getByRole('button', { name: 'Найти здания' }).click()
  await expect(page.getByTestId(`building-${unknown.registry_id}`)).toBeVisible()
  await expect(page.getByRole('img', { name: 'Визуализация ЖК' })).toBeVisible()
  const photoCard = page.getByTestId(`building-${unknown.registry_id}`)
  await photoCard.scrollIntoViewIfNeeded()
  await photoCard.getByRole('button', { name: 'Повторить' }).click()
  const photo = photoCard.getByRole('img', { name: 'Фото ЖК' })
  await expect.poll(() => photo.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0)
  expect(photoRequests).toBe(2)
  await expect.poll(() => page.getByRole('img', { name: 'Визуализация ЖК' }).evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0)
  await expect(page.getByTestId(`building-${known.registry_id}`)).toContainText('Индекс шума: 35 из 100')
  await expect(page.getByTestId(`building-${known.registry_id}`)).toContainText('Модельная оценка: PM2.5 8,4 мкг/м³')
  await expect(page.getByTestId(`building-${known.registry_id}`)).toContainText('Open-Meteo / CAMS Europe (CC BY 4.0)')
  await expect(page.getByTestId(`building-${unknown.registry_id}`)).toContainText('Воздух: —')
  await page.getByRole('combobox', { name: 'Административный округ' }).selectOption(okrug.id)
  await page.getByRole('checkbox', { name: 'Ограничить индекс шума' }).check()
  await page.getByRole('button', { name: 'Показать дома' }).click()
  await expect(page.getByTestId(`building-${known.registry_id}`)).toBeVisible()
  await expect(page.getByTestId(`building-${unknown.registry_id}`)).toHaveCount(0)
  await expect(page.locator('.map-top')).toContainText('1 корпус')
  expect(seen).toEqual(new Set(['list', 'map', 'points']))
})
