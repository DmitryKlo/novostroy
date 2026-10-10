import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

async function importTypeScript(path) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
  return import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)
}

const { MODEL_PAGE_SIZE, parseModelReviewQuery, modelReviewSearch, modelProgress, safeModelNextStep, selectedModelRequestAllowed, selectedModelVolumeEligible, selectedPreviewPaths } = await importTypeScript('../src/modelReview.ts')
const { api, setModelRun, ModelSnapshotChanged } = await importTypeScript('../src/api.ts')

test('rejected deep link, filters, and page offset survive a URL round trip without loading 2317 rows', () => {
  assert.equal(parseModelReviewQuery('').decision, 'rejected')
  const filters = parseModelReviewQuery('?decision=rejected&reason_code=no_corpus_link&source_group=mapped_construction&registry_id=registry-aaaaaaaaaaaaaaaaaaaaaaaa&query=%D0%B4%D0%BE%D0%BC&offset=49')
  assert.deepEqual(filters, { decision: 'rejected', reason_code: 'no_corpus_link', source_group: 'mapped_construction', registry_id: 'registry-aaaaaaaaaaaaaaaaaaaaaaaa', query: 'дом', offset: 2 * MODEL_PAGE_SIZE })
  assert.deepEqual(parseModelReviewQuery(`?${modelReviewSearch(filters)}`), filters)
  assert.equal(parseModelReviewQuery('?decision=invalid&offset=-7').decision, 'rejected')
  assert.equal(parseModelReviewQuery('?offset=NaN').offset, 0)
  assert.equal(parseModelReviewQuery('?registry_id=../../secret').registry_id, '')
})

test('status metrics distinguish ready outlines from volumes and rejected from unevaluated', () => {
  assert.deepEqual(modelProgress(2317, { ready: 756, review: 100, rejected: 900, unavailable: 561 }), { assessed: 2317, pending: 0, renderable: 756 })
  assert.deepEqual(modelProgress(2317, { ready: 100, review: 50, rejected: 200, unavailable: 100 }), { assessed: 450, pending: 1867, renderable: 100 })
  assert.match(safeModelNextStep(['no_corpus_link']), /связывает контур/)
  assert.match(safeModelNextStep(['rights_unconfirmed'], '  Требуется лицензия.  '), /^Требуется лицензия\.$/)
  assert.match(safeModelNextStep(['non_building_infrastructure']), /инфраструктурная запись/)
})

test('selected 3D root requires a measured or explicitly estimated positive render height', () => {
  const base = { flatPresented: true, visible: true, decision: 'ready', assetReadiness: 'ready_to_render', qualityStage: 'coarse', hasVerifiedFootprint: true, depiction: 'schematic_massing', renderHeightM: 18 }
  assert.equal(selectedModelRequestAllowed(base), true)
  assert.equal(selectedModelVolumeEligible('construction_outline', null), false, 'flat-only candidate never creates a Viewer')
  assert.equal(selectedModelVolumeEligible('schematic_massing', 18), true)
  for (const change of [{ flatPresented: false }, { visible: false }, { decision: 'rejected' }, { assetReadiness: 'review' }, { qualityStage: 'flat' }, { hasVerifiedFootprint: false }, { depiction: 'construction_outline' }, { renderHeightM: null }, { renderHeightM: 0 }, { renderHeightM: Number.NaN }, { renderHeightM: Number.POSITIVE_INFINITY }]) {
    assert.equal(selectedModelRequestAllowed({ ...base, ...change }), false)
  }
})

test('selected flat preview keeps courtyard holes and separate multipolygon islands', async () => {
  const outer = [[37.6, 55.7], [37.61, 55.7], [37.61, 55.71], [37.6, 55.71], [37.6, 55.7]]
  const hole = [[37.602, 55.702], [37.604, 55.702], [37.604, 55.704], [37.602, 55.704], [37.602, 55.702]]
  const island = [[37.62, 55.72], [37.621, 55.72], [37.621, 55.721], [37.62, 55.72]]
  const shape = selectedPreviewPaths({ type: 'MultiPolygon', coordinates: [[outer, hole], [island]] }, null)
  assert.equal(shape.polygons.length, 2)
  assert.equal((shape.polygons[0].match(/M /g) ?? []).length, 2, 'courtyard remains a separate path ring')
  assert.equal((shape.polygons[1].match(/M /g) ?? []).length, 1, 'second island is retained')
  assert.equal(selectedPreviewPaths(null, null), null, 'bbox is never invented as a footprint')
  assert.equal(selectedPreviewPaths({ type: 'Polygon', coordinates: [Array.from({ length: 513 }, () => [37.6, 55.7])] }, null), null)
  const previewSource = await readFile(new URL('../src/SelectedModelPreview.tsx', import.meta.url), 'utf8')
  assert.match(previewSource, /fillRule="evenodd"/, 'holes must be cut out when SVG is painted')
})

test('candidate list uses bounded server paging, selected detail/geometry only, and run-scoped cache', async () => {
  const oldFetch = globalThis.fetch
  const calls = []
  const meta = { dataset_id: 'model_candidates', registry_snapshot_id: 'registry-1', model_run_id: 'run-a', checked_at: '2026-10-09T00:00:00Z' }
  const height_evidence = { mapped_height_m: null, mapped_levels: null, estimated_render_height_m: null, render_height_m: null, render_height_basis: null }
  const item = { candidate_id: 'cand_aaaaaaaaaaaaaaaaaaaaaaaa', registry_id: null, name: 'Контур стройки', source_group: 'mapped_construction', entity_type: 'mapped_construction', decision: 'ready', asset_readiness: 'ready_to_render', project_link_status: 'unlinked', ready_to_render: true, depiction: 'construction_outline', model_quality: 'mapped_footprint', geometry_quality: 'mapped_footprint', geometry_as_of: '2026-10-09', render_height_m: null, height_evidence, reason_codes: [], explanation_ru: 'Контур доступен', next_step_ru: '' }
  globalThis.fetch = async (url, options) => {
    calls.push([String(url), options?.signal])
    const path = String(url).split('?')[0]
    const body = path.endsWith('/summary') ? { ...meta, run_status: 'ready', counts: { scanned: 2317, ready: 756, review: 1, rejected: 1000, unavailable: 560 }, facets: { reason_codes: [], source_groups: [] } }
      : path.endsWith('/geometry') ? { ...meta, candidate_id: item.candidate_id, center: [37.6, 55.7], bbox: [37.5, 55.6, 37.7, 55.8], footprint: [[37.6, 55.7], [37.61, 55.7], [37.61, 55.71]], preview_geometry: { type: 'Polygon', coordinates: [[[37.6, 55.7], [37.61, 55.7], [37.61, 55.71], [37.6, 55.7]]] }, render_height_m: null, height_evidence, model_tileset: { url: '/assets/public-models-v1/model_a.tileset.json', kind: 'mapped_construction', model_quality: 'mapped_footprint', attribution: '© OpenStreetMap contributors' } }
        : path.endsWith(`/${item.candidate_id}`) ? { ...meta, item: { ...item, model_tileset: null, attempts: [{ code: 'license', label_ru: 'Права', result_ru: 'Проверены', count: null }] } }
          : { ...meta, dataset_total: 2317, records_total: 1000, limit: 24, offset: 0, items: [item] }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    const summary = await api.modelSummary()
    setModelRun(summary.model_run_id)
    const list = await api.modelCandidates({ decision: 'rejected', reason_code: 'no_corpus_link', registry_id: 'registry-aaaaaaaaaaaaaaaaaaaaaaaa', query: 'дом', limit: 24, offset: 0 }, 'run-a')
    assert.equal(list.items.length, 1)
    assert.equal(calls.length, 2)
    assert.match(calls[1][0], /limit=24/)
    assert.match(calls[1][0], /offset=0/)
    assert.match(calls[1][0], /reason_code=no_corpus_link/)
    assert.match(calls[1][0], /registry_id=registry-aaaaaaaaaaaaaaaaaaaaaaaa/)
    assert.equal(calls.some(([url]) => url.endsWith('/geometry')), false)
    await api.modelCandidates({ decision: 'rejected', reason_code: 'no_corpus_link', registry_id: 'registry-aaaaaaaaaaaaaaaaaaaaaaaa', query: 'дом', limit: 24, offset: 0 }, 'run-a')
    assert.equal(calls.length, 2, 'same run/page is reused')
    const detail = await api.modelCandidate(item.candidate_id, 'run-a')
    assert.equal(detail.item.candidate_id, item.candidate_id)
    assert.equal(detail.item.attempts.length, 1)
    assert.equal(calls.length, 3)
    const geometry = await api.modelGeometry(item.candidate_id, 'run-a')
    assert.equal(geometry.model_tileset.kind, 'mapped_construction')
    assert.equal(calls.length, 4)
    setModelRun('run-b')
    await assert.rejects(api.modelCandidates({ decision: 'rejected', limit: 24, offset: 0 }, 'run-b'), ModelSnapshotChanged)
    assert.equal(calls.length, 5, 'new run never reuses old page')
  } finally { globalThis.fetch = oldFetch }
})

test('aborted selected detail cannot be cached as the next selection', async () => {
  const oldFetch = globalThis.fetch
  const controller = new AbortController()
  let requests = 0
  globalThis.fetch = (_url, options) => {
    requests++
    return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true }))
  }
  try {
    setModelRun('abort-run')
    const pending = api.modelCandidate('candidate-switch', 'abort-run', controller.signal)
    controller.abort()
    await assert.rejects(pending, { name: 'AbortError' })
    assert.equal(requests, 1)
  } finally { globalThis.fetch = oldFetch }
})
