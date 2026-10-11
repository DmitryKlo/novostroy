import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { api, setApiSnapshot, RegistrySnapshotChanged } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)
const tileCacheSource = await readFile(new URL('../src/tileCache.ts', import.meta.url), 'utf8')
const tileCacheJavascript = ts.transpileModule(tileCacheSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { cityVolumesAtHeight, reconcileTiles, roadDetailAllowed, roadDetailAtHeight, roadScreenSpaceError, stagedCityVersion, TileLoadLedger, tilesToRequest, traversalOptions } = await import(`data:text/javascript;base64,${Buffer.from(tileCacheJavascript).toString('base64')}`)

test('ground and roads use visible-child skip traversal without sibling preloading', () => {
  assert.deepEqual(traversalOptions('ground_buildings'), { skipLevelOfDetail: true, loadSiblings: false })
  assert.deepEqual(traversalOptions('city_volumes'), { skipLevelOfDetail: false, loadSiblings: false })
  assert.deepEqual(traversalOptions('roads'), { skipLevelOfDetail: true, loadSiblings: false })
})

test('road exact cells wait for block scale and measured client headroom while coarse stays cached', () => {
  let near = false
  for (const [height, expected] of [[65_000, false], [4_200, false], [1_200, false], [1_000, true], [1_400, true], [1_500, false], [900, true]]) {
    near = roadDetailAtHeight(height, near)
    assert.equal(near, expected, `height=${height}`)
  }
  assert.equal(roadDetailAtHeight(Number.NaN, true), false)
  assert.equal(roadDetailAllowed(true, false, 'flat'), false)
  assert.equal(roadDetailAllowed(true, false, 'coarse'), false)
  assert.equal(roadDetailAllowed(true, true, 'detail'), false)
  assert.equal(roadDetailAllowed(false, false, 'detail'), false)
  assert.equal(roadDetailAllowed(true, false, 'balanced'), true)
  assert.equal(roadDetailAllowed(true, false, 'detail'), true)
  assert.equal(roadScreenSpaceError(470, 800, true), 8)
  assert.ok(roadScreenSpaceError(4_200, 800, false) > 100)
  assert.ok(roadScreenSpaceError(65_000, 800, false) < roadScreenSpaceError(4_200, 800, false))
  assert.ok(roadScreenSpaceError(65_000, 800, false) > 4)
  const roads = { show: true, maximumScreenSpaceError: roadScreenSpaceError(470, 800, true) }
  const cached = new Map([['roads', roads]])
  reconcileTiles(cached, new Set(['roads']), () => false, () => assert.fail('road primitive was removed'))
  roads.maximumScreenSpaceError = roadScreenSpaceError(4_200, 800, false)
  assert.equal(cached.get('roads'), roads)
})

test('viewer teardown releases pending URLs and discards late roots from the old scene', async () => {
  const ledger = new TileLoadLedger()
  const releases = []
  const accepted = []
  const discarded = []
  const create = () => new Promise((resolve) => { releases.push(resolve) })
  const accept = (value) => { accepted.push(value); return value }
  const discard = (value) => { discarded.push(value) }
  const failed = (error) => { throw error }
  const first = ledger.ensure('/assets/ground/tileset.json', create, accept, discard, failed)
  assert.equal(ledger.ensure('/assets/ground/tileset.json', create, accept, discard, failed), first)
  assert.equal(releases.length, 1)
  ledger.reset()
  const second = ledger.ensure('/assets/ground/tileset.json', create, accept, discard, failed)
  assert.equal(releases.length, 2)
  releases[0]('old-viewer-root')
  assert.equal(await first, null)
  assert.deepEqual(discarded, ['old-viewer-root'])
  assert.equal(ledger.pending.get('/assets/ground/tileset.json'), second)
  releases[1]('new-viewer-root')
  assert.equal(await second, 'new-viewer-root')
  assert.deepEqual(accepted, ['new-viewer-root'])
  assert.equal(ledger.pending.size, 0)
})

test('ground-first scheduling excludes both city and facade root requests until the frame gate opens', () => {
  const layers = ['ground_buildings', 'roads', 'parks', 'water', 'city_volumes', 'facade'].map((kind) => ({ kind }))
  assert.deepEqual(tilesToRequest(layers, true, false).map((tile) => tile.kind), ['ground_buildings', 'roads', 'parks', 'water'])
  assert.deepEqual(tilesToRequest(layers, true, true), layers)
  assert.deepEqual(tilesToRequest(layers, true, true, false).map((tile) => tile.kind), ['ground_buildings', 'roads', 'parks', 'water', 'facade'])
  assert.deepEqual(tilesToRequest(layers, false, false), layers)
})

test('ground city volumes use 12/16 km hysteresis and keep an existing tileset cached when far', () => {
  let enabled = false
  for (const [height, expected] of [[65_000, false], [14_000, false], [12_000, true], [14_000, true], [15_999, true], [16_000, false], [14_000, false], [11_000, true]]) {
    enabled = cityVolumesAtHeight(height, enabled)
    assert.equal(enabled, expected, `height=${height}`)
  }
  assert.equal(cityVolumesAtHeight(Number.NaN, true), false)
  const city = { show: true }
  const cached = new Map([['city', city]])
  const removed = []
  const wanted = new Set(['city'])
  reconcileTiles(cached, wanted, () => false, (tile) => removed.push(tile))
  city.show = cityVolumesAtHeight(65_000, true)
  assert.equal(city.show, false)
  assert.equal(cached.get('city'), city)
  city.show = cityVolumesAtHeight(11_000, false)
  assert.equal(city.show, true)
  assert.equal(cached.get('city'), city)
  assert.deepEqual(removed, [])
})
const gateSource = await readFile(new URL('../src/groundGate.ts', import.meta.url), 'utf8')
const gateJavascript = ts.transpileModule(gateSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { flatRootsResolved, GroundFirstGate, groundViewSignature, isPreciseGroundTile } = await import(`data:text/javascript;base64,${Buffer.from(gateJavascript).toString('base64')}`)

test('root overview raster does not release 3D before a visible precise ground region', () => {
  assert.equal(isPreciseGroundTile(47), false)
  assert.equal(isPreciseGroundTile(2), true)
  assert.equal(isPreciseGroundTile(0), true)
  const gate = new GroundFirstGate('flat', 'ground', ['ground'])
  const frame = { visible: true, cameraKey: 'camera', groundLoaded: true, flatSettled: true, groundFailed: false }
  assert.equal(gate.frame({ ...frame, groundVisible: isPreciseGroundTile(47) }).startOverlay, false)
  assert.equal(gate.frame({ ...frame, groundVisible: isPreciseGroundTile(2) }).startOverlay, false)
  assert.equal(gate.frame({ ...frame, groundVisible: isPreciseGroundTile(2) }).startOverlay, true)
})

test('ground gate waits for flat roots, not every ancillary detail tile', () => {
  const urls = ['ground', 'roads', 'parks', 'water']
  const roots = new Set(['ground', 'roads', 'parks'])
  assert.equal(flatRootsResolved(urls, (url) => roots.has(url)), false)
  roots.add('water')
  assert.equal(flatRootsResolved(urls, (url) => roots.has(url)), true)
  const gate = new GroundFirstGate('flat', 'ground', urls)
  const frame = { visible: true, cameraKey: 'camera', groundVisible: true, groundLoaded: true, flatSettled: flatRootsResolved(urls, (url) => roots.has(url)), groundFailed: false }
  assert.equal(gate.frame(frame).startOverlay, false)
  assert.equal(gate.frame(frame).startOverlay, true)
})
const healthSource = await readFile(new URL('../src/mapTileHealth.ts', import.meta.url), 'utf8')
const healthJavascript = ts.transpileModule(healthSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { MapTileHealth, layerLoadText } = await import(`data:text/javascript;base64,${Buffer.from(healthJavascript).toString('base64')}`)
const qualitySource = await readFile(new URL('../src/adaptiveQuality.ts', import.meta.url), 'utf8')
const qualityJavascript = ts.transpileModule(qualitySource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { AdaptiveQuality, BoundedFrameProbe, citySseForQuality, meaningfulViewChange, ProbeAttemptLedger, qualityAllows } = await import(`data:text/javascript;base64,${Buffer.from(qualityJavascript).toString('base64')}`)

test('probe cap cannot restart on the same pose and quality, but a new epoch can', () => {
  const attempts = new ProbeAttemptLedger()
  assert.equal(attempts.claim(1, 'flat', 'ground_frame_ready'), true)
  assert.equal(attempts.claim(1, 'flat', 'map_resumed'), false)
  assert.equal(attempts.claim(1, 'coarse', 'coarse_city_visible'), true)
  assert.equal(attempts.claim(1, 'coarse', 'camera_motion'), true)
  assert.equal(attempts.claim(1, 'coarse', 'camera_motion'), false)
  attempts.releaseInterrupted(1, 'coarse', 'camera_motion')
  assert.equal(attempts.claim(1, 'coarse', 'camera_motion'), true)
  assert.equal(attempts.claim(2, 'flat', 'camera_settled'), true)
})

test('camera interruption can retry once, but completed probes cannot loop on the same pose', () => {
  const attempts = new ProbeAttemptLedger()
  assert.equal(attempts.claim(3, 'flat', 'ground_frame_ready'), true)
  attempts.releaseInterrupted(3, 'flat', 'ground_frame_ready')
  assert.equal(attempts.claim(3, 'flat', 'camera_settled'), true)
  assert.equal(attempts.claim(3, 'flat', 'camera_settled'), false)
})

test('stress retry requires a meaningful camera or viewport change', () => {
  const pose = { x: 0, y: 0, z: 0, height: 5000, heading: 0, pitch: -1, width: 390, canvasHeight: 844 }
  assert.equal(meaningfulViewChange(pose, { ...pose, x: 20, height: 5010 }), false)
  assert.equal(meaningfulViewChange(pose, { ...pose, width: 391, canvasHeight: 843 }), false)
  assert.equal(meaningfulViewChange(pose, { ...pose, x: 150 }), true)
  assert.equal(meaningfulViewChange(pose, { ...pose, heading: 0.04 }), true)
  assert.equal(meaningfulViewChange(pose, { ...pose, width: 844, canvasHeight: 390 }), true)
})

test('bounded active probe ignores idle gaps and stops when hidden or capped', () => {
  const probe = new BoundedFrameProbe()
  assert.deepEqual(probe.rendered(1000, true), { interval: null, window: null })
  probe.start('flat_ready')
  assert.equal(probe.request(), true)
  assert.deepEqual(probe.rendered(10_000, true), { interval: null, window: null })
  for (let index = 1; index <= 9; index++) assert.equal(probe.rendered(10_000 + index * 20, true).window, null)
  assert.equal(probe.rendered(10_200, true).window?.length, 10)
  assert.equal(probe.windows, 1)
  assert.deepEqual(probe.rendered(50_000, false), { interval: null, window: null })
  assert.equal(probe.active, false)
  probe.start('retry')
  for (let index = 0; index < 80; index++) assert.equal(probe.request(), true)
  assert.equal(probe.request(), false)
  assert.equal(probe.reason, 'request_cap')
})

test('active flat and 3D frame windows progressively earn detail without credit for hidden, idle, or moving frames', () => {
  const quality = new AdaptiveQuality()
  const good = { samples: Array(10).fill(20), activeProbe: true, visible: true, moving: false, contentReady: true }
  assert.equal(quality.observeWindow({ ...good, activeProbe: false }).stage, 'flat')
  assert.equal(quality.observeWindow({ ...good, visible: false }).stage, 'flat')
  assert.equal(quality.observeWindow({ ...good, moving: true }).stage, 'flat')
  assert.equal(quality.observeWindow(good).stage, 'flat')
  assert.equal(quality.observeWindow(good).stage, 'silhouette')
  assert.equal(citySseForQuality(quality.stage), 16_384)
  assert.equal(qualityAllows('city_volumes', quality.stage), true)
  assert.equal(qualityAllows('facade', quality.stage), false)
  assert.equal(quality.observeWindow({ ...good, contentReady: false }).stage, 'silhouette')
  assert.equal(quality.observeWindow(good).stage, 'silhouette')
  assert.equal(quality.observeWindow(good).stage, 'coarse')
  assert.equal(citySseForQuality(quality.stage), 128)
  assert.equal(quality.observeWindow(good).stage, 'coarse')
  assert.equal(quality.observeWindow(good).stage, 'balanced')
  assert.equal(citySseForQuality(quality.stage), 64)
  assert.equal(quality.observeWindow(good).stage, 'balanced')
  assert.equal(quality.observeWindow(good).stage, 'detail')
  assert.equal(citySseForQuality(quality.stage), 24)
  assert.equal(qualityAllows('facade', quality.stage), true)
  assert.equal(qualityAllows('city_volumes', 'flat'), false)
  assert.equal(qualityAllows('facade', 'flat'), false)
  const legacyLayers = ['roads', 'parks', 'city_volumes', 'facade'].map((kind) => ({ kind }))
  assert.deepEqual(tilesToRequest(legacyLayers, false, true).filter((tile) => qualityAllows(tile.kind, 'flat')).map((tile) => tile.kind), ['roads', 'parks'])
})

test('moderate flat frames earn only regional silhouettes; sustained stress still sheds them', () => {
  const quality = new AdaptiveQuality()
  const moderate = { samples: Array(10).fill(45), activeProbe: true, visible: true, moving: false, contentReady: true }
  quality.observeWindow(moderate)
  assert.equal(quality.observeWindow(moderate).stage, 'silhouette')
  quality.observeWindow(moderate)
  assert.equal(quality.observeWindow(moderate).stage, 'silhouette', 'closer city detail retains the stricter budget')
  const stressed = { ...moderate, samples: Array(10).fill(55) }
  assert.equal(quality.observeWindow(stressed).stage, 'flat')
  assert.equal(quality.locked, true)
})

test('stress sheds detail or 3D and cannot oscillate back without a meaningful view change', () => {
  const quality = new AdaptiveQuality()
  const good = { samples: Array(10).fill(20), activeProbe: true, visible: true, moving: false, contentReady: true }
  for (let index = 0; index < 8; index++) quality.observeWindow(good)
  assert.equal(quality.stage, 'detail')
  const slow = { ...good, samples: [20, 20, 20, 20, 20, 20, 55, 55, 55, 55] }
  assert.equal(quality.observeWindow(slow).stage, 'coarse')
  for (let index = 0; index < 4; index++) quality.observeWindow(good)
  assert.equal(quality.stage, 'coarse')
  assert.equal(quality.observeWindow(slow).stage, 'silhouette')
  assert.equal(quality.locked, false)
  assert.equal(quality.observeWindow(slow).stage, 'flat')
  assert.equal(quality.locked, true)
  for (let index = 0; index < 4; index++) quality.observeWindow(good)
  assert.equal(quality.stage, 'flat')
  quality.onMeaningfulChange()
  assert.equal(quality.observeWindow(good).stage, 'flat')
  assert.equal(quality.observeWindow(good).stage, 'silhouette')
  assert.equal(quality.observeFrame(120, true), false)
  assert.equal(quality.observeFrame(120, true), true)
  assert.equal(quality.stage, 'flat')
  assert.equal(quality.reason, 'two_severe_frames')
  quality.onMeaningfulChange()
  quality.pauseForStress('probe_no_rendered_frames')
  assert.equal(quality.stage, 'flat')
  assert.equal(quality.locked, true)
})

test('two intermittent long frames in ten are caught even when p75 looks smooth', () => {
  const quality = new AdaptiveQuality()
  const good = { samples: Array(10).fill(20), activeProbe: true, visible: true, moving: false, contentReady: true }
  quality.observeWindow(good)
  quality.observeWindow(good)
  assert.equal(quality.stage, 'silhouette')
  const janky = quality.observeWindow({ ...good, samples: [20, 65, 20, 20, 20, 20, 65, 20, 20, 20] })
  assert.equal(janky.p75, 20)
  assert.equal(janky.stage, 'flat')
  assert.equal(quality.locked, true)
})

test('content failure overrides a complete root count and keeps the actual Cesium cause in diagnostics', () => {
  const health = new MapTileHealth()
  const city = { url: '/assets/public-citywide-v8/tileset.json', kind: 'city_volumes', label: 'Объёмы зданий' }
  assert.equal(layerLoadText(7, 7, health.active([city.url])), 'Подключено 7/7 наборов')
  assert.equal(health.mark(city, 'content', { url: '/assets/public-citywide-v8/region_X.tileset.json', message: 'Invalid bounding volume' }), 1)
  assert.equal(health.mark(city, 'content', { message: 'Second failed cell' }), 2)
  const failure = health.active([city.url])[0]
  assert.equal(failure.phase, 'content')
  assert.equal(failure.count, 2)
  assert.equal(failure.message, 'Second failed cell')
  assert.equal(failure.errorClass, 'Object')
  assert.equal(failure.contentUrl, '/assets/public-citywide-v8/region_X.tileset.json')
  assert.match(layerLoadText(7, 7, [failure]), /^Ошибка загрузки тайлов: Объёмы зданий$/)
  assert.deepEqual(health.active(['/assets/unrelated/tileset.json']), [])
  health.clear()
  assert.deepEqual(health.active([city.url]), [])
})

test('ground frame identity changes on resize, DPR, or projection even at the same camera pose', () => {
  const pose = [1, 2, 3]
  const orientation = [0, -1, 0]
  const base = groundViewSignature(pose, orientation, [390, 844, 585, 1266, 0.5], [1, 0, 0, 1])
  assert.notEqual(base, groundViewSignature(pose, orientation, [844, 390, 1266, 585, 0.5], [1, 0, 0, 1]))
  assert.notEqual(base, groundViewSignature(pose, orientation, [390, 844, 780, 1688, 1], [1, 0, 0, 1]))
  assert.notEqual(base, groundViewSignature(pose, orientation, [390, 844, 585, 1266, 0.5], [1.1, 0, 0, 1]))
})

test('3D starts only after two visible loaded ground frames at one camera', () => {
  const gate = new GroundFirstGate('flat-a', 'ground', ['ground', 'roads'])
  const frame = { visible: true, cameraKey: 'camera-a', groundVisible: true, groundLoaded: true, flatSettled: true, groundFailed: false }
  assert.equal(gate.frame({ ...frame, groundLoaded: false }).startOverlay, false)
  assert.equal(gate.frame({ ...frame, groundVisible: false }).startOverlay, false)
  assert.deepEqual(gate.frame(frame), { startOverlay: false, requestFrame: true })
  assert.deepEqual(gate.frame({ ...frame, cameraKey: 'camera-b' }), { startOverlay: false, requestFrame: true })
  assert.deepEqual(gate.frame({ ...frame, cameraKey: 'camera-b' }), { startOverlay: true, requestFrame: false })
  assert.equal(gate.overlayStarted, true)
})

test('hidden, failed, and empty ground views cannot release 3D; cached overlay survives pan', () => {
  const frame = { visible: true, cameraKey: 'camera-a', groundVisible: true, groundLoaded: true, flatSettled: true, groundFailed: false }
  const gate = new GroundFirstGate('flat-a', 'ground', ['ground'])
  assert.equal(gate.frame(frame).requestFrame, true)
  assert.equal(gate.frame({ ...frame, visible: false }).startOverlay, false)
  assert.equal(gate.frame({ ...frame, groundFailed: true }).startOverlay, false)
  assert.equal(gate.frame({ ...frame, groundVisible: false }).startOverlay, false)
  assert.equal(gate.frame(frame).requestFrame, true)
  assert.equal(gate.frame(frame).startOverlay, true)
  gate.cameraMoved()
  assert.equal(gate.frame({ ...frame, cameraKey: 'camera-b', groundVisible: false }).startOverlay, false)
  assert.equal(gate.overlayStarted, true)
  const reset = new GroundFirstGate('flat-b', 'ground-new', ['ground-new'])
  assert.equal(reset.frame(frame).startOverlay, false)
})

test('v6 and v7 city roots enable staged traversal only at exact versioned URLs', () => {
  assert.equal(stagedCityVersion('/assets/public-citywide-v6/tileset.json'), 'v6')
  assert.equal(stagedCityVersion('/assets/public-citywide-v7/tileset.json'), 'v7')
  assert.equal(stagedCityVersion('/assets/public-citywide-v7/tileset.json?v=1'), 'v7')
  assert.equal(stagedCityVersion('/assets/public-citywide-v7/region_R0_R0.tileset.json'), null)
  assert.equal(stagedCityVersion('/assets/facades/tileset.json'), null)
  assert.equal(stagedCityVersion('/assets/public-citywide-v8/tileset.json'), null)
})

test('repeated active tile reconciliation terminates and bounds dormant facades', () => {
  const removed = []
  const tiles = new Map([
    ['city', { show: true, facade: false }],
    ['facade-a', { show: true, facade: true }],
    ['facade-b', { show: true, facade: true }],
    ['facade-c', { show: true, facade: true }],
    ['facade-d', { show: true, facade: true }],
  ])
  const retain = (tile) => tile.facade
  const remove = (tile) => removed.push(tile)
  for (let index = 0; index < 100; index++) {
    assert.equal(reconcileTiles(tiles, new Set(['city', 'facade-a']), retain, remove), 3)
    assert.equal(tiles.size, 5)
    assert.equal(tiles.get('city').show, true)
    assert.equal(tiles.get('facade-a').show, true)
  }
  tiles.set('facade-e', { show: true, facade: true })
  assert.equal(reconcileTiles(tiles, new Set(['city', 'facade-e']), retain, remove), 3)
  assert.equal(tiles.size, 5)
  assert.equal(removed.length, 1)
  assert.equal(tiles.has('facade-b'), false)
  assert.equal(tiles.get('facade-e').show, true)
})

test('map requests share pending work, reuse results, and expire on snapshot change', async () => {
  const originalFetch = globalThis.fetch
  const calls = []
  let release
  globalThis.fetch = async (url) => {
    calls.push(String(url))
    await new Promise((resolve) => { release = resolve })
    return new Response(JSON.stringify({ snapshot_id: calls.length === 1 ? 'snapshot-a' : 'snapshot-b', items: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    setApiSnapshot('snapshot-a')
    const first = api.map({ limit: 100, district_ids: ['b', 'a'], rooms: [2, 1] })
    const second = api.map({ rooms: [1, 2], district_ids: ['a', 'b'] })
    assert.equal(calls.length, 1)
    release()
    assert.deepEqual(await first, await second)
    await api.map({ district_ids: ['b', 'a'], rooms: [2, 1] })
    assert.equal(calls.length, 1)
    setApiSnapshot('snapshot-b')
    const next = api.map({ district_ids: ['a', 'b'], rooms: [2, 1] })
    assert.equal(calls.length, 2)
    release()
    assert.equal((await next).snapshot_id, 'snapshot-b')
  } finally { globalThis.fetch = originalFetch }
})

test('aborted search propagates AbortError instead of a connection message', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (_url, options) => {
    await new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))))
  }
  try {
    const controller = new AbortController()
    const pending = api.search({ query: 'test' }, controller.signal)
    controller.abort()
    await assert.rejects(pending, { name: 'AbortError' })
  } finally { globalThis.fetch = originalFetch }
})

test('a new snapshot does not join an older in-flight map request', async () => {
  const originalFetch = globalThis.fetch
  const pending = []
  globalThis.fetch = (url) => new Promise((resolve) => pending.push({ url, resolve }))
  try {
    setApiSnapshot('snapshot-c')
    const older = api.map({ complex_ids: ['one'] })
    setApiSnapshot('snapshot-d')
    const newer = api.map({ complex_ids: ['one'] })
    assert.equal(pending.length, 2)
    pending[0].resolve(new Response(JSON.stringify({ snapshot_id: 'snapshot-c' }), { status: 200 }))
    pending[1].resolve(new Response(JSON.stringify({ snapshot_id: 'snapshot-d' }), { status: 200 }))
    assert.equal((await older).snapshot_id, 'snapshot-c')
    assert.equal((await newer).snapshot_id, 'snapshot-d')
    assert.equal((await api.map({ complex_ids: ['one'] })).snapshot_id, 'snapshot-d')
    assert.equal(pending.length, 2)
  } finally { globalThis.fetch = originalFetch }
})

test('registry list and map use one filter set and reject mixed snapshots', async () => {
  const originalFetch = globalThis.fetch
  const seen = []
  let mapSnapshot = 'registry-a'
  globalThis.fetch = async (url) => {
    const path = String(url)
    seen.push(path)
    const base = { dataset_id: 'building_registry', snapshot_id: path.includes('/map') ? mapSnapshot : 'registry-a' }
    return new Response(JSON.stringify(path.includes('/map') ? { ...base, items: [], counts: { records: 0, geocoded: 0 }, tilesets: [] } : { ...base, items: [], records_total: 0 }), { status: 200 })
  }
  try {
    const view = await api.registryView({ query: 'Арбат', status: 'строится', kind: 'multifamily' }, 'registry-a')
    assert.equal(view.list.snapshot_id, view.map.snapshot_id)
    assert.equal(seen.length, 2)
    for (const url of seen) {
      assert.equal(new URL(`http://local${url}`).searchParams.get('query'), 'Арбат')
      assert.equal(new URL(`http://local${url}`).searchParams.get('status'), 'строится')
      assert.equal(new URL(`http://local${url}`).searchParams.get('kind'), 'multifamily')
    }
    mapSnapshot = 'registry-b'
    await assert.rejects(api.registryView({ query: 'Арбат' }, 'registry-a'), RegistrySnapshotChanged)
  } finally { globalThis.fetch = originalFetch }
})

test('registry pair cancellation propagates an abort', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))))
  try {
    const controller = new AbortController()
    const pending = api.registryView({ query: 'old' }, 'registry-a', controller.signal)
    controller.abort()
    await assert.rejects(pending, { name: 'AbortError' })
  } finally { globalThis.fetch = originalFetch }
})
