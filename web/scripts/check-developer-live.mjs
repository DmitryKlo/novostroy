import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { chromium, devices } from '@playwright/test'

// Read-only check of an already running managed preview. It never starts a server.
const positional = process.argv.slice(2).filter((arg) => arg !== '--public-ui')
const base = new URL(positional[0] ?? 'http://127.0.0.1:5173')
const output = path.resolve(positional[1] ?? `../.runtime/browser-developer-live/${new Date().toISOString().replaceAll(':', '-')}`)
await mkdir(output, { recursive: true })
const report = { base_url: base.origin, checked_at: new Date().toISOString(), views: [], api: {}, errors: [] }
const browser = await chromium.launch({ headless: true })
const diagnosticsAvailable = base.hostname === '127.0.0.1' && !process.argv.includes('--public-ui')
report.validation_mode = diagnosticsAvailable ? 'loopback_diagnostics' : 'public_dom_network'

async function json(request, route) {
  const response = await request.get(new URL(route, base).toString())
  assert.equal(response.status(), 200, `${route}: HTTP ${response.status()}`)
  assert.equal(new URL(response.url()).origin, base.origin, `${route}: unexpected redirect away from deployed origin`)
  return response.json()
}

async function until(label, task, timeout = 25_000) {
  const started = Date.now()
  let last
  while (Date.now() - started < timeout) {
    try {
      const value = await task()
      if (value) return value
    } catch (error) { last = error }
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error(`Timed out waiting for ${label}${last ? `: ${last.message}` : ''}`)
}

async function auditNearby(page, health, point) {
  const nearby = await json(page.request, `/v1/map/nearby?point_id=${encodeURIComponent(point.point_id)}&education=all`)
  assert.equal(nearby.point_id, point.point_id)
  assert.equal(nearby.registry_snapshot_id, health.registry_snapshot_id)
  assert.equal(nearby.map_run_id, health.map_run_id)
  const metro = nearby.layers.metro?.items ?? []
  const stationNames = metro.map((item) => item.name.trim().toLocaleLowerCase('ru-RU'))
  assert.equal(new Set(stationNames).size, stationNames.length, 'same metro station is repeated as separate exits')
  for (const layer of Object.values(nearby.layers)) for (const item of layer.items ?? []) {
    const route = item.route
    if (!route) continue
    if (route.status === 'routed') {
      assert.ok(Number.isFinite(route.distance_m) && route.distance_m >= 0
        && Number.isFinite(route.estimated_minutes) && route.estimated_minutes >= 0
        && Array.isArray(route.geometry) && route.geometry.length >= 2, 'routed POI lacks its walking path')
      if (route.origin_access) {
        assert.equal(route.origin_access.approximate, true)
        assert.ok(Array.isArray(route.origin_access.geometry) && route.origin_access.geometry.length >= 2)
        assert.ok(Array.isArray(route.network_geometry) && route.network_geometry.length >= 2)
      }
    } else assert.ok(route.distance_m == null && route.estimated_minutes == null, 'unavailable route must not fall back to air distance')
  }
  assert.doesNotMatch(await page.locator('.nearby-panel').innerText(), /по прямой/i)
  return { status: nearby.status, map_run_id: nearby.map_run_id, layer_counts: Object.fromEntries(Object.entries(nearby.layers).map(([key, value]) => [key, value.items?.length ?? 0])) }
}

async function inspectViewport(name, options, health, firstPage, points) {
  const context = await browser.newContext(options)
  const page = await context.newPage()
  const result = { name, screenshots: {}, console_errors: [], request_failures: [], asset_http_errors: [], asset_responses: 0 }
  report.views.push(result)
  page.on('pageerror', (error) => result.console_errors.push(error.message))
  page.on('console', (message) => { if (message.type() === 'error') result.console_errors.push(message.text()) })
  page.on('requestfailed', (request) => result.request_failures.push(`${request.method()} ${new URL(request.url()).pathname}: ${request.failure()?.errorText}`))
  page.on('response', (response) => {
    const pathname = new URL(response.url()).pathname
    if (!pathname.startsWith('/assets/') && !pathname.startsWith('/v1/developer-model-assets/') && !pathname.startsWith('/v1/developer-unit-assets/')) return
    result.asset_responses += 1
    if (response.status() >= 400) result.asset_http_errors.push(`${response.status()} ${pathname}`)
  })
  try {
    await page.goto(new URL(diagnosticsAvailable ? '/?__map_test=1' : '/', base).toString(), { waitUntil: 'domcontentloaded' })
    assert.equal(new URL(page.url()).origin, base.origin, 'browser redirected away from deployed origin')
    await page.locator('.registry-card').first().waitFor({ timeout: 30_000 })
    assert.equal(await page.locator('.offer-card').count(), 0, 'developer mode must not show demo offer cards')
    assert.equal(await page.getByRole('link', { name: 'Модели' }).count(), 0, 'old model review must be hidden')
    const filterToggle = page.getByRole('button', { name: /Фильтры квартир/ })
    assert.equal(await filterToggle.count(), 1, 'apartment filter toggle must be present')
    if (await filterToggle.getAttribute('aria-expanded') !== 'true') await filterToggle.click()
    await page.getByRole('spinbutton', { name: 'Цена от' }).waitFor()
    await page.getByRole('spinbutton', { name: 'Цена за м² от' }).waitFor()
    await page.getByRole('spinbutton', { name: 'Площадь от' }).waitFor()
    await page.getByRole('spinbutton', { name: 'Этаж квартиры от' }).waitFor()
    await page.getByRole('combobox', { name: 'Комнат' }).waitFor()
    assert.equal(await page.getByRole('combobox', { name: 'Статус' }).count(), 0, 'unsupported legacy status filter must remain absent')
    result.screenshots.filters = path.join(output, `${name}-filters.png`)
    await page.screenshot({ path: result.screenshots.filters, fullPage: true })

    let target = firstPage.items.find((row) => row.location_quality === 'verified' && points.items.some((point) => point.registry_id === row.registry_id))
    if (!target) {
      const point = points.items[0]
      assert.ok(point, 'published developer snapshot has no verified map points')
      const detail = await json(page.request, `/v1/registry/buildings/${encodeURIComponent(point.registry_id)}`)
      await page.getByRole('textbox', { name: 'Поиск зданий' }).fill(detail.name)
      await page.getByRole('button', { name: 'Найти здания' }).click()
      await page.getByTestId(`building-${point.registry_id}`).waitFor({ timeout: 20_000 })
      target = detail
    }
    const point = points.items.find((item) => item.registry_id === target.registry_id)
    assert.ok(point)
    const card = page.getByTestId(`building-${target.registry_id}`)
    await card.waitFor()
    const cardText = await card.innerText()
    assert.match(cardText, /ЖК:/)
    assert.match(cardText, /Застройщик:/)
    assert.ok(cardText.includes(target.address ?? 'Не указано'), 'list card must show the source address')
    assert.ok(cardText.includes(target.complex_label), 'list card must show the source ЖК')
    assert.ok(cardText.includes(target.developer_name ?? 'Не указано'), 'list card must show the developer')
    if (target.address_complete === false) assert.match(cardText, /частичный адрес/)
    result.list_card = { registry_id: target.registry_id, has_address: true, has_complex: true, has_developer: true, partial_address: target.address_complete === false }
    result.screenshots.list = path.join(output, `${name}-list.png`)
    await page.screenshot({ path: result.screenshots.list, fullPage: true })

    if (!diagnosticsAvailable) {
      await card.locator('button').click()
      await page.getByRole('button', { name: 'Закрыть сведения о точке' }).waitFor({ timeout: 15_000 })
      assert.ok(await page.locator('.cesium-widget canvas').first().isVisible(), 'public Cesium canvas is not visible')
      const selectedText = await page.locator('.map-point-card').last().innerText()
      assert.ok(selectedText.includes(target.address ?? 'Адрес не указан'))
      assert.ok(selectedText.includes(target.complex_label))
      assert.ok(selectedText.includes(target.developer_name ?? 'не указан'))
      assert.match(await page.locator('.map-key').textContent(), /Корпуса с подтверждённой точкой/)
      assert.doesNotMatch(await page.locator('.map-key').textContent(), /Картографические стройки/)
      if (name === 'mobile-chromium') {
        assert.equal(await page.locator('.map-key').isVisible(), false)
        const top = await page.locator('.map-top').boundingBox()
        const controls = await page.locator('.map-controls').boundingBox()
        const selected = await page.locator('.map-point-card').last().boundingBox()
        const nearby = await page.locator('.nearby-panel').boundingBox()
        const bottomNote = await page.locator('.map-bottom-note').boundingBox()
        assert.ok(top && controls && selected && nearby && bottomNote)
        assert.ok(top.x + top.width < controls.x, 'mobile map status overlaps controls')
        assert.ok(nearby.y >= selected.y + selected.height + 8, 'nearby panel overlaps selected house card')
        assert.ok(nearby.y + nearby.height <= bottomNote.y, 'nearby panel covers map attribution')
        assert.equal(await page.locator('.nearby-expand').getAttribute('aria-expanded'), 'false')
      }
      result.nearby = await auditNearby(page, health, point)
      result.screenshots.list_selection = path.join(output, `${name}-list-selection.png`)
      await page.screenshot({ path: result.screenshots.list_selection, fullPage: true })
      if (name === 'mobile-chromium') {
        await page.locator('.nearby-expand').click()
        await page.locator('.nearby-results').waitFor()
        result.screenshots.nearby_expanded = path.join(output, `${name}-nearby-expanded.png`)
        await page.screenshot({ path: result.screenshots.nearby_expanded, fullPage: true })
        await page.locator('.nearby-expand').click()
        await page.getByRole('tab', { name: 'Здания' }).click()
      }
      await page.getByRole('textbox', { name: 'Поиск зданий' }).fill(target.complex_label)
      await page.getByRole('button', { name: 'Найти здания' }).click()
      const filtered = await json(page.request, `/v1/map/points?query=${encodeURIComponent(target.complex_label)}`)
      assert.equal(filtered.dataset_id, 'developer_map_points')
      assert.equal(filtered.map_run_id, health.map_run_id)
      assert.ok(filtered.items.every((item) => item.point_type === 'developer_building'))
      assert.equal(await page.getByRole('button', { name: /Фильтры квартир/ }).count(), 1)
      result.filtered_points = filtered.items.length
      await page.getByRole('link', { name: 'Данные' }).click()
      await page.getByRole('tab', { name: 'Дома из каталога квартир' }).waitFor({ timeout: 15_000 })
      assert.equal(await page.getByRole('tab', { name: 'Демо-предложения' }).count(), 0)
      assert.equal(await page.getByRole('tab', { name: 'Реестр зданий' }).count(), 0)
      result.screenshots.data = path.join(output, `${name}-data.png`)
      await page.screenshot({ path: result.screenshots.data, fullPage: true })
      assert.deepEqual(result.asset_http_errors, [], 'map or prepared asset returned HTTP error')
      assert.deepEqual(result.console_errors, [])
      return
    }

    const mapTest = () => page.evaluate(() => Boolean(window.__novoMapTest?.cameraPose))
    await until('Cesium test hook', mapTest)
    result.camera_before = await page.evaluate(() => window.__novoMapTest.cameraPose())
    await card.locator('button').click()
    await page.getByRole('button', { name: 'Закрыть сведения о точке' }).waitFor({ timeout: 15_000 })
    assert.match(await page.locator('.map-key').textContent(), /Корпуса с подтверждённой точкой/)
    assert.doesNotMatch(await page.locator('.map-key').textContent(), /Картографические стройки/)
    if (name === 'mobile-chromium') {
      assert.equal(await page.locator('.map-key').isVisible(), false, 'mobile developer legend must leave room for the selected point')
      const top = await page.locator('.map-top').boundingBox()
      const controls = await page.locator('.map-controls').boundingBox()
      assert.ok(top && controls && top.x + top.width < controls.x, 'mobile map status must not overlap controls')
    }
    result.camera_after_list = await until('camera focus on selected list building', async () => {
      const pose = await page.evaluate(() => window.__novoMapTest.cameraPose())
      return pose.height < result.camera_before.height - 100 ? pose : null
    }, 20_000)
    const selectedText = await page.locator('.map-point-card').last().innerText()
    assert.ok(selectedText.includes(target.address ?? 'Адрес не указан'))
    assert.ok(selectedText.includes(target.complex_label))
    assert.ok(selectedText.includes(target.developer_name ?? 'не указан'))
    result.screenshots.list_selection = path.join(output, `${name}-list-selection.png`)
    await page.screenshot({ path: result.screenshots.list_selection, fullPage: true })

    result.nearby = await auditNearby(page, health, point)

    await page.getByRole('button', { name: 'Закрыть сведения о точке' }).click()
    const cameraBeforePoint = await page.evaluate(() => window.__novoMapTest.cameraPose())
    await page.getByRole('button', { name: 'Отдалить карту' }).click()
    await page.getByRole('button', { name: 'Отдалить карту' }).click()
    const cameraZoomedOut = await until('camera zooms out before marker selection', async () => {
      const pose = await page.evaluate(() => window.__novoMapTest.cameraPose())
      return pose.height > cameraBeforePoint.height + 150 ? pose : null
    })
    await until('loaded ground and marker', async () => {
      const visibility = await page.evaluate(() => window.__novoMapTest.markerVisibility())
      const targetActive = await page.evaluate((id) => window.__novoMapTest.markerActive(id), point.point_id)
      return targetActive && visibility.activeMarkers > 0 && visibility.lastGroundCoverageCount > 0 ? visibility : null
    }, 30_000)
    const projected = await page.evaluate((id) => window.__novoMapTest.project(id), point.point_id)
    assert.ok(projected, 'selected source point must project on the map')
    const canvas = page.locator('.cesium-widget canvas').first()
    const box = await canvas.boundingBox()
    assert.ok(box && projected[0] > 0 && projected[0] < box.width && projected[1] > 0 && projected[1] < box.height,
      'selected point must be inside the canvas')
    await canvas.click({ position: { x: projected[0], y: projected[1] } })
    await page.getByRole('button', { name: 'Закрыть сведения о точке' }).waitFor({ timeout: 10_000 })
    result.camera_after_point = await until('camera focus on selected map point', async () => {
      const pose = await page.evaluate(() => window.__novoMapTest.cameraPose())
      return pose.height < cameraZoomedOut.height - 100 ? pose : null
    }, 20_000)
    const pointText = await page.locator('.map-point-card').last().innerText()
    assert.ok(pointText.includes(target.address ?? 'Адрес не указан'))
    assert.ok(pointText.includes(target.complex_label))
    assert.ok(pointText.includes(target.developer_name ?? 'не указан'))
    if (name === 'mobile-chromium') {
      const card = await page.locator('.map-point-card').last().boundingBox()
      const nearby = await page.locator('.nearby-panel').boundingBox()
      const bottomNote = await page.locator('.map-bottom-note').boundingBox()
      const switchBar = await page.locator('.mobile-switch').boundingBox()
      const date = await page.locator('.map-point-card-item small').last().boundingBox()
      assert.ok(card && nearby && bottomNote && switchBar && date)
      assert.ok(date.y + date.height <= card.y + card.height + 1, 'selected card must show its observation date')
      assert.ok(nearby.y >= card.y + card.height + 20, 'nearby panel must leave a usable map gap below selected card')
      assert.ok(nearby.y + nearby.height <= bottomNote.y, 'nearby panel must not cover the map attribution')
      assert.ok(bottomNote.y + bottomNote.height <= switchBar.y + 1, 'map attribution must sit above mobile navigation')
      assert.equal(await page.locator('.nearby-expand').getAttribute('aria-expanded'), 'false')
    }
    result.marker_visibility = await page.evaluate(() => window.__novoMapTest.markerVisibility())
    result.screenshots.point_selection = path.join(output, `${name}-point-selection.png`)
    await page.screenshot({ path: result.screenshots.point_selection, fullPage: true })
    if (name === 'mobile-chromium') {
      await page.locator('.nearby-expand').click()
      await page.locator('.nearby-results').waitFor()
      const expandedCard = await page.locator('.map-point-card').last().boundingBox()
      const expandedNearby = await page.locator('.nearby-panel').boundingBox()
      assert.ok(expandedCard && expandedNearby && expandedNearby.y >= expandedCard.y + expandedCard.height + 8,
        'expanded nearby panel must not cover the selected card')
      result.screenshots.nearby_expanded = path.join(output, `${name}-nearby-expanded.png`)
      await page.screenshot({ path: result.screenshots.nearby_expanded, fullPage: true })
      await page.locator('.nearby-expand').click()
    }

    if (name === 'mobile-chromium') await page.getByRole('tab', { name: 'Здания' }).click()
    const query = target.complex_label
    await page.getByRole('textbox', { name: 'Поиск зданий' }).fill(query)
    await page.getByRole('button', { name: 'Найти здания' }).click()
    const filtered = await json(page.request, `/v1/map/points?query=${encodeURIComponent(query)}`)
    assert.equal(filtered.dataset_id, 'developer_map_points')
    assert.equal(filtered.map_run_id, health.map_run_id)
    assert.ok(filtered.items.every((item) => item.point_type === 'developer_building'))
    assert.equal(await page.getByRole('button', { name: /Фильтры квартир/ }).count(), 1)
    result.filtered_points = filtered.items.length

    await page.getByRole('link', { name: 'Данные' }).click()
    await page.getByRole('tab', { name: 'Дома из каталога квартир' }).waitFor({ timeout: 15_000 })
    assert.equal(await page.getByRole('tab', { name: 'Демо-предложения' }).count(), 0)
    assert.equal(await page.getByRole('tab', { name: 'Реестр зданий' }).count(), 0)
    result.screenshots.data = path.join(output, `${name}-data.png`)
    await page.screenshot({ path: result.screenshots.data, fullPage: true })
    assert.deepEqual(result.asset_http_errors, [], 'map or prepared asset returned HTTP error')
    assert.deepEqual(result.console_errors, [])
  } catch (error) {
    result.error = error instanceof Error ? error.message : String(error)
    result.screenshots.failure = path.join(output, `${name}-failure.png`)
    await page.screenshot({ path: result.screenshots.failure, fullPage: true }).catch(() => {})
    throw error
  } finally {
    await context.close()
  }
}

try {
  const request = await browser.newContext()
  const health = await json(request.request, '/v1/health')
  assert.equal(health.catalog_view, 'developer_buildings', 'developer snapshot is not active')
  assert.ok(health.registry_snapshot_id && health.map_run_id)
  const [list, map, points, coverage] = await Promise.all([
    json(request.request, '/v1/registry/buildings?limit=48'),
    json(request.request, '/v1/registry/map'),
    json(request.request, '/v1/map/points'),
    json(request.request, '/v1/data-coverage'),
  ])
  assert.equal(list.dataset_id, 'developer_building_registry')
  assert.equal(map.dataset_id, 'developer_building_registry')
  assert.equal(points.dataset_id, 'developer_map_points')
  assert.equal(list.snapshot_id, map.snapshot_id)
  assert.equal(points.registry_snapshot_id, health.registry_snapshot_id)
  assert.equal(list.snapshot_id, health.registry_snapshot_id)
  assert.equal(list.map_run_id, map.map_run_id)
  assert.equal(points.map_run_id, health.map_run_id)
  assert.equal(list.map_run_id, health.map_run_id)
  assert.equal(list.unit_run_id, map.unit_run_id)
  assert.equal(points.unit_run_id, list.unit_run_id)
  if (health.unit_run_id) assert.equal(list.unit_run_id, health.unit_run_id)
  assert.equal(points.group_evidence.registry_snapshot_id, health.registry_snapshot_id)
  assert.equal(points.group_evidence.map_run_id, health.map_run_id)
  assert.ok(list.records_total > 0 && points.items.length > 0, 'published snapshot has no visible buildings or points')
  assert.equal(list.records_total, map.counts.records)
  assert.equal(map.counts.geocoded, map.items.length)
  assert.equal(map.items.length, points.items.length)
  assert.equal(points.counts.points, points.items.length)
  assert.equal(points.counts.mapped_construction_points, 0)
  assert.ok(points.items.every((item) => item.point_type === 'developer_building' && item.registry_id && !item.candidate_id))
  const pointIds = new Set(points.items.map((item) => item.point_id))
  assert.ok(points.group_evidence.groups.every((group) => group.group_scope === 'residential_complex'
    && group.identity_basis === 'source_complex_id'
    && group.point_ids.every((id) => pointIds.has(id))), 'ЖК groups must use source project identity and published points')
  assert.ok(map.tilesets.some((tile) => tile.kind === 'ground_buildings'), 'city background must retain ground tiles')
  const tileRoots = await Promise.all(map.tilesets.map(async (tile) => {
    const url = new URL(tile.url, base)
    assert.equal(url.origin, base.origin, 'city tileset must use the deployed origin')
    const response = await request.request.get(url.toString())
    assert.equal(response.status(), 200, `missing ${tile.kind} tileset root: ${tile.url}`)
    const body = await response.json()
    assert.ok(body.root && body.asset && Number.isFinite(body.geometricError), `invalid ${tile.kind} tileset root`)
    return { kind: tile.kind, url: tile.url }
  }))
  assert.ok(coverage.datasets.some((item) => item.dataset_id === 'developer_building_registry'))
  assert.ok(!coverage.datasets.some((item) => item.dataset_id === 'building_registry'), 'old registry coverage must be absent')
  const unsupportedFilters = await Promise.all([
    request.request.get(new URL('/v1/registry/buildings?status=building', base).toString()),
    request.request.get(new URL('/v1/map/points?status=building', base).toString()),
  ])
  assert.ok(unsupportedFilters.every((response) => response.status() === 422), 'old status filter must be rejected in developer mode')
  report.api = { snapshot_id: health.registry_snapshot_id, map_run_id: health.map_run_id, unit_run_id: list.unit_run_id,
    list_total: list.records_total, map_total: map.items.length, point_total: points.items.length,
    source_project_groups: points.group_evidence.groups.length,
    city_tilesets: tileRoots, coverage_datasets: coverage.datasets.map((item) => item.dataset_id) }
  await request.close()
  await inspectViewport('desktop', { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 }, health, list, points)
  const { defaultBrowserType: _browserType, ...mobileViewport } = devices['iPhone 13']
  await inspectViewport('mobile-chromium', mobileViewport, health, list, points)
} catch (error) {
  report.errors.push(error instanceof Error ? error.stack ?? error.message : String(error))
} finally {
  await browser.close()
  const destination = path.join(output, 'report.json')
  await writeFile(destination, `${JSON.stringify(report, null, 2)}\n`)
  console.log(destination)
  if (report.errors.length) process.exitCode = 1
}
