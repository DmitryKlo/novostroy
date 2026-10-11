import { RegistrySnapshotChanged, type RegistryFilters } from './api'

export type DeveloperOverlayExpectedIdentity = {
  registry_snapshot_id: string
  map_run_id: string
  unit_run_id: string
  enrichment_run_id: string
  overlay_run_id: string
}

export type DeveloperOverlayViewport = DeveloperOverlayExpectedIdentity & {
  dataset_id: 'developer_source_geometry_overlay'
  tileset_url: string
  city_patch_url: string | null
  lake_persistent: boolean
  lake_registry_id: string | null
  lake_persistent_model_sha256: string | null
  facade_style: {
    basis: 'project_style_inference'
    label: 'Реконструкция'
    styled_projects_total: number
    styled_persistent_features_total: number
    texture_level: 'near'
  } | null
  bbox: [number, number, number, number]
  features_total: number
  matched_total: number
  limit: number
  offset: number
  truncated: boolean
  eligible_registry_ids: string[]
  applied_filters: Record<string, unknown>
}

const FILTER_KEYS = new Set([
  'query', 'district', 'okrug_id', 'max_noise_index', 'max_air_index',
  'price_min_rub', 'price_max_rub', 'price_per_m2_min_rub',
  'price_per_m2_max_rub', 'rooms', 'area_min_m2', 'area_max_m2',
  'unit_floor_min', 'unit_floor_max',
])
const RUN = /^developer-overlay-v1-[0-9a-f]{16}$/
const REGISTRY_ID = /^registry-[0-9a-f]{24}$/
const SHA256 = /^[0-9a-f]{64}$/

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function count(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0
}

function viewportUrl(
  bbox: [number, number, number, number], filters: RegistryFilters,
  limit: number, offset: number,
): string {
  const [west, south, east, north] = bbox
  if (bbox.some((value) => !Number.isFinite(value)) || west < -180 || east > 180
    || south < -90 || north > 90 || west >= east || south >= north
    || east - west > 10 || north - south > 10) {
    throw new Error('Неверные границы области карты.')
  }
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 1500
    || !Number.isSafeInteger(offset) || offset < 0 || offset > 100000) {
    throw new Error('Неверная страница геометрии домов.')
  }
  const params = new URLSearchParams({ west: String(west), south: String(south),
    east: String(east), north: String(north), limit: String(limit), offset: String(offset) })
  for (const [key, value] of Object.entries(filters).sort(([a], [b]) => a.localeCompare(b))) {
    if (value === undefined || value === null || key === 'limit' || key === 'offset') continue
    if (!FILTER_KEYS.has(key)) {
      throw new Error(`Неподдерживаемый фильтр геометрии домов: ${key}`)
    }
    if (typeof value === 'number' && Number.isFinite(value)) params.set(key, String(value))
    else if (typeof value === 'string') params.set(key, value)
    else throw new Error(`Неверное значение фильтра геометрии домов: ${key}`)
  }
  return `/v1/developer-overlay/viewport?${params.toString()}`
}

function checkedViewport(
  value: unknown, expected: DeveloperOverlayExpectedIdentity,
  bbox: [number, number, number, number], limit: number, offset: number,
): DeveloperOverlayViewport {
  const changed = () => new RegistrySnapshotChanged(
    'Геометрия и список домов относятся к разным снимкам. Обновите страницу.')
  if (!record(value) || value.dataset_id !== 'developer_source_geometry_overlay'
    || !RUN.test(expected.overlay_run_id)
    || value.registry_snapshot_id !== expected.registry_snapshot_id
    || value.map_run_id !== expected.map_run_id
    || value.unit_run_id !== expected.unit_run_id
    || value.enrichment_run_id !== expected.enrichment_run_id
    || value.overlay_run_id !== expected.overlay_run_id) throw changed()
  const tileset = `/v1/developer-overlay/${expected.overlay_run_id}/tileset.json`
  const patch = `/v1/developer-overlay/${expected.overlay_run_id}/city-patch/tileset.json`
  if (value.tileset_url !== tileset || (value.city_patch_url !== null && value.city_patch_url !== patch)
    || typeof value.lake_persistent !== 'boolean'
    || (value.lake_persistent
      ? (typeof value.lake_registry_id !== 'string' || !REGISTRY_ID.test(value.lake_registry_id)
        || typeof value.lake_persistent_model_sha256 !== 'string'
        || !SHA256.test(value.lake_persistent_model_sha256) || value.city_patch_url !== patch)
      : (value.lake_registry_id !== null || value.lake_persistent_model_sha256 !== null))
    || (value.facade_style !== null && (!record(value.facade_style)
      || value.facade_style.basis !== 'project_style_inference'
      || value.facade_style.label !== 'Реконструкция'
      || value.facade_style.texture_level !== 'near'
      || !count(value.facade_style.styled_projects_total)
      || !count(value.facade_style.styled_persistent_features_total)))
    || !Array.isArray(value.bbox) || value.bbox.length !== 4
    || value.bbox.some((coordinate, index) => coordinate !== bbox[index])
    || !count(value.features_total) || !count(value.matched_total)
    || value.matched_total > value.features_total
    || value.limit !== limit || value.offset !== offset
    || typeof value.truncated !== 'boolean'
    || value.truncated !== (offset + limit < value.matched_total)
    || !Array.isArray(value.eligible_registry_ids)
    || value.eligible_registry_ids.length > limit
    || value.eligible_registry_ids.some((id) => typeof id !== 'string' || !REGISTRY_ID.test(id))
    || new Set(value.eligible_registry_ids).size !== value.eligible_registry_ids.length
    || !record(value.applied_filters)) throw changed()
  return value as DeveloperOverlayViewport
}

export async function loadDeveloperOverlayViewport(
  bbox: [number, number, number, number], filters: RegistryFilters,
  expected: DeveloperOverlayExpectedIdentity, signal?: AbortSignal,
  page: { limit?: number; offset?: number } = {},
): Promise<DeveloperOverlayViewport> {
  const limit = page.limit ?? 1200
  const offset = page.offset ?? 0
  const url = viewportUrl(bbox, filters, limit, offset)
  let response: Response
  try {
    response = await fetch(url, { signal, cache: 'no-store' })
  } catch (error) {
    if (signal?.aborted) throw error
    throw new Error('Нет соединения с геометрией домов. Попробуйте ещё раз.')
  }
  if (!response.ok) {
    let message = `Геометрия домов временно недоступна (${response.status}).`
    try {
      const body: unknown = await response.json()
      if (record(body) && record(body.detail) && typeof body.detail.message === 'string') {
        message = body.detail.message
      }
    } catch { /* preserve HTTP status */ }
    throw new Error(message)
  }
  return checkedViewport(await response.json() as unknown, expected, bbox, limit, offset)
}
