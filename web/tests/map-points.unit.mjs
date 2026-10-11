import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

async function importTypeScript(path) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  let javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
  if (path === '../src/mapPoints.ts') {
    const factsSource = await readFile(new URL('../src/developerBuildingFacts.ts', import.meta.url), 'utf8')
    const factsJavascript = ts.transpileModule(factsSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
    const factsUrl = `data:text/javascript;base64,${Buffer.from(factsJavascript).toString('base64')}`
    javascript = javascript.replace("from './developerBuildingFacts'", `from '${factsUrl}'`)
  }
  return import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)
}

const { coincidentPoints, groupMarkerId, pointAction, pointAddressCaption, pointComplexCaption, pointComplexCardCaption, pointDeveloperCardCaption, pointDisplayMarkers, pointMarkers, pointRegistryLinkCaption, unlocatedRegistryCount } = await importTypeScript('../src/mapPoints.ts')
const { groupMapPoints, mapPointDisplayItems } = await importTypeScript('../src/mapPointGroups.ts')
const { api, RegistrySnapshotChanged } = await importTypeScript('../src/api.ts')

const registry = { point_id: 'mappt_' + 'a'.repeat(24), point_type: 'registry', registry_id: 'registry-' + 'b'.repeat(24), candidate_id: null,
  name: 'Корпус А', location: [37.61, 55.75], latest_known_date: '2026-09-06', geometry_as_of: null,
  address: 'улица Примерная, 1', address_basis: 'registry_source', complex_label: 'ЖК Пример', complex_label_basis: 'registry_source_name', known_catalog_registry_id: 'registry-' + 'b'.repeat(24) }
const mapped = { point_id: 'mappt_' + 'c'.repeat(24), point_type: 'mapped_construction', registry_id: null, candidate_id: 'cand_' + 'd'.repeat(24),
  name: 'Строящийся объект на карте', location: [37.61, 55.75], latest_known_date: null, geometry_as_of: '2026-10-08T00:00:00Z',
  address: 'улица Примерная, 1', address_basis: 'osm_street_house', complex_label: null, complex_label_basis: null, known_catalog_registry_id: null }
const developer = { ...registry, point_type: 'developer_building', developer_name: null,
  developer_display_name: 'Застройщик', developer_display_basis: 'aggregator_asserted_unverified',
  observed_at: '2026-10-10T00:00:00Z', unit_count: 3, location_basis: 'publisher_building', address_complete: false }

test('developer point keeps address, ЖК and developer in its card data', () => {
  assert.deepEqual(pointAction(developer), { kind: 'registry', id: developer.registry_id })
  assert.equal(pointAddressCaption(developer), 'Частичный адрес: улица Примерная, 1')
  assert.equal(pointComplexCardCaption(developer, null), 'ЖК: ЖК Пример')
  assert.equal(pointDeveloperCardCaption(developer), 'Застройщик по данным каталога: Застройщик')
  assert.equal(pointDeveloperCardCaption({ ...developer, developer_display_name: null }), 'Застройщик не указан в данных источника')
})

test('coincident source records remain two selectable typed identities', () => {
  const far = { ...mapped, point_id: 'mappt_' + 'e'.repeat(24), location: [37.62, 55.75] }
  const items = [registry, mapped, far]
  assert.deepEqual(coincidentPoints(items, registry.point_id).map((item) => item.point_id), [registry.point_id, mapped.point_id])
  assert.deepEqual(coincidentPoints(items, mapped.point_id).map((item) => item.point_id), [registry.point_id, mapped.point_id])
  assert.deepEqual(coincidentPoints(items, 'mappt_unknown'), [])
  assert.deepEqual(pointAction(registry), { kind: 'registry', id: registry.registry_id })
  assert.deepEqual(pointAction(mapped), { kind: 'candidate', id: mapped.candidate_id })
})

test('address and complex captions use explicit point fields without inferring a cross-source project link', () => {
  assert.equal(pointAddressCaption(registry), 'Адрес по реестру: улица Примерная, 1')
  assert.equal(pointAddressCaption(mapped), 'Адрес на карте: улица Примерная, 1')
  assert.equal(pointComplexCaption(mapped), null, 'same coordinate and address do not borrow the registry label')
  const named = { ...mapped, address: 'ЖК Шагал, к20', address_basis: 'osm_place_house', complex_label: 'ЖК Шагал', complex_label_basis: 'osm_addr_place' }
  assert.equal(pointAddressCaption(named), 'Ориентир на карте: ЖК Шагал, к20')
  assert.equal(pointComplexCaption(named), 'Подпись ЖК на контуре: ЖК Шагал')
  assert.equal(pointComplexCaption(registry), null, 'registry name is already shown on its own card')
  assert.equal(pointAddressCaption({ ...mapped, address: null, address_basis: null }), 'Адрес не указан')
})

test('all point markers stay independent from list paging and exclude geometry URLs', () => {
  const items = [
    ...Array.from({ length: 10 }, (_, index) => ({ ...registry, point_id: `mappt_${(1000 + index).toString(16).padStart(24, '0')}`, registry_id: `registry-${index.toString(16).padStart(24, '0')}`, location: [37.61 + index / 1000, 55.75 + index / 1000] })),
    ...Array.from({ length: 756 }, (_, index) => ({ ...mapped, point_id: `mappt_${index.toString(16).padStart(24, '0')}`, candidate_id: `cand_${index.toString(16).padStart(24, '0')}`, location: [37.61 + index / 10000, 55.75] })),
  ]
  const markers = pointMarkers({ items })
  assert.equal(markers.length, 766)
  assert.equal(markers[0].point_type, 'registry')
  assert.equal(markers[10].point_type, 'mapped_construction')
  assert.equal(markers.some((marker) => 'model_tileset' in marker || 'footprint' in marker), false)
  assert.equal(unlocatedRegistryCount(1473, 10), 1463)
  assert.equal(unlocatedRegistryCount(8, 2), 6, 'filtered coverage uses filtered registry count')
})

test('verified group becomes one visual marker at overview and original typed points after click or zoom', () => {
  const next = { ...mapped, point_id: 'mappt_' + 'f'.repeat(24), location: [37.611, 55.75] }
  const pointSet = { registry_snapshot_id: 'snapshot-a', model_run_id: 'run-a', items: [mapped, next, registry] }
  const evidence = { schema_version: 1, visibility: 'public', registry_snapshot_id: 'snapshot-a', model_run_id: 'run-a',
    groups: [{ group_id: 'group_' + 'e'.repeat(24), group_scope: 'residential_complex',
      identity_basis: 'explicit_relation_membership', group_label: 'ЖК Проверенный', point_ids: [mapped.point_id, next.point_id] }] }
  const grouping = groupMapPoints(pointSet, evidence)
  const group = grouping.groups[0]
  const overview = pointDisplayMarkers(mapPointDisplayItems(grouping))
  assert.deepEqual(overview.map((item) => item.id), [groupMarkerId(group), mapped.point_id, next.point_id, registry.point_id])
  assert.equal(overview[0].point_type, 'complex_group')
  assert.deepEqual(overview[0].group_member_locations, [mapped.location, next.location])
  assert.equal(overview[1].fallback_group_id, groupMarkerId(group))
  assert.equal(overview[2].fallback_group_id, groupMarkerId(group))
  assert.equal(pointAction(mapped).id, mapped.candidate_id, 'visual group is never a routing or detail origin')
  assert.equal(pointComplexCardCaption(mapped, group), 'Подтверждённый ЖК: ЖК Проверенный')
  assert.equal(pointRegistryLinkCaption(mapped, group), 'Связь с реестром не подтверждена', 'source relation membership is not a catalog crosswalk')
  assert.equal(pointRegistryLinkCaption(mapped, { ...group, identity_basis: 'verified_crosswalk' }), 'Связь с реестром подтверждена')
  assert.equal(pointComplexCardCaption(registry, group), 'Название ЖК не указано в источнике', 'unrelated coincident registry point cannot borrow a group label')
  for (const options of [{ selected_group_id: group.group_id }, { expanded_group_ids: [group.group_id] }, { selected_point_id: mapped.point_id }]) {
    const expanded = pointDisplayMarkers(mapPointDisplayItems(grouping, options))
    assert.deepEqual(expanded.map((item) => item.id), [mapped.point_id, next.point_id, registry.point_id])
  }
  const stale = pointDisplayMarkers(mapPointDisplayItems(groupMapPoints(pointSet, { ...evidence, model_run_id: 'stale' })))
  assert.deepEqual(stale.map((item) => item.id), [mapped.point_id, next.point_id, registry.point_id])
})

test('catalog filter requests one compact point set alongside registry list/map, never candidate detail', async () => {
  const oldFetch = globalThis.fetch
  const calls = []
  const snapshot = 'registry-snapshot-a'
  globalThis.fetch = async (url) => {
    calls.push(String(url))
    const path = String(url).split('?')[0]
    const body = path === '/v1/registry/buildings' ? { dataset_id: 'building_registry', snapshot_id: snapshot, snapshot_at: '2026-10-09', dataset_total: 1473, records_total: 4, items: Array.from({ length: 4 }, (_, index) => ({ registry_id: `registry-${index}` })) }
      : path === '/v1/registry/map' ? { dataset_id: 'building_registry', snapshot_id: snapshot, snapshot_at: '2026-10-09', center: [37.62, 55.75], items: [], counts: { records: 4, geocoded: 1 }, tilesets: [] }
        : { dataset_id: 'map_points', registry_snapshot_id: snapshot, model_run_id: 'run-a', snapshot_at: '2026-10-09', checked_at: '2026-10-09T12:00:00Z', registry_rights_status: 'user_supplied_internal_use', mapped_attribution: '© OpenStreetMap contributors; ODbL 1.0', dataset_total: 766, counts: { points: 1, registry_points: 1, registry_points_total: 10, mapped_construction_points: 0, coordinate_positions: 1, excluded_mapped_by_registry_filters: 756 }, applied_filters: { status: 'Строится' }, items: [registry] }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    const result = await api.registryPointView({ status: 'Строится' }, snapshot)
    assert.equal(result.list.items.length, 4)
    assert.equal(result.points.dataset_total, 766)
    assert.equal(result.points.counts.excluded_mapped_by_registry_filters, 756)
    assert.equal(calls.length, 3)
    assert.ok(calls.some((url) => url.startsWith('/v1/map/points?status=')))
    assert.ok(calls.some((url) => url.includes('/v1/registry/buildings?') && url.includes('limit=48')))
    assert.equal(calls.some((url) => url.includes('/geometry') || url.includes('/v1/models/candidates/')), false)
  } finally { globalThis.fetch = oldFetch }
})

test('point snapshot mismatch cannot pair markers with an unrelated registry list', async () => {
  const oldFetch = globalThis.fetch
  globalThis.fetch = async (url) => {
    const path = String(url).split('?')[0]
    const body = path === '/v1/map/points' ? { dataset_id: 'map_points', registry_snapshot_id: 'wrong', model_run_id: 'run-b' }
      : path === '/v1/registry/map' ? { dataset_id: 'building_registry', snapshot_id: 'expected', items: [], tilesets: [], counts: { records: 0, geocoded: 0 } }
        : { dataset_id: 'building_registry', snapshot_id: 'expected', items: [], records_total: 0 }
    return new Response(JSON.stringify(body), { status: 200 })
  }
  try { await assert.rejects(api.registryPointView({}, 'expected'), RegistrySnapshotChanged) }
  finally { globalThis.fetch = oldFetch }
})

test('developer registry rejects legacy point sets and accepts only its own map run', async () => {
  const oldFetch = globalThis.fetch
  let pointBody = { dataset_id: 'developer_map_points', registry_snapshot_id: 'new-snapshot',
    map_run_id: 'map-run-a', counts: { mapped_construction_points: 0 }, items: [developer] }
  globalThis.fetch = async (url) => {
    const path = String(url).split('?')[0]
    const body = path === '/v1/registry/buildings'
      ? { dataset_id: 'developer_building_registry', snapshot_id: 'new-snapshot', map_run_id: 'map-run-a', items: [] }
      : path === '/v1/registry/map'
        ? { dataset_id: 'developer_building_registry', snapshot_id: 'new-snapshot', map_run_id: 'map-run-a', items: [], tilesets: [] }
        : pointBody
    return new Response(JSON.stringify(body), { status: 200 })
  }
  try {
    const result = await api.registryPointView({}, 'new-snapshot', undefined, 'developer_buildings', 'map-run-a')
    assert.equal(result.points.map_run_id, 'map-run-a')
    pointBody = { ...pointBody, items: [mapped] }
    await assert.rejects(api.registryPointView({}, 'new-snapshot', undefined, 'developer_buildings', 'map-run-a'), RegistrySnapshotChanged)
    pointBody = { ...pointBody, dataset_id: 'map_points', items: [developer] }
    await assert.rejects(api.registryPointView({}, 'new-snapshot', undefined, 'developer_buildings', 'map-run-a'), RegistrySnapshotChanged)
    pointBody = { ...pointBody, dataset_id: 'developer_map_points', map_run_id: '' }
    await assert.rejects(api.registryPointView({}, 'new-snapshot', undefined, 'developer_buildings', 'map-run-a'), RegistrySnapshotChanged)
  } finally { globalThis.fetch = oldFetch }
})
