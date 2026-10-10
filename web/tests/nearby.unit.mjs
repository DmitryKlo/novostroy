import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/nearby.ts', import.meta.url), 'utf8')
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { boundedNearbyBbox, displayNearbyItems, NearbyCache, nearbyKey, nearbyViewportKey, noNearbyLayers, shownNearbyItems, validNearbyResult, validNearbyViewport, validRouteGeometry, validNetworkRouteGeometry, validOriginAccessGeometry, originAccessCaption, walkingCaption, nearbyWalkingCaption } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)

const item = (id, route = null) => ({ id, name: id, category: 'school', coordinate: { lon: 37.6, lat: 55.7 },
  coordinate_basis: 'source_point', straight_line_m: 300, route, free_oms: { status: 'unknown', scope: null } })
const response = (pointId = 'mappt_a', education = 'all') => ({ status: 'ok', point_id: pointId, education,
  registry_snapshot_id: 'registry-a', model_run_id: 'run-a', layers: {
    metro: { items: Array.from({ length: 8 }, (_, index) => item(`metro-${index}`)) },
    education: { items: Array.from({ length: 8 }, (_, index) => item(`school-${index}`)) },
    medical: { items: Array.from({ length: 8 }, (_, index) => item(`clinic-${index}`)) },
  } })

test('nearby remains hidden until enabled and caps each facility kind at two', () => {
  assert.deepEqual(shownNearbyItems(response(), noNearbyLayers()), [])
  assert.equal(shownNearbyItems(response(), { metro: true, education: true, medical: true }).length, 6)
  assert.equal(shownNearbyItems(response(), { metro: false, education: true, medical: false }).length, 2)
  const mixed = [...Array.from({ length: 5 }, (_, index) => item(`school-${index}`)),
    ...Array.from({ length: 5 }, (_, index) => ({ ...item(`kindergarten-${index}`), category: 'kindergarten' }))]
  assert.deepEqual(displayNearbyItems('education', mixed).map((value) => value.id),
    ['school-0', 'school-1', 'kindergarten-0', 'kindergarten-1'])
})

test('only routed geometry earns walking time; straight-line distance never creates an estimate', () => {
  assert.equal(walkingCaption(item('none')), 'Пеший маршрут пока не подтверждён')
  const airOnly = item('air', { status: 'no_route', distance_m: null, estimated_minutes: null, geometry: null })
  assert.equal(walkingCaption(airOnly), 'Пеший маршрут пока не подтверждён')
  const routed = item('route', { status: 'routed', distance_m: 812, estimated_minutes: 11.3, geometry: [[37.6, 55.7], [37.61, 55.71]] })
  assert.match(walkingCaption(routed), /12 мин пешком/)
  assert.match(walkingCaption(item('ferry', { status: 'contains_ferry' })), /включает паром/)
  assert.equal(validRouteGeometry(routed).length, 2)
  assert.equal(validRouteGeometry(item('invalid', { ...routed.route, geometry: [[37.6, 55.7], [190, 55.7]] })), null)
})

test('metro is far only for a confirmed walking route above 40 minutes', () => {
  const routed = (minutes) => item('metro', { status: 'routed', distance_m: 3000, estimated_minutes: minutes,
    geometry: [[37.6, 55.7], [37.61, 55.71]] })
  assert.match(nearbyWalkingCaption('metro', routed(40)), /40 мин пешком/)
  assert.equal(nearbyWalkingCaption('metro', routed(41)), 'Метро далеко')
  assert.match(nearbyWalkingCaption('education', routed(41)), /41 мин пешком/)
  assert.equal(nearbyWalkingCaption('metro', item('unknown')), 'Пеший маршрут пока не подтверждён')
  assert.equal(nearbyWalkingCaption('metro', item('no-route', { status: 'no_route', estimated_minutes: null })), 'Пеший маршрут пока не подтверждён')
})

test('estimated access to the street stays separate from the routed network path', () => {
  const route = { status: 'routed', distance_m: 950, estimated_minutes: 13.2,
    geometry: [[37.6, 55.7], [37.6005, 55.7005], [37.61, 55.71]],
    network_geometry: [[37.6005, 55.7005], [37.61, 55.71]],
    network_distance_m: 820, network_estimated_minutes: 11.5,
    origin_access: { status: 'estimated', approximate: true,
      geometry: [[37.6, 55.7], [37.6005, 55.7005]], distance_m: 130, estimated_minutes: 1.7 } }
  const poi = item('split', route)
  assert.deepEqual(validNetworkRouteGeometry(poi), route.network_geometry)
  assert.deepEqual(validOriginAccessGeometry(poi), route.origin_access.geometry)
  assert.match(walkingCaption(poi), /≈ 14 мин пешком · 950 м/)
  assert.match(originAccessCaption(poi), /Примерный подход до улицы: ≈ 130 м/)
  assert.equal(validOriginAccessGeometry(item('bad', { ...route,
    origin_access: { ...route.origin_access, geometry: [[37.6, 55.7], [190, 55.7]] } })), null)
  assert.equal(validNetworkRouteGeometry(item('bad', { ...route, network_geometry: null })), null,
    'combined geometry cannot become a falsely fully routed line')
})

test('nearby cache and response guards isolate selected point, education and snapshots', () => {
  const cache = new NearbyCache(2, 100)
  const first = nearbyKey('registry-a', 'run-a', 'mappt_a', 'all')
  const second = nearbyKey('registry-a', 'run-a', 'mappt_a', 'school')
  const newer = nearbyKey('registry-b', 'run-a', 'mappt_a', 'all')
  cache.set(first, response(), 0)
  assert.equal(cache.get(first, 50)?.point_id, 'mappt_a')
  assert.equal(cache.get(second, 50), null)
  assert.equal(cache.get(newer, 50), null)
  assert.equal(cache.get(first, 100), null)
  cache.set(first, response(), 101)
  cache.set(second, response('mappt_a', 'school'), 101)
  cache.set(newer, response(), 101)
  assert.equal(cache.get(first, 102), null, 'bounded LRU drops oldest entry')
  assert.equal(validNearbyResult(response(), 'mappt_a', 'all', 'registry-a', 'run-a'), true)
  assert.equal(validNearbyResult(response(), 'mappt_b', 'all', 'registry-a', 'run-a'), false)
  assert.equal(validNearbyResult(response(), 'mappt_a', 'school', 'registry-a', 'run-a'), false)
  assert.equal(validNearbyResult(response(), 'mappt_a', 'all', 'registry-b', 'run-a'), false)
  const tiny = new NearbyCache(12, 100, 10)
  tiny.set(first, response(), 0)
  assert.equal(tiny.get(first, 1), null, 'oversized routed payload cannot enter the in-session cache')
})

test('developer nearby responses require map_run_id and cannot reuse legacy model routes', () => {
  const current = { ...response(), map_run_id: 'map-run-a' }
  assert.equal(validNearbyResult(current, 'mappt_a', 'all', 'registry-a', 'map-run-a', true), true)
  assert.equal(validNearbyResult(response(), 'mappt_a', 'all', 'registry-a', 'run-a', true), false)
  assert.equal(validNearbyResult(current, 'mappt_a', 'all', 'registry-a', 'map-run-b', true), false)
  assert.notEqual(nearbyKey('registry-a', 'map-run-a', 'mappt_a', 'all'),
    nearbyKey('registry-a', 'map-run-b', 'mappt_a', 'all'))
})

test('viewport requests stay inside the bounded district window and reject stale filters', () => {
  assert.equal(boundedNearbyBbox([37.4, 55.7, 37.7, 55.8]), null, 'citywide view cannot request all POIs')
  const bbox = boundedNearbyBbox([37.60001, 55.70001, 37.62001, 55.72001])
  assert.deepEqual(bbox, [37.6, 55.7, 37.6201, 55.7201])
  const layers = ['metro', 'education']
  const key = nearbyViewportKey('registry-a', 'run-a', bbox, layers, 'school')
  assert.notEqual(key, nearbyViewportKey('registry-a', 'run-a', bbox, ['metro'], 'school'))
  const value = { status: 'ok', registry_snapshot_id: 'registry-a', model_run_id: 'run-a', education: 'school', bbox,
    layers: { metro: { items: [] }, education: { items: [] } } }
  assert.equal(validNearbyViewport(value, bbox, layers, 'school', 'registry-a', 'run-a'), true)
  assert.equal(validNearbyViewport(value, bbox, layers, 'all', 'registry-a', 'run-a'), false)
  assert.equal(validNearbyViewport(value, bbox, layers, 'school', 'registry-b', 'run-a'), false)
  assert.equal(validNearbyViewport(value, bbox, ['metro', 'medical'], 'school', 'registry-a', 'run-a'), false)
})
