import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/markerVisibility.ts', import.meta.url), 'utf8')
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { collapseCoveredGroupMembers, markerEntityChanges, markerIdsOnLoadedGround } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)

const center = [100, 200, 300]
const point = (id, x, y, world = center, front = true, longitude = 0.65, latitude = 0.97) => ({ id, x, y, world, front, longitude, latitude })
const tile = (epoch, radius = 100, at = center, region = [0.64, 0.96, 0.66, 0.98], geometricError = 2) => ({ epoch, center: at, radius, region, geometricError })
const view = { epoch: 7, width: 390, height: 610, visible: true, requirePrecise: true }

test('points appear only under a rendered loaded ground tile in the current camera epoch', () => {
  const points = [point('inside', 190, 300), point('offscreen', 500, 300),
    point('behind', 190, 300, center, false), point('uncovered', 200, 300, [1000, 1000, 1000]),
    point('sphere-only', 200, 300, center, true, 0.68, 0.97)]
  assert.deepEqual([...markerIdsOnLoadedGround(points, [tile(6)], view)], [], 'stale camera coverage cannot reveal points')
  assert.deepEqual([...markerIdsOnLoadedGround(points, [tile(7)], view)], ['inside'])
  assert.deepEqual([...markerIdsOnLoadedGround(points, [tile(7, 100, center, null)], view)], [],
    'unknown bounding volume cannot claim verified ground coverage')
  assert.deepEqual([...markerIdsOnLoadedGround(points, [tile(7)], { ...view, visible: false })], [], 'hidden map has no selectable point glyphs')
})

test('one failed or pending child does not hold back points under another loaded visible tile', () => {
  const points = [point('loaded', 120, 100, [100, 200, 300]), point('pending', 220, 100, [1000, 200, 300])]
  assert.deepEqual([...markerIdsOnLoadedGround(points, [tile(7, 120)], view)], ['loaded'])
  assert.deepEqual([...markerIdsOnLoadedGround(points, [tile(7, 120), tile(7, 120, [1000, 200, 300])], view)], ['loaded', 'pending'])
})

test('nearby point waits for regional vector ground even when common overview raster is drawn', () => {
  const marker = point('near', 190, 300)
  assert.deepEqual([...markerIdsOnLoadedGround([marker], [tile(7, 100, center, undefined, 94)], view)], [])
  assert.deepEqual([...markerIdsOnLoadedGround([marker], [tile(7, 100, center, undefined, 94)], { ...view, requirePrecise: false })], ['near'])
})

test('a visual ЖК group cannot replace two points until loaded ground covers both members', () => {
  const group = { ...point('group:verified', 190, 300), groupMembers: [
    { world: center, longitude: 0.65, latitude: 0.97 },
    { world: [1000, 200, 300], longitude: 0.65, latitude: 0.97 },
  ] }
  assert.deepEqual([...markerIdsOnLoadedGround([group], [tile(7)], view)], [], 'anchor alone cannot claim full group coverage')
  assert.deepEqual([...markerIdsOnLoadedGround([group], [tile(7), tile(7, 100, [1000, 200, 300])], view)], ['group:verified'])
  const markers = [{ id: 'group:verified' }, { id: 'member-a', fallback_group_id: 'group:verified' },
    { id: 'member-b', fallback_group_id: 'group:verified' }]
  assert.deepEqual([...collapseCoveredGroupMembers(new Set(['member-a']), markers)], ['member-a'],
    'partially loaded group keeps its covered individual point')
  assert.deepEqual([...collapseCoveredGroupMembers(new Set(['group:verified', 'member-a', 'member-b']), markers)], ['group:verified'],
    'complete group coverage replaces members with one visual marker')
})

test('hidden or no-ground frame removes selected glyph, stable visible frame does not recreate it', () => {
  const active = new Set(['selected', 'other'])
  const hiddenDesired = markerIdsOnLoadedGround([point('selected', 190, 300)], [tile(7)], { ...view, visible: false })
  assert.deepEqual(markerEntityChanges(active, hiddenDesired), { remove: ['selected', 'other'], add: [] })
  assert.deepEqual(markerEntityChanges(active, active), { remove: [], add: [] })
  assert.deepEqual(markerEntityChanges(active, new Set(['selected'])), { remove: ['other'], add: [] })
})
