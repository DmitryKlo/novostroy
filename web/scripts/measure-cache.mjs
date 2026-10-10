#!/usr/bin/env node
// Run only with an already available managed preview. This script never starts a server.
import { createHash } from 'node:crypto'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { chromium } from '@playwright/test'

const target = new URL(process.env.NOVOSTROY_PREVIEW_URL ?? 'http://127.0.0.1:5173/')
if (target.protocol !== 'http:' || target.hostname !== '127.0.0.1' || target.port !== '5173') {
  throw new Error('Use the managed preview at http://127.0.0.1:5173/.')
}
target.searchParams.set('__map_test', '1')
const output = resolve(process.env.NOVOSTROY_CACHE_REPORT ?? '../.runtime/cache-cold-warm-ground-v8.json')

function keyFor(raw) {
  const url = new URL(raw)
  const path = url.pathname
  const kind = path.startsWith('/assets/public-ground-') && path.endsWith('.glb') ? 'ground-glb'
    : path.startsWith('/assets/public-citywide-') && path.endsWith('.glb') ? 'city-glb'
      : path.startsWith('/assets/') && path.endsWith('.glb') ? 'other-glb'
    : path.startsWith('/assets/') && path.endsWith('.json') ? 'tileset-json'
      : path === '/v1/registry/map' ? 'api-registry-map'
        : path === '/v1/registry/buildings' ? 'api-registry-buildings'
          : path.startsWith('/v1/') ? 'api-other'
        : path.startsWith('/cesium/') ? 'cesium-static'
          : path.startsWith('/bundled/') ? 'app-bundle' : 'other'
  return `${kind}:${createHash('sha256').update(path + url.search).digest('hex').slice(0, 12)}`
}

async function pass(page, phase) {
  const started = performance.now()
  const requests = new Map()
  const pending = new Set()
  let lastActivity = Date.now()
  const onRequest = (request) => {
    pending.add(request); lastActivity = Date.now()
    const key = keyFor(request.url())
    const row = requests.get(key) ?? { key, count: 0, statuses: {}, bytes: 0, cacheControl: null }
    row.count++
    requests.set(key, row)
  }
  const onResponse = async (response) => {
    const row = requests.get(keyFor(response.url()))
    if (!row) return
    row.statuses[response.status()] = (row.statuses[response.status()] ?? 0) + 1
    const headers = await response.allHeaders()
    row.cacheControl = headers['cache-control'] ?? row.cacheControl
  }
  const onDone = async (request) => {
    pending.delete(request); lastActivity = Date.now()
    const row = requests.get(keyFor(request.url()))
    if (row) { try { row.bytes += (await request.sizes()).responseBodySize } catch { /* Timing below remains available. */ } }
  }
  const onFailed = (request) => { pending.delete(request); lastActivity = Date.now() }
  page.on('request', onRequest); page.on('response', onResponse)
  page.on('requestfinished', onDone); page.on('requestfailed', onFailed)
  try {
    const navigation = phase === 'cold' ? await page.goto(target.href, { waitUntil: 'domcontentloaded' }) : await page.reload({ waitUntil: 'domcontentloaded' })
    if (!navigation?.ok()) throw new Error(`${phase} navigation failed: ${navigation?.status()}`)
    await page.getByRole('tab', { name: 'Карта' }).click()
    await page.locator('[data-testid="map"]').waitFor()
    await page.waitForFunction(() => Boolean(window.__novoMapTest?.cameraPose?.()), undefined, { timeout: 15_000 })
    const deadline = Date.now() + 35_000
    while (Date.now() < deadline) {
      await page.waitForTimeout(250)
      if (pending.size === 0 && Date.now() - lastActivity > 1500) break
    }
    const diagnostics = await page.evaluate(() => {
      const api = window.__novoMapTest
      const resources = performance.getEntriesByType('resource').map((item) => ({
        url: item.name, transferSize: item.transferSize, encodedBodySize: item.encodedBodySize,
      }))
      const layers = api.cacheBudget()
      const frames = api.frameTimes()
      return {
        registryMode: Boolean(document.querySelector('.registry-app')),
        camera: api.cameraPose(), cityRange: api.cityRange(), adaptiveQuality: api.adaptiveQuality(), flatLayerStates: api.flatLayerStates(), legacyCityStage: api.cityStage(), groundStage: api.groundStage(), volumeStage: api.volumeStage(), markerCount: api.markerCount(),
        cacheBudgetBytes: layers.reduce((sum, item) => sum + item.cacheBytes + item.maximumCacheOverflowBytes, 0),
        tilesetMemoryBytes: layers.reduce((sum, item) => sum + item.totalMemoryUsageInBytes, 0),
        layerCount: layers.length, frameCount: frames.length, resources,
      }
    })
    const resources = {}
    for (const item of diagnostics.resources) {
      const key = keyFor(item.url)
      const row = resources[key] ?? { count: 0, transferBytes: 0, reusedFromCache: 0 }
      row.count++; row.transferBytes += item.transferSize
      if (item.transferSize === 0 && item.encodedBodySize > 0) row.reusedFromCache++
      resources[key] = row
    }
    const { resources: _rawResources, ...metrics } = diagnostics
    return {
      phase, durationMs: Math.round(performance.now() - started), idleTimedOut: pending.size > 0 || Date.now() - lastActivity <= 1500,
      ...metrics, requests: [...requests.values()].sort((a, b) => a.key.localeCompare(b.key)), resources,
    }
  } finally {
    page.off('request', onRequest); page.off('response', onResponse)
    page.off('requestfinished', onDone); page.off('requestfailed', onFailed)
  }
}

const browser = await chromium.launch({ headless: true })
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true })
  const page = await context.newPage()
  const cold = await pass(page, 'cold')
  const warm = await pass(page, 'warm')
  if (!cold.registryMode || !warm.registryMode) throw new Error('The building registry is not the active catalog; no registry cache comparison was written.')
  const poseClose = ['longitude', 'latitude', 'height', 'heading', 'pitch'].every((key) => Math.abs(cold.camera[key] - warm.camera[key]) <= (key === 'height' ? 30 : 0.03))
  const report = { scenario: 'Chromium mobile viewport, same context, cold navigation then warm reload and Map tab', viewport: { width: 390, height: 844, deviceScaleFactor: 3 }, sameCamera: poseClose, cold, warm }
  await mkdir(resolve(output, '..'), { recursive: true })
  await writeFile(output, JSON.stringify(report, null, 2) + '\n')
  if (!poseClose) throw new Error(`Camera differs between passes; inspect ${output}`)
  process.stdout.write(`${output}\n`)
} finally { await browser.close() }
