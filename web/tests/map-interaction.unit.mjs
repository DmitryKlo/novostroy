import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

async function importTypeScript(path) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
  return import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)
}

const { resolveMapPick, selectionFocusKey, shouldFlyToSelection, shouldStartInitialCameraFlight } = await importTypeScript('../src/mapInteraction.ts')
const { AdaptiveQuality, probeStartAllowed } = await importTypeScript('../src/adaptiveQuality.ts')

test('point wins over a 3D facade or route in the same pick stack', () => {
  const facade = { primitive: { tile: true } }
  const route = { id: { id: 'nearby-route:poi_1' } }
  const point = { id: { id: 'complex:mappt_abc' } }
  const lookup = (object) => object === facade.primitive ? 'demo-complex' : undefined
  assert.deepEqual(resolveMapPick([route, facade, point], null, lookup), { kind: 'marker', id: 'mappt_abc' })
  assert.deepEqual(resolveMapPick([route, facade], null, lookup), { kind: 'facade', id: 'demo-complex' })
  assert.equal(resolveMapPick([route], null, lookup), null, 'a route itself never becomes a selectable building')
})

test('registry marker data cannot launch a delayed startup flight over an early point click', () => {
  assert.equal(shouldStartInitialCameraFlight(true, false), false)
  assert.equal(shouldStartInitialCameraFlight(true, true), false)
  assert.equal(shouldStartInitialCameraFlight(false, false), true, 'demo featured facade may still frame once')
  assert.equal(shouldStartInitialCameraFlight(false, true), false, 'explicit selection or camera move owns the view')
})

test('nearby point stays selectable alone, while an overlapped map point takes priority', () => {
  const lookup = () => undefined
  assert.deepEqual(resolveMapPick([{ id: { id: 'nearby:poi_1' } }], null, lookup), { kind: 'nearby', id: 'poi_1' })
  assert.deepEqual(resolveMapPick([{ id: { id: 'nearby:poi_1' } }, { id: { id: 'complex:mappt_abc' } }], null, lookup),
    { kind: 'marker', id: 'mappt_abc' })
  assert.deepEqual(resolveMapPick([{ getProperty: () => 'demo-complex' }], null, lookup), { kind: 'facade', id: 'demo-complex' })
})

test('same marker reclick after pan is a new focus request, while passive data refresh and restored camera are not', () => {
  const original = selectionFocusKey('mappt_abc', 3)
  assert.equal(shouldFlyToSelection(original, null, true, false), true)
  assert.equal(shouldFlyToSelection(original, original, true, false), false)
  assert.equal(shouldFlyToSelection(selectionFocusKey('mappt_abc', 4), original, true, false), true)
  assert.equal(shouldFlyToSelection(selectionFocusKey('mappt_abc', 4), original, true, true), false)
  assert.equal(shouldFlyToSelection(selectionFocusKey('mappt_abc', 4), original, false, false), false)
  assert.equal(shouldFlyToSelection(null, original, true, false), false)
})

test('movement cannot consume a frame probe; visible cached coarse content may resume after camera settles', () => {
  const readiness = { visible: true, documentVisible: true, baseReady: true, cityAtScale: true,
    locked: false, moving: true, contentReady: true }
  assert.equal(probeStartAllowed(readiness), false)
  assert.equal(probeStartAllowed({ ...readiness, moving: false, contentReady: false }), false)
  assert.equal(probeStartAllowed({ ...readiness, moving: false }), true)
  const quality = new AdaptiveQuality()
  quality.stage = 'coarse'
  const window = { samples: Array(10).fill(20), activeProbe: true, visible: true, moving: false, contentReady: true }
  assert.equal(quality.observeWindow(window).changed, false)
  assert.equal(quality.observeWindow(window).stage, 'balanced')
  quality.pauseForStress('client_stall')
  assert.equal(probeStartAllowed({ ...readiness, moving: false, locked: quality.locked }), false,
    'a selected point never forces 3D through a stress lock')
  assert.equal(probeStartAllowed({ ...readiness, moving: false, documentVisible: false }), false)
})
