import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/developerUnitFilters.ts', import.meta.url), 'utf8')
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { emptyDeveloperUnitFilters, parseDeveloperUnitFilters, developerUnitFilterSummary, selectedDeveloperUnitCriteria } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)

test('observed apartment criteria convert to the strict server fields shared by list and map', () => {
  const draft = { ...emptyDeveloperUnitFilters(), priceMinMln: '12,5', priceMaxMln: '18',
    pricePerM2MaxThousand: '450', rooms: '2', areaMinM2: '45.5', floorMin: '5' }
  const result = parseDeveloperUnitFilters(draft)
  assert.equal(result.error, null)
  assert.deepEqual(result.filters, { price_min_rub: 12_500_000, price_max_rub: 18_000_000,
    price_per_m2_max_rub: 450_000, rooms: 2, area_min_m2: 45.5, unit_floor_min: 5 })
  assert.match(developerUnitFilterSummary(result.filters), /цена в снимке/)
  assert.deepEqual(selectedDeveloperUnitCriteria({ query: 'Лэйк', status: 'building', ...result.filters }), {
    price_min_rub: 12_500_000, price_max_rub: 18_000_000, price_per_m2_min_rub: undefined,
    price_per_m2_max_rub: 450_000, rooms: 2, area_min_m2: 45.5, area_max_m2: undefined,
    unit_floor_min: 5, unit_floor_max: undefined, district: undefined, okrug_id: undefined, max_noise_index: undefined,
    max_air_index: undefined,
  })
})

test('invalid ranges and decimals cannot silently broaden the source unit query', () => {
  for (const draft of [
    { priceMinMln: '20', priceMaxMln: '10' },
    { rooms: '2.5' },
    { floorMin: '0' },
    { areaMinM2: '-10' },
    { priceMinMln: '1.0000001' },
  ]) {
    const result = parseDeveloperUnitFilters({ ...emptyDeveloperUnitFilters(), ...draft })
    assert.ok(result.error)
    assert.deepEqual(result.filters, {})
  }
})

test('okrug, noise and air only narrow houses when explicitly selected', () => {
  assert.deepEqual(parseDeveloperUnitFilters(emptyDeveloperUnitFilters()).filters, {})
  const selected = parseDeveloperUnitFilters({ ...emptyDeveloperUnitFilters(), okrugId: 'okrug:северный административный', noiseMax: '0', airMax: '42' })
  assert.equal(selected.error, null)
  assert.deepEqual(selected.filters, { okrug_id: 'okrug:северный административный', max_noise_index: 0, max_air_index: 42 })
  assert.equal(selectedDeveloperUnitCriteria(selected.filters).max_noise_index, 0)
  assert.equal(selectedDeveloperUnitCriteria(selected.filters).max_air_index, 42)
  assert.deepEqual(parseDeveloperUnitFilters({ ...emptyDeveloperUnitFilters(), noiseMax: '100' }).filters, { max_noise_index: 100 })
  assert.deepEqual(parseDeveloperUnitFilters({ ...emptyDeveloperUnitFilters(), airMax: '0' }).filters, { max_air_index: 0 })
  for (const noiseMax of ['101', '-1', '10.5', 'abc']) {
    const invalid = parseDeveloperUnitFilters({ ...emptyDeveloperUnitFilters(), noiseMax })
    assert.ok(invalid.error)
    assert.deepEqual(invalid.filters, {})
  }
  for (const airMax of ['101', '-1', '10.5', 'abc']) {
    const invalid = parseDeveloperUnitFilters({ ...emptyDeveloperUnitFilters(), airMax })
    assert.ok(invalid.error)
    assert.deepEqual(invalid.filters, {})
  }
})
