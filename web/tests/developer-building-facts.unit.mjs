import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/developerBuildingFacts.ts', import.meta.url), 'utf8')
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { developerDisplayCaption, developerFloorsCaption } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)

test('developer label retains the source qualification and does not invent a legal name', () => {
  assert.equal(developerDisplayCaption({ developer_display_name: 'Пример', developer_display_basis: 'aggregator_asserted_unverified' }),
    'Застройщик по данным каталога: Пример')
  assert.equal(developerDisplayCaption({ developer_display_name: 'Пример', developer_display_basis: 'official_project_brand' }),
    'Бренд проекта: Пример')
  assert.equal(developerDisplayCaption({ developer_display_name: 'Пример' }), 'Застройщик не указан в данных источника')
})

test('building floors require source basis; independent section counts never become one building total', () => {
  assert.equal(developerFloorsCaption({ total_floors: 22, total_floors_basis: 'source_apartment_record_building_floors' }),
    'Этажей в корпусе по источнику: 22')
  assert.equal(developerFloorsCaption({ total_floors: null, section_floor_counts: [
    { section_label: '1', total_floors: 16, total_floors_basis: 'source_section_floor_count' },
    { section_label: '2', total_floors: 10, total_floors_basis: 'source_section_floor_count' },
  ] }), 'Этажность секций по источнику: 1 — 16, 2 — 10')
  assert.equal(developerFloorsCaption({ total_floors: null, max_observed_unit_floor: 13 }), 'Этажность корпуса: —')
})
