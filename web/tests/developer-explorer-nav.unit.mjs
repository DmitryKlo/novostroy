import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/developerExplorerNav.ts', import.meta.url), 'utf8')
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { initialExplorerState, openExplorerFloor, openExplorerUnit, explorerBack } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)

test('Escape closes only the top menu, then returns unit to floor to building to map', () => {
  const building = initialExplorerState()
  const floor = openExplorerFloor(building, 'floor-opaque', 'section-opaque')
  const unit = openExplorerUnit(floor, 'unit-opaque', 'floor-opaque', 'section-opaque')
  const menu = { ...unit, menuOpen: true }
  assert.deepEqual(explorerBack(menu), { state: unit, close: false })
  assert.deepEqual(explorerBack(unit), { state: floor, close: false })
  assert.deepEqual(explorerBack(floor), { state: building, close: false })
  assert.deepEqual(explorerBack(building), { state: building, close: true })
})

test('a unit without a verified floor returns directly to its building', () => {
  const building = initialExplorerState()
  const unit = openExplorerUnit(building, 'unit-unassigned', null, null)
  assert.deepEqual(explorerBack(unit), { state: building, close: false })
})

test('an unassigned-floor list remains a distinct back step without inventing floor number', () => {
  const building = initialExplorerState()
  const unassigned = openExplorerFloor(building, null, null)
  const unit = openExplorerUnit(unassigned, 'unit-unassigned', null, null)
  assert.deepEqual(explorerBack(unit), { state: unassigned, close: false })
  assert.deepEqual(explorerBack(unassigned), { state: building, close: false })
})
