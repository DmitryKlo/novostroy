import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { chromium, devices, expect } from '@playwright/test'

// Read-only browser/API gate for the already activated developer experience.
const positional = process.argv.slice(2).filter((arg) => arg !== '--public-ui' && arg !== '--require-enrichment')
const base = new URL(positional[0] ?? 'http://127.0.0.1:5173')
const output = path.resolve(positional[1] ?? `../.runtime/browser-developer-experience-live/${new Date().toISOString().replaceAll(':', '-')}`)
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true })
const diagnosticsAvailable = base.hostname === '127.0.0.1' && !process.argv.includes('--public-ui')
const requireEnrichment = process.argv.includes('--require-enrichment')
const report = { base_url: base.origin, checked_at: new Date().toISOString(), validation_mode: diagnosticsAvailable ? 'loopback_diagnostics' : 'public_dom_network', api: {}, views: [], errors: [] }
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
    await page.goto(new URL(diagnosticsAvailable ? '/?__map_test=1' : '/', base).toString(), { waitUntil: 'domcontentloaded' })
    assert.equal(new URL(page.url()).origin, base.origin, 'browser redirected away from deployed origin')
    const filterToggle = page.getByRole('button', { name: /Фильтры квартир/ })
    await filterToggle.waitFor()
    if (await filterToggle.getAttribute('aria-expanded') !== 'true') await filterToggle.click()
    await page.getByRole('textbox', { name: 'Поиск зданий' }).fill('Лэйк')
    if (expected.house) {
      await page.getByRole('combobox', { name: 'Административный округ' }).selectOption(expected.house.okrug_id)
      await page.getByRole('checkbox', { name: 'Ограничить индекс шума' }).check()
    }
    await page.getByRole('spinbutton', { name: 'Цена до' }).fill(String(sample.price_rub / 1_000_000))
    await page.getByRole('spinbutton', { name: 'Этаж квартиры от' }).fill(String(sample.floor_number))
    await page.getByRole('spinbutton', { name: 'Этаж квартиры до' }).fill(String(sample.floor_number))
    await page.getByRole('button', { name: 'Показать дома' }).click()
    const card = page.getByTestId(`building-${lakeId}`)
    await card.waitFor({ timeout: 30_000 })
    if (expected.enrichment_run_id) {
      assert.match(await card.innerText(), /Индекс шума: \d+ из 100/)
      if (expected.house.air_quality?.status === 'modeled_forecast') {
        assert.match(await card.innerText(), /Модельная оценка: PM2\.5 .*PM10 .*NO₂/)
        assert.match(await card.innerText(), /Open-Meteo \/ CAMS Europe/)
      }
      const photo = card.getByRole('img', { name: 'Фото ЖК' })
      await photo.scrollIntoViewIfNeeded()
      await expect.poll(() => photo.evaluate(async (image) => { await image.decode().catch(() => {}); return image.naturalWidth })).toBeGreaterThan(0)
      result.house_photo_decoded = true
    }
    result.screenshots.filtered_list = path.join(output, `${name}-filtered-list.png`)
    await page.screenshot({ path: result.screenshots.filtered_list, fullPage: true })
    const modelResponse = page.waitForResponse((response) => new URL(response.url()).pathname === `/v1/developer-buildings/${lakeId}/model`)
    const glbResponse = page.waitForResponse((response) => new URL(response.url()).pathname === expected.model_asset_url)
    await card.locator('button').click()
    await page.getByRole('button', { name: 'Закрыть сведения о точке' }).waitFor()
    assert.equal((await (await modelResponse).json()).status, 'ready')
    assert.equal((await glbResponse).status(), 200, 'selected GLB did not load in browser')
    await page.locator('.selected-model-caption').waitFor({ timeout: 30_000 })
    await expect(page.locator('.map-point-park-summary')).toContainText(/[Вв]ход не подтверждён/)
    assert.match(await page.locator('.map-point-park-summary').innerText(), new RegExp(`${new Intl.NumberFormat('ru-RU').format(expected.park_walk.distance_m)} м по маршруту`))
    assert.match(await page.locator('.map-top').textContent(), /реконструкция выбранного корпуса/i)
    assert.ok(await page.locator('.cesium-widget canvas').first().isVisible(), 'Cesium map canvas is not visible')
    if (diagnosticsAvailable) {
      const selectedModel = await page.evaluate(() => window.__novoMapTest.selectedModelState())
      assert.ok(selectedModel.visible && selectedModel.texturesReady && selectedModel.cityVolumes.every((tile) => tile.shown === tile.expectedWithoutSelection), 'selected textured GLB must preserve city volume visibility chosen by adaptive quality')
    }
    result.screenshots.selected_model = path.join(output, `${name}-selected-model.png`)
    await page.screenshot({ path: result.screenshots.selected_model, fullPage: true })
    const credits = page.locator('.map-credit-disclosure')
    await credits.locator('summary').click()
    await expect(credits).toHaveAttribute('open', '')
    assert.match(await credits.innerText(), /OpenStreetMap.*Overture/s, 'full map credits cannot be read')
    result.screenshots.map_credits = path.join(output, `${name}-map-credits.png`)
    await page.screenshot({ path: result.screenshots.map_credits, fullPage: true })
    await credits.locator('summary').click()

    const unitsPath = `/v1/developer-buildings/${lakeId}/units`
    const floorsPath = `/v1/developer-buildings/${lakeId}/floors`
    const hasActiveFilters = (response) => {
      const params = new URL(response.url()).searchParams
      return params.get('price_max_rub') === String(sample.price_rub)
        && params.get('unit_floor_min') === String(sample.floor_number)
        && params.get('unit_floor_max') === String(sample.floor_number)
    }
    const filteredFloorsResponse = page.waitForResponse((response) => new URL(response.url()).pathname === floorsPath && hasActiveFilters(response))
    const filteredUnitsResponse = page.waitForResponse((response) => new URL(response.url()).pathname === unitsPath && hasActiveFilters(response))
    await page.locator('.map-point-card-item > button').filter({ hasText: 'Этажи и квартиры' }).first().click()
    const filteredFloors = await (await filteredFloorsResponse).json()
    const filteredUnits = await (await filteredUnitsResponse).json()
    const dialog = page.getByRole('dialog')
    await dialog.locator(`[data-unit-id="${sample.unit_id}"]`).waitFor()
    assert.equal(filteredUnits.unit_run_id, expected.unit_run_id)
    const floorChoices = filteredFloors.sections.flatMap((section) => section.floors)
    assert.ok(floorChoices.length > 0 && floorChoices.every((floor) => floor.floor_number === sample.floor_number && floor.unit_count > 0), 'filtered floor selector contains an empty or out-of-range floor')
    assert.equal(await dialog.locator('[data-floor-id]').count(), floorChoices.length, 'floor buttons differ from filtered floor DTO')
    assert.ok(filteredUnits.items.every((unit) => unit.price_rub !== null && unit.price_rub <= sample.price_rub), 'house menu widened observed-price filter')
    assert.ok(filteredUnits.items.every((unit) => unit.floor_number === sample.floor_number), 'house menu widened floor filter')
    assert.ok(filteredUnits.items.some((unit) => unit.unit_id === sample.unit_id), 'matching source unit missing from menu')
    assert.equal(await dialog.locator('[data-unit-id]').count(), filteredUnits.items.length)
    result.screenshots.filtered_house = path.join(output, `${name}-filtered-house.png`)
    await page.screenshot({ path: result.screenshots.filtered_house, fullPage: true })

    const filteredFloorResponse = page.waitForResponse((response) => new URL(response.url()).pathname === unitsPath && new URL(response.url()).searchParams.get('floor_id') === sample.floor_id && hasActiveFilters(response))
    await dialog.locator(`[data-floor-id="${sample.floor_id}"]`).click()
    const floorUnits = await (await filteredFloorResponse).json()
    assert.ok(floorUnits.items.every((unit) => unit.floor_id === sample.floor_id && unit.price_rub !== null && unit.price_rub <= sample.price_rub))
    assert.equal(await dialog.locator('.developer-floor-region polygon[role="button"]').count(), 0, 'anonymous plan regions must not impersonate filtered units')
    result.screenshots.filtered_floor = path.join(output, `${name}-filtered-floor.png`)
    await page.screenshot({ path: result.screenshots.filtered_floor, fullPage: true })

    await dialog.locator(`[data-unit-id="${sample.unit_id}"]`).click()
    await dialog.getByRole('heading', { name: /Квартира/ }).waitFor()
    assert.match(await dialog.locator('.developer-explorer-price').innerText(), /Цена в снимке на/)
    if (expected.enrichment_run_id) {
      const plan = dialog.locator('.developer-explorer-plan img').first()
      await plan.scrollIntoViewIfNeeded()
      await expect.poll(() => plan.evaluate(async (image) => { await image.decode().catch(() => {}); return image.naturalWidth })).toBeGreaterThan(0)
      result.unit_plan_decoded = true
    }
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
    await dialog.locator(`[data-floor-id="${sample.floor_id}"]`).click()
    await dialog.getByRole('region', { name: 'Схема этажа по плану источника' }).waitFor()
    assert.equal(await dialog.locator('.developer-floor-volume polygon[role="button"]').count(), 5)
    await page.locator(`.developer-floor-volume image[href="${expected.plan_asset_url}"]`).waitFor()
    const planRegion = dialog.locator('.developer-floor-volume polygon[role="button"]').first()
    await planRegion.click()
    await dialog.locator('.developer-floor-region-detail').waitFor()
    await page.keyboard.press('Escape')
    assert.equal(await dialog.locator('.developer-floor-region-detail').count(), 0, 'first Escape must close only anonymous region detail')
    await expect(planRegion, 'Escape must restore focus to the plan region').toBeFocused()
    await page.keyboard.press('Escape')
    await dialog.locator(`[data-floor-id="${sample.floor_id}"]`).waitFor()
    result.filtered_house_units = filteredUnits.total
    result.all_house_units = allUnits.total
    result.filtered_floor_units = floorUnits.total
    await page.keyboard.press('Escape')
    await dialog.waitFor({ state: 'detached' })
    const lakeOpener = page.locator('.map-point-card-item > button').filter({ hasText: 'Этажи и квартиры' }).first()
    await expect(lakeOpener, 'closing house menu must return focus to its opener').toBeFocused()
    await page.getByRole('button', { name: 'Закрыть сведения о точке' }).click()
    await page.locator('.selected-model-caption').waitFor({ state: 'detached', timeout: 10_000 })
    if (diagnosticsAvailable) await page.waitForFunction(() => window.__novoMapTest?.selectedModelState()?.visible === false, null, { timeout: 10_000 })
    if (name === 'mobile-chromium') await page.getByRole('tab', { name: 'Здания' }).click()
    await page.locator('.developer-filter-panel .filter-buttons .text-button').click()
    await page.getByRole('textbox', { name: 'Поиск зданий' }).fill(fallback.name)
    await page.getByRole('button', { name: 'Найти здания' }).click()
    const fallbackCard = page.getByTestId(`building-${fallback.registry_id}`)
    await fallbackCard.waitFor({ timeout: 30_000 })
    const fallbackResponse = page.waitForResponse((response) => response.url().endsWith(`/v1/developer-buildings/${fallback.registry_id}/model`))
    await fallbackCard.locator('button').click()
    assert.equal((await (await fallbackResponse).json()).status, 'unavailable')
    await page.getByRole('button', { name: 'Закрыть сведения о точке' }).waitFor()
    assert.equal(await page.locator('.selected-model-caption').count(), 0, 'unavailable model must not claim a reconstruction')
    assert.doesNotMatch(await page.locator('.map-top').textContent(), /реконструкция выбранного корпуса/i)
    assert.ok(await page.locator('.cesium-widget canvas').first().isVisible(), 'map canvas disappeared for a house without GLB')
    if (diagnosticsAvailable) await page.waitForFunction(() => {
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

async function inspectYasenevsky(registryId, expectedFloors) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  const result = { name: 'yasenevsky-floor-filter', screenshots: {}, page_errors: [] }
  report.views.push(result)
  page.on('pageerror', (error) => result.page_errors.push(error.message))
  try {
    await page.goto(new URL(diagnosticsAvailable ? '/?__map_test=1' : '/', base).toString(), { waitUntil: 'domcontentloaded' })
    const toggle = page.getByRole('button', { name: /Фильтры квартир/ })
    await toggle.waitFor()
    if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
    await page.getByRole('textbox', { name: 'Поиск зданий' }).fill('Ясеневский')
    await page.getByRole('spinbutton', { name: 'Цена до' }).fill('15')
    await page.getByRole('combobox', { name: 'Комнат' }).selectOption('2')
    await page.getByRole('button', { name: 'Показать дома' }).click()
    const card = page.getByTestId(`building-${registryId}`)
    await card.waitFor({ timeout: 30_000 })
    await card.locator('button').click()
    await page.getByTestId('open-developer-explorer').click()
    const dialog = page.getByRole('dialog')
    await dialog.getByRole('heading', { name: 'Этажи с подходящими квартирами' }).waitFor()
    const floorButtons = dialog.locator('[data-floor-id]')
    const expected = expectedFloors.sections.flatMap((section) => section.floors)
    assert.equal(await floorButtons.count(), expected.length, 'Yasenevsky UI floor buttons differ from filtered API')
    assert.ok((await floorButtons.allTextContents()).every((label) => !/^13 этаж\b/.test(label.trim())), 'Yasenevsky empty 13th floor stayed clickable')
    for (const floor of expected) {
      const label = await dialog.locator(`[data-floor-id="${floor.floor_id}"]`).textContent()
      assert.ok(label?.includes(String(floor.unit_count)) && floor.unit_count > 0, 'Yasenevsky floor count is not the matching-unit count')
    }
    result.screenshots.filtered_floors = path.join(output, 'desktop-yasenevsky-filtered-floors.png')
    await page.screenshot({ path: result.screenshots.filtered_floors, fullPage: true })
    assert.deepEqual(result.page_errors, [])
  } catch (error) {
    result.error = error instanceof Error ? error.message : String(error)
    result.screenshots.failure = path.join(output, 'desktop-yasenevsky-failure.png')
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
  if (requireEnrichment) assert.ok(health.enrichment_run_id, 'unified enrichment snapshot is not active')
  const [floors, units, model, parkWalk] = await Promise.all([
    getJson(request, `/v1/developer-buildings/${lakeId}/floors`),
    getJson(request, `/v1/developer-buildings/${lakeId}/units?limit=100`),
    getJson(request, `/v1/developer-buildings/${lakeId}/model`),
    getJson(request, `/v1/developer-buildings/${lakeId}/park-walk`),
  ])
  const sameRun = (value) => assert.ok(value.registry_snapshot_id === health.registry_snapshot_id && value.map_run_id === health.map_run_id && value.unit_run_id === health.unit_run_id
    && (value.enrichment_run_id ?? null) === (health.enrichment_run_id ?? null))
  for (const value of [floors, units, model, parkWalk]) sameRun(value)
  assert.equal(parkWalk.status, 'routed', 'Lake pilot park walking route is unavailable')
  assert.ok(parkWalk.distance_m > 0 && parkWalk.estimated_minutes > 0 && parkWalk.access_verified === false)
  assert.equal(model.status, 'ready')
  assert.equal(model.depiction, 'schematic_massing')
  assert.equal(model.photo_facade_reconstruction, true)
  assert.equal(model.model_transform.length, 16)
  const modelAsset = await request.get(new URL(model.asset_url, base).toString())
  assert.equal(modelAsset.status(), 200, 'selected GLB asset is missing')
  const modelBytes = Buffer.from(await modelAsset.body())
  assert.ok(modelBytes.length > 100_000)
  assert.equal(modelBytes.toString('ascii', 0, 4), 'glTF', 'selected model is not a GLB')
  const gltf = JSON.parse(modelBytes.toString('utf8', 20, 20 + modelBytes.readUInt32LE(12)).trim())
  assert.ok(gltf.images?.length > 0 && gltf.textures?.length > 0 && gltf.materials?.some((material) => material.pbrMetallicRoughness?.baseColorTexture), 'Lake GLB lacks its embedded facade texture')
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
  let enrichedHouse = null
  if (health.enrichment_run_id) {
    const listForMedia = await getJson(request, '/v1/registry/buildings?query=%D0%9B%D1%8D%D0%B9%D0%BA&limit=48')
    assert.equal(listForMedia.enrichment_run_id, health.enrichment_run_id)
    const house = listForMedia.items.find((item) => item.registry_id === lakeId)
    assert.ok(house?.okrug_id && Number.isInteger(house.noise_index) && house.noise_index >= 0 && house.noise_index <= 100, 'Lake geography/noise row is incomplete')
    enrichedHouse = house
    const photo = house?.media?.find((item) => item.scope === 'project_gallery_single_corpus' && item.role === 'facade_photo')
    assert.ok(photo, 'exact Lake photo descriptor is missing')
    const unitDetail = await getJson(request, `/v1/developer-buildings/${lakeId}/units/${sample.unit_id}`)
    sameRun(unitDetail)
    const plan = unitDetail.plans?.find((item) => item.role === 'apartment_plan' || item.role === 'furnished_apartment_plan')
      ?? unitDetail.media?.find((item) => item.scope === 'unit_plan')
    assert.ok(plan, 'exact selected Lake unit plan descriptor is missing')
    const [photoResponse, unitPlanResponse] = await Promise.all([
      request.get(new URL(photo.asset_url, base).toString()), request.get(new URL(plan.asset_url, base).toString()),
    ])
    assert.equal(photoResponse.status(), 200, 'exact Lake photo bytes are unavailable')
    assert.match(photoResponse.headers()['content-type'] ?? '', /^image\/(jpeg|png)/, 'Lake photo MIME is not an image')
    assert.ok((await photoResponse.body()).length > 1_000, 'Lake photo is empty')
    assert.equal(unitPlanResponse.status(), 200, 'exact selected unit plan bytes are unavailable')
    assert.match(unitPlanResponse.headers()['content-type'] ?? '', /^image\/(jpeg|png)/, 'unit plan MIME is not an image')
    assert.ok((await unitPlanResponse.body()).length > 1_000, 'unit plan is empty')
    const geo = new URLSearchParams({ okrug_id: house.okrug_id, max_noise_index: String(house.noise_index) })
    const geoResponses = await Promise.all(['/v1/registry/buildings', '/v1/registry/map', '/v1/map/points'].map((route) => getJson(request, `${route}?${geo}`)))
    for (const response of geoResponses) {
      assert.equal(response.enrichment_run_id, health.enrichment_run_id)
      assert.ok(response.items.some((item) => item.registry_id === lakeId), 'Lake disappeared under its own okrug/noise limit')
      assert.ok(response.items.every((item) => item.okrug_id === house.okrug_id && Number.isInteger(item.noise_index) && item.noise_index <= house.noise_index), 'active noise/okrug filter admitted unknown or excessive score')
    }
  }
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
  for (const value of [list, map, points]) assert.equal(value.enrichment_run_id ?? null, health.enrichment_run_id ?? null)
  const ids = (items) => items.map((item) => item.registry_id).sort()
  assert.deepEqual(ids(map.items), ids(points.items), 'filtered map and point identities differ')
  assert.ok(list.items.some((item) => item.registry_id === lakeId))
  assert.ok(map.items.some((item) => item.registry_id === lakeId))
  assert.ok(filteredMenu.items.some((item) => item.unit_id === sample.unit_id))
  assert.ok(filteredMenu.items.every((item) => item.price_rub <= sample.price_rub && item.floor_id === sample.floor_id))
  const filteredFloors = await getJson(request, `/v1/developer-buildings/${lakeId}/floors?price_max_rub=${sample.price_rub}&unit_floor_min=${sample.floor_number}&unit_floor_max=${sample.floor_number}`)
  const matchingFloors = filteredFloors.sections.flatMap((section) => section.floors)
  assert.ok(matchingFloors.some((floor) => floor.floor_id === sample.floor_id), 'matching Lake floor missing from filtered selector')
  assert.ok(matchingFloors.every((floor) => floor.floor_number === sample.floor_number && floor.unit_count > 0), 'filtered Lake selector promises an empty floor')
  assert.equal(filteredFloors.matching_units_total, filteredFloors.sections.reduce((sum, section) => sum + section.floors.reduce((count, floor) => count + floor.unit_count, 0), filteredFloors.unassigned_unit_count))
  const yasenevskyId = 'registry-df0c5887d92ea0335cdba75c'
  const [yasenevskyAll, yasenevskyFiltered] = await Promise.all([
    getJson(request, `/v1/developer-buildings/${yasenevskyId}/floors`),
    getJson(request, `/v1/developer-buildings/${yasenevskyId}/floors?price_max_rub=15000000&rooms=2`),
  ])
  assert.ok(yasenevskyAll.sections.some((section) => section.floors.some((floor) => floor.floor_number === 13)), 'Yasenevsky source fixture no longer has 13th floor')
  assert.ok(yasenevskyFiltered.matching_units_total > 0, 'Yasenevsky filter unexpectedly has no matching units')
  assert.ok(yasenevskyFiltered.sections.every((section) => section.floors.every((floor) => floor.floor_number !== 13 && floor.unit_count > 0)), 'Yasenevsky 13th floor remained clickable with zero matches')
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
  report.api = { registry_snapshot_id: health.registry_snapshot_id, map_run_id: health.map_run_id, unit_run_id: health.unit_run_id, enrichment_run_id: health.enrichment_run_id ?? null,
    model_run_id: model.model_run_id, lake_id: lakeId, sample_unit_id: sample.unit_id, sample_price_rub: sample.price_rub,
    filtered_list_total: list.records_total, filtered_map_points: map.items.length, filtered_menu_total: filteredMenu.total,
    filtered_floor_count: matchingFloors.length, yasenevsky_filtered_units: yasenevskyFiltered.matching_units_total,
    combined_filter_menu_total: exactResponses[3].total,
    anonymous_plan_regions: floorDetail.plan_regions.length, model_asset_bytes: modelBytes.length, model_embedded_textures: gltf.textures.length,
    park_walk_distance_m: parkWalk.distance_m, park_walk_estimated_minutes: parkWalk.estimated_minutes,
    unavailable_model_registry_id: fallback.registry_id }
  await context.close()
  const expected = { ...health, house: enrichedHouse, model_asset_url: model.asset_url, plan_asset_url: floorDetail.plan.asset_url, park_walk: parkWalk }
  await inspect('desktop', { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 }, sample, expected, fallback)
  const { defaultBrowserType: _browserType, ...mobileViewport } = devices['iPhone 13']
  await inspect('mobile-chromium', mobileViewport, sample, expected, fallback)
  await inspectYasenevsky(yasenevskyId, yasenevskyFiltered)
} catch (error) { report.errors.push(error instanceof Error ? error.stack ?? error.message : String(error)) }
finally {
  await browser.close()
  const destination = path.join(output, 'report.json')
  await writeFile(destination, `${JSON.stringify(report, null, 2)}\n`)
  console.log(destination)
  if (report.errors.length) process.exitCode = 1
}
