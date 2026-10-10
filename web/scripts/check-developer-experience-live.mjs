import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { chromium, devices, expect } from '@playwright/test'

// Read-only browser/API gate for the already activated developer experience.
const base = new URL(process.argv[2] ?? 'http://127.0.0.1:5173')
const output = path.resolve(process.argv[3] ?? `../.runtime/browser-developer-experience-live/${new Date().toISOString().replaceAll(':', '-')}`)
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true })
const report = { base_url: base.origin, checked_at: new Date().toISOString(), api: {}, views: [], errors: [] }
const lakeId = 'registry-b1feab9d36bced349dfa4b1d'

async function getJson(request, route) {
  const response = await request.get(new URL(route, base).toString())
  assert.equal(response.status(), 200, `${route}: HTTP ${response.status()}`)
  assert.equal(new URL(response.url()).origin, base.origin, `${route}: unexpected redirect away from deployed origin`)
  return response.json()
}

async function inspect(name, options, sample, expected, fallback) {
  const context = await browser.newContext(options)
  const page = await context.newPage()
  const result = { name, screenshots: {}, page_errors: [] }
  report.views.push(result)
  page.on('pageerror', (error) => result.page_errors.push(error.message))
  try {
    await page.goto(new URL('/?__map_test=1', base).toString(), { waitUntil: 'domcontentloaded' })
    assert.equal(new URL(page.url()).origin, base.origin, 'browser redirected away from deployed origin')
    const filterToggle = page.getByRole('button', { name: /Фильтры квартир/ })
    await filterToggle.waitFor()
    if (await filterToggle.getAttribute('aria-expanded') !== 'true') await filterToggle.click()
    await page.getByRole('textbox', { name: 'Поиск зданий' }).fill('Лэйк')
    await page.getByRole('spinbutton', { name: 'Цена до' }).fill(String(sample.price_rub / 1_000_000))
    await page.getByRole('button', { name: 'Показать дома' }).click()
    const card = page.getByTestId(`building-${lakeId}`)
    await card.waitFor({ timeout: 30_000 })
    result.screenshots.filtered_list = path.join(output, `${name}-filtered-list.png`)
    await page.screenshot({ path: result.screenshots.filtered_list, fullPage: true })
    await card.locator('button').click()
    await page.getByRole('button', { name: 'Закрыть сведения о точке' }).waitFor()
    await page.waitForFunction(() => window.__novoMapTest?.selectedModelState()?.visible === true, null, { timeout: 30_000 })
    const selectedModel = await page.evaluate(() => window.__novoMapTest.selectedModelState())
    assert.ok(selectedModel.cityVolumes.every((tile) => !tile.shown), 'city volumes must be hidden while selected GLB is visible')
    result.screenshots.selected_model = path.join(output, `${name}-selected-model.png`)
    await page.screenshot({ path: result.screenshots.selected_model, fullPage: true })

    const unitsPath = `/v1/developer-buildings/${lakeId}/units`
    const filteredUnitsResponse = page.waitForResponse((response) => response.url().includes(unitsPath) && new URL(response.url()).searchParams.get('price_max_rub') === String(sample.price_rub))
    await page.locator('.map-point-card-item > button').filter({ hasText: 'Этажи и квартиры' }).first().click()
    const filteredUnits = await (await filteredUnitsResponse).json()
    const dialog = page.getByRole('dialog')
    await dialog.locator(`[data-unit-id="${sample.unit_id}"]`).waitFor()
    assert.equal(filteredUnits.unit_run_id, expected.unit_run_id)
    assert.ok(filteredUnits.items.every((unit) => unit.price_rub !== null && unit.price_rub <= sample.price_rub), 'house menu widened observed-price filter')
    assert.ok(filteredUnits.items.some((unit) => unit.unit_id === sample.unit_id), 'matching source unit missing from menu')
    assert.equal(await dialog.locator('[data-unit-id]').count(), filteredUnits.items.length)
    result.screenshots.filtered_house = path.join(output, `${name}-filtered-house.png`)
    await page.screenshot({ path: result.screenshots.filtered_house, fullPage: true })

    const filteredFloorResponse = page.waitForResponse((response) => response.url().includes(unitsPath) && new URL(response.url()).searchParams.get('floor_id') === sample.floor_id && new URL(response.url()).searchParams.get('price_max_rub') === String(sample.price_rub))
    await dialog.locator(`[data-floor-id="${sample.floor_id}"]`).click()
    const floorUnits = await (await filteredFloorResponse).json()
    assert.ok(floorUnits.items.every((unit) => unit.floor_id === sample.floor_id && unit.price_rub !== null && unit.price_rub <= sample.price_rub))
    await dialog.getByRole('region', { name: 'Схема этажа по плану источника' }).waitFor()
    assert.equal(await dialog.locator('.developer-floor-volume polygon[role="button"]').count(), 5)
    const planRegion = dialog.locator('.developer-floor-volume polygon[role="button"]').first()
    await planRegion.click()
    await dialog.locator('.developer-floor-region-detail').waitFor()
    await page.keyboard.press('Escape')
    assert.equal(await dialog.locator('.developer-floor-region-detail').count(), 0, 'first Escape must close only anonymous region detail')
    await expect(planRegion, 'Escape must restore focus to the plan region').toBeFocused()
    result.screenshots.filtered_floor = path.join(output, `${name}-filtered-floor.png`)
    await page.screenshot({ path: result.screenshots.filtered_floor, fullPage: true })

    await dialog.locator(`[data-unit-id="${sample.unit_id}"]`).click()
    await dialog.getByRole('heading', { name: /Квартира/ }).waitFor()
    assert.match(await dialog.locator('.developer-explorer-price').innerText(), /Цена в снимке на/)
    result.screenshots.unit_detail = path.join(output, `${name}-unit-detail.png`)
    await page.screenshot({ path: result.screenshots.unit_detail, fullPage: true })
    await page.keyboard.press('Escape')
    await dialog.locator(`[data-unit-id="${sample.unit_id}"]`).waitFor()
    await expect(dialog.locator(`[data-unit-id="${sample.unit_id}"]`), 'unit Escape must restore focus to its floor row').toBeFocused()

    await page.keyboard.press('Escape')
    await dialog.locator(`[data-floor-id="${sample.floor_id}"]`).waitFor()
    const allUnitsResponse = page.waitForResponse((response) => response.url().includes(unitsPath) && !new URL(response.url()).searchParams.has('price_max_rub') && !new URL(response.url()).searchParams.has('floor_id'))
    await dialog.getByRole('button', { name: 'Показать все квартиры дома' }).click()
    const allUnits = await (await allUnitsResponse).json()
    assert.ok(allUnits.total >= filteredUnits.total, 'explicit show-all must not lose source units')
    await dialog.getByRole('button', { name: 'Вернуть фильтры' }).waitFor()
    result.filtered_house_units = filteredUnits.total
    result.all_house_units = allUnits.total
    result.filtered_floor_units = floorUnits.total
    await page.keyboard.press('Escape')
    await dialog.waitFor({ state: 'detached' })
    const lakeOpener = page.locator('.map-point-card-item > button').filter({ hasText: 'Этажи и квартиры' }).first()
    await expect(lakeOpener, 'closing house menu must return focus to its opener').toBeFocused()
    await page.getByRole('button', { name: 'Закрыть сведения о точке' }).click()
    await page.waitForFunction(() => window.__novoMapTest?.selectedModelState()?.visible === false, null, { timeout: 10_000 })
    if (name === 'mobile-chromium') await page.getByRole('tab', { name: 'Здания' }).click()
    await page.locator('.developer-filter-panel .filter-buttons .text-button').click()
    await page.getByRole('textbox', { name: 'Поиск зданий' }).fill(fallback.name)
    await page.getByRole('button', { name: 'Найти здания' }).click()
    const fallbackCard = page.getByTestId(`building-${fallback.registry_id}`)
    await fallbackCard.waitFor({ timeout: 30_000 })
    const fallbackResponse = page.waitForResponse((response) => response.url().endsWith(`/v1/developer-buildings/${fallback.registry_id}/model`))
    await fallbackCard.locator('button').click()
    assert.equal((await (await fallbackResponse).json()).status, 'unavailable')
    await page.waitForFunction(() => {
      const state = window.__novoMapTest?.selectedModelState()
      return state && !state.visible && !state.loaded && state.cityVolumes.every((tile) => tile.shown === tile.expectedWithoutSelection)
    }, null, { timeout: 15_000 })
    result.screenshots.model_fallback = path.join(output, `${name}-model-fallback.png`)
    await page.screenshot({ path: result.screenshots.model_fallback, fullPage: true })
    result.model_fallback_registry_id = fallback.registry_id
    assert.deepEqual(result.page_errors, [])
  } catch (error) {
    result.error = error instanceof Error ? error.message : String(error)
    result.screenshots.failure = path.join(output, `${name}-failure.png`)
    await page.screenshot({ path: result.screenshots.failure, fullPage: true }).catch(() => {})
    throw error
  } finally { await context.close() }
}

try {
  const context = await browser.newContext()
  const request = context.request
  const health = await getJson(request, '/v1/health')
  assert.equal(health.catalog_view, 'developer_buildings')
  assert.ok(health.registry_snapshot_id && health.map_run_id && health.unit_run_id, 'atomic developer experience is not active')
  const [floors, units, model] = await Promise.all([
    getJson(request, `/v1/developer-buildings/${lakeId}/floors`),
    getJson(request, `/v1/developer-buildings/${lakeId}/units?limit=100`),
    getJson(request, `/v1/developer-buildings/${lakeId}/model`),
  ])
  const sameRun = (value) => assert.ok(value.registry_snapshot_id === health.registry_snapshot_id && value.map_run_id === health.map_run_id && value.unit_run_id === health.unit_run_id)
  for (const value of [floors, units, model]) sameRun(value)
  assert.equal(model.status, 'ready')
  assert.equal(model.depiction, 'schematic_massing')
  assert.equal(model.photo_facade_reconstruction, true)
  assert.equal(model.model_transform.length, 16)
  const modelAsset = await request.get(new URL(model.asset_url, base).toString())
  assert.equal(modelAsset.status(), 200, 'selected GLB asset is missing')
  assert.ok((await modelAsset.body()).length > 100_000)
  const pilotFloor = floors.sections.flatMap((section) => section.floors).find((floor) => floor.floor_number === 5 && floor.plan)
  assert.ok(pilotFloor, 'Lake fifth-floor source plan was not published')
  const floorDetail = await getJson(request, `/v1/developer-buildings/${lakeId}/floors/${pilotFloor.floor_id}`)
  sameRun(floorDetail)
  assert.equal(floorDetail.plan_regions.length, 5)
  assert.ok(floorDetail.plan_regions.every((region) => region.unit_id === null && region.unit_action === false && region.match_status === 'unconfirmed'))
  const planAsset = await request.get(new URL(floorDetail.plan.asset_url, base).toString())
  assert.equal(planAsset.status(), 200, 'source floor-plan asset is missing')
  const sample = units.items.find((unit) => unit.floor_id === pilotFloor.floor_id && Number.isInteger(unit.price_rub) && unit.price_rub > 0)
  assert.ok(sample, 'Lake pilot floor has no priced observed unit for the filter gate')
  const params = new URLSearchParams({ query: 'Лэйк', price_max_rub: String(sample.price_rub), unit_floor_min: String(sample.floor_number), unit_floor_max: String(sample.floor_number) })
  const [list, map, points, filteredMenu] = await Promise.all([
    getJson(request, `/v1/registry/buildings?${params}&limit=48`),
    getJson(request, `/v1/registry/map?${params}`),
    getJson(request, `/v1/map/points?${params}`),
    getJson(request, `/v1/developer-buildings/${lakeId}/units?price_max_rub=${sample.price_rub}&floor_id=${sample.floor_id}&limit=48`),
  ])
  assert.equal(list.unit_run_id, health.unit_run_id)
  assert.equal(map.unit_run_id, health.unit_run_id)
  assert.equal(points.unit_run_id, health.unit_run_id)
  const ids = (items) => items.map((item) => item.registry_id).sort()
  assert.deepEqual(ids(map.items), ids(points.items), 'filtered map and point identities differ')
  assert.ok(list.items.some((item) => item.registry_id === lakeId))
  assert.ok(map.items.some((item) => item.registry_id === lakeId))
  assert.ok(filteredMenu.items.some((item) => item.unit_id === sample.unit_id))
  assert.ok(filteredMenu.items.every((item) => item.price_rub <= sample.price_rub && item.floor_id === sample.floor_id))
  const exactUnit = new URLSearchParams({ query: 'Лэйк', price_min_rub: String(sample.price_rub), price_max_rub: String(sample.price_rub),
    price_per_m2_min_rub: String(Math.floor(sample.price_rub / sample.area_m2) - 1_000),
    price_per_m2_max_rub: String(Math.ceil(sample.price_rub / sample.area_m2) + 1_000),
    rooms: sample.rooms_label, area_min_m2: String(sample.area_m2 - 0.1), area_max_m2: String(sample.area_m2 + 0.1),
    unit_floor_min: String(sample.floor_number), unit_floor_max: String(sample.floor_number) })
  const exactResponses = await Promise.all([
    getJson(request, `/v1/registry/buildings?${exactUnit}&limit=48`),
    getJson(request, `/v1/registry/map?${exactUnit}`),
    getJson(request, `/v1/map/points?${exactUnit}`),
    getJson(request, `/v1/developer-buildings/${lakeId}/units?${exactUnit}&limit=48`),
  ])
  assert.ok(exactResponses.every((value) => value.items.length > 0), 'combined source-unit filter lost the pilot unit')
  assert.deepEqual(ids(exactResponses[1].items), ids(exactResponses[2].items))
  assert.ok(exactResponses[3].items.some((item) => item.unit_id === sample.unit_id), 'combined filter lost its exact source unit')
  for (const [key, value, scope] of [['price_max_rub', 1, ''], ['price_per_m2_max_rub', 1, ''], ['rooms', 0, 'query=Лэйк&'], ['area_min_m2', 1_000, 'query=Лэйк&'], ['unit_floor_min', 100, 'query=Лэйк&']]) {
    const none = await Promise.all(['/v1/registry/buildings', '/v1/registry/map', '/v1/map/points']
      .map((route) => getJson(request, `${route}?${scope}${key}=${value}`)))
    assert.ok(none.every((result) => result.items.length === 0), `${key} failed to exclude unknown or out-of-range units`)
  }
  const allPoints = await getJson(request, '/v1/map/points')
  let fallback = null
  for (const point of allPoints.items.slice(0, 32)) {
    if (point.registry_id === lakeId) continue
    const candidate = await getJson(request, `/v1/developer-buildings/${point.registry_id}/model`)
    sameRun(candidate)
    if (candidate.status === 'unavailable') { fallback = point; break }
  }
  assert.ok(fallback, 'no mapped building without a prepared GLB was found for fallback verification')
  report.api = { registry_snapshot_id: health.registry_snapshot_id, map_run_id: health.map_run_id, unit_run_id: health.unit_run_id,
    model_run_id: model.model_run_id, lake_id: lakeId, sample_unit_id: sample.unit_id, sample_price_rub: sample.price_rub,
    filtered_list_total: list.records_total, filtered_map_points: map.items.length, filtered_menu_total: filteredMenu.total,
    combined_filter_menu_total: exactResponses[3].total,
    anonymous_plan_regions: floorDetail.plan_regions.length, model_asset_bytes: (await modelAsset.body()).length,
    unavailable_model_registry_id: fallback.registry_id }
  await context.close()
  await inspect('desktop', { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 }, sample, health, fallback)
  const { defaultBrowserType: _browserType, ...mobileViewport } = devices['iPhone 13']
  await inspect('mobile-chromium', mobileViewport, sample, health, fallback)
} catch (error) { report.errors.push(error instanceof Error ? error.stack ?? error.message : String(error)) }
finally {
  await browser.close()
  const destination = path.join(output, 'report.json')
  await writeFile(destination, `${JSON.stringify(report, null, 2)}\n`)
  console.log(destination)
  if (report.errors.length) process.exitCode = 1
}
