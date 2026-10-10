import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

async function importTypeScript(path) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
  return import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)
}

const { expandGroupsForCameraHeight, groupMapPoints, mapPointDisplayItems, parseMapPointGroupEvidenceBundle, projectMapPointGroups, MAX_COMPLEX_GROUP_DIAMETER_M } = await importTypeScript('../src/mapPointGroups.ts')

test('true ЖК groups collapse at block scale with hysteresis and selected group reveals original points', () => {
  assert.equal(expandGroupsForCameraHeight(true, 2_400), true)
  assert.equal(expandGroupsForCameraHeight(true, 2_500), false)
  assert.equal(expandGroupsForCameraHeight(false, 2_000), false)
  assert.equal(expandGroupsForCameraHeight(false, 1_800), true)
  assert.equal(expandGroupsForCameraHeight(false, Number.NaN), false)
  const grouped = groupMapPoints(points, bundle)
  assert.deepEqual(mapPointDisplayItems(grouped, { expanded_group_ids: [] }).map((item) => item.kind), ['complex_group', 'point'])
  assert.deepEqual(mapPointDisplayItems(grouped, { expanded_group_ids: [], selected_group_id: group.group_id }).map((item) => item.kind), ['point', 'point', 'point'])
})
const first = { point_id: `mappt_${'a'.repeat(24)}`, point_type: 'mapped_construction', candidate_id: 'way/1/v1', registry_id: null, name: 'Object 1', location: [37.61, 55.75] }
const second = { ...first, point_id: `mappt_${'b'.repeat(24)}`, candidate_id: 'way/2/v1', name: 'Object 2', location: [37.611, 55.75] }
const other = { ...first, point_id: `mappt_${'c'.repeat(24)}`, candidate_id: 'way/3/v1', name: 'Object 3', location: [37.7, 55.75] }
const points = { registry_snapshot_id: 'registry-snapshot-a', model_run_id: 'run-a', items: [first, second, other] }
const group = { group_id: `group_${'1'.repeat(24)}`, group_scope: 'residential_complex', identity_basis: 'explicit_relation_membership', group_label: 'ЖК Source name', point_ids: [first.point_id, second.point_id] }
const bundle = { schema_version: 1, visibility: 'public', registry_snapshot_id: 'registry-snapshot-a', model_run_id: 'run-a', groups: [group] }

test('current source-backed membership collapses only its listed members and exposes a visual-only anchor', () => {
  const result = groupMapPoints(points, bundle)
  assert.equal(result.groups.length, 1)
  assert.equal(result.groups[0].group_id, group.group_id, 'keep the opaque stable ID')
  assert.equal(result.groups[0].group_label, group.group_label)
  assert.deepEqual(result.groups[0].member_ids, [first.point_id, second.point_id])
  assert.equal(result.groups[0].member_count, 2)
  assert.equal(result.groups[0].visual_anchor_basis, 'derived_from_members_for_visual_placement_only')
  assert.deepEqual(result.individuals.map((point) => point.point_id), [other.point_id])
})

test('public-safe projection loader requires schema, visibility and current run IDs', () => {
  assert.deepEqual(projectMapPointGroups(points, bundle).groups[0].member_ids, [first.point_id, second.point_id])
  assert.equal(parseMapPointGroupEvidenceBundle({ ...bundle, visibility: 'internal' }), null)
  assert.equal(parseMapPointGroupEvidenceBundle({ ...bundle, schema_version: 2 }), null)
  assert.equal(parseMapPointGroupEvidenceBundle({ ...bundle, groups: 'not-an-array' }), null)
  assert.deepEqual(parseMapPointGroupEvidenceBundle({ ...bundle, groups: [{ ...group, group_id: 'osm:relation:700@v2' }] }).groups, [])
  assert.equal(parseMapPointGroupEvidenceBundle({ groups: [] }), null)
})

test('snapshot or model-run mismatch leaves every point independent', () => {
  assert.deepEqual(groupMapPoints(points, { ...bundle, model_run_id: 'stale-run' }), { groups: [], individuals: points.items })
  assert.deepEqual(groupMapPoints(points, { ...bundle, registry_snapshot_id: 'stale-snapshot' }), { groups: [], individuals: points.items })
})

test('developer ЖК membership is scoped to its immutable map run', () => {
  const developerPoints = { dataset_id: 'developer_map_points', registry_snapshot_id: 'developer-snapshot',
    map_run_id: 'map-run-a', items: [{ ...first, point_type: 'developer_building' },
      { ...second, point_type: 'developer_building' }] }
  const evidence = { ...bundle, registry_snapshot_id: 'developer-snapshot', map_run_id: 'map-run-a',
    groups: [{ ...group, identity_basis: 'source_complex_id' }] }
  delete evidence.model_run_id
  assert.equal(groupMapPoints(developerPoints, evidence).groups.length, 1)
  assert.deepEqual(groupMapPoints(developerPoints, { ...evidence, groups: [group] }),
    { groups: [], individuals: developerPoints.items }, 'developer groups require source project identity')
  assert.deepEqual(groupMapPoints(developerPoints, { ...evidence, map_run_id: 'map-run-b' }),
    { groups: [], individuals: developerPoints.items })
  assert.deepEqual(groupMapPoints(developerPoints, { ...bundle, registry_snapshot_id: 'developer-snapshot' }),
    { groups: [], individuals: developerPoints.items }, 'legacy model-run evidence cannot group new buildings')
})

test('label equality and proximity do not create membership without explicit evidence', () => {
  const sameLabel = points.items.map((point) => ({ ...point, complex_label: 'ЖК Source name' }))
  assert.deepEqual(groupMapPoints({ ...points, items: sameLabel }, null), { groups: [], individuals: sameLabel })
  assert.deepEqual(groupMapPoints(points, { ...bundle, groups: [] }), { groups: [], individuals: points.items })
})

test('singletons, missing members, and non-complex evidence remain independent', () => {
  for (const invalidGroup of [
    { ...group, point_ids: [first.point_id] },
    { ...group, point_ids: [first.point_id, 'missing-point'] },
    { ...group, group_scope: 'building' },
    { ...group, identity_basis: 'source_label' },
  ]) {
    const result = groupMapPoints(points, { ...bundle, groups: [invalidGroup] })
    assert.equal(result.groups.length, 0)
    assert.deepEqual(result.individuals, points.items)
  }
})

test('group membership is stable across list order and retains members for selection or zoom expansion', () => {
  const original = groupMapPoints(points, bundle).groups[0]
  const reordered = groupMapPoints({ ...points, items: [other, second, first] }, bundle).groups[0]
  assert.equal(reordered.group_id, original.group_id)
  assert.equal(reordered.member_count, original.member_count)
  assert.deepEqual(reordered.member_ids, original.member_ids)
  assert.deepEqual(reordered.members.map((point) => point.point_id), [first.point_id, second.point_id])
})

test('overview uses one group marker and zoom or selection expands to original point IDs', () => {
  const grouping = groupMapPoints(points, bundle)
  const overview = mapPointDisplayItems(grouping)
  assert.deepEqual(overview.map((item) => item.kind), ['complex_group', 'point'])
  assert.equal(overview[0].group.group_id, group.group_id)

  for (const expanded of [
    mapPointDisplayItems(grouping, { expanded_group_ids: [group.group_id] }),
    mapPointDisplayItems(grouping, { selected_group_id: group.group_id }),
    mapPointDisplayItems(grouping, { selected_point_id: first.point_id }),
  ]) {
    assert.deepEqual(expanded.map((item) => item.kind), ['point', 'point', 'point'])
    assert.deepEqual(expanded.slice(0, 2).map((item) => item.point.point_id), [first.point_id, second.point_id])
  }
})

test('conflicting memberships or repeated group IDs fail closed for all claimed points', () => {
  const competing = { ...group, group_id: `group_${'2'.repeat(24)}`, point_ids: [first.point_id, other.point_id] }
  const result = groupMapPoints(points, { ...bundle, groups: [group, competing] })
  assert.equal(result.groups.length, 0)
  assert.deepEqual(result.individuals, points.items)

  const duplicateId = groupMapPoints(points, { ...bundle, groups: [group, { ...group, point_ids: [first.point_id, other.point_id] }] })
  assert.equal(duplicateId.groups.length, 0)
  assert.deepEqual(duplicateId.individuals, points.items)
})

test('duplicate point IDs or invalid coordinates are not collapsed', () => {
  const duplicatePoint = { ...second, point_id: first.point_id }
  const duplicate = groupMapPoints({ ...points, items: [first, duplicatePoint] }, { ...bundle, groups: [{ ...group, point_ids: [first.point_id, second.point_id] }] })
  assert.equal(duplicate.groups.length, 0)
  assert.equal(duplicate.individuals.length, 2)

  const invalid = { ...second, location: [Number.NaN, 55.75] }
  const badCoordinate = groupMapPoints({ ...points, items: [first, invalid] }, { ...bundle, groups: [{ ...group, point_ids: [first.point_id, second.point_id] }] })
  assert.equal(badCoordinate.groups.length, 0)
  assert.equal(badCoordinate.individuals.length, 2)
})

test('implausibly wide group does not produce a whole-city fit target', () => {
  const far = { ...second, location: [37.61 + 0.2, 55.75] }
  const result = groupMapPoints({ ...points, items: [first, far] }, { ...bundle, groups: [{ ...group, point_ids: [first.point_id, far.point_id] }] })
  assert.ok(MAX_COMPLEX_GROUP_DIAMETER_M < 25_000)
  assert.equal(result.groups.length, 0)
  assert.deepEqual(result.individuals, [first, far])
})

test('the current audited point set without group evidence stays fully independent', () => {
  const auditedPoints = Array.from({ length: 766 }, (_, index) => ({
    ...first,
    point_id: `mappt_${index.toString(16).padStart(24, '0')}`,
    candidate_id: `candidate-${index}`,
  }))
  const result = groupMapPoints({ ...points, items: auditedPoints }, {
    schema_version: 1,
    visibility: 'public',
    registry_snapshot_id: points.registry_snapshot_id,
    model_run_id: points.model_run_id,
    groups: [],
  })
  assert.equal(result.groups.length, 0)
  assert.equal(result.individuals.length, 766)
  const emptySidecar = { schema_version: 1, visibility: 'public', registry_snapshot_id: points.registry_snapshot_id, model_run_id: points.model_run_id, groups: [] }
  assert.equal(parseMapPointGroupEvidenceBundle(emptySidecar).groups.length, 0)
  assert.equal(projectMapPointGroups({ ...points, items: auditedPoints }, emptySidecar).individuals.length, 766)
})
