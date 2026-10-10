import type { NearbyCategory, NearbyEducation, NearbyPoi, NearbySelection, NearbyViewport } from './api'

export const NEARBY_CATEGORIES: NearbyCategory[] = ['metro', 'education', 'medical']
export const NEARBY_LABELS: Record<NearbyCategory, string> = { metro: 'Метро', education: 'Школы и детсады', medical: 'Медицина' }
export const NEARBY_COLORS: Record<NearbyCategory, string> = { metro: '#a4437f', education: '#318a50', medical: '#247f9d' }
export type NearbyVisibility = Record<NearbyCategory, boolean>
export const noNearbyLayers = (): NearbyVisibility => ({ metro: false, education: false, medical: false })

export function nearbyKey(registrySnapshot: string, mapRun: string, pointId: string, education: NearbyEducation): string {
  return JSON.stringify([registrySnapshot, mapRun, pointId, education])
}

/** A short in-session reuse window; HTTP validators remain authoritative after expiry. */
export class NearbyCache<T = NearbySelection> {
  private entries = new Map<string, { value: T; expires: number; bytes: number }>()
  private bytes = 0
  constructor(private readonly maxEntries = 12, private readonly maxAgeMs = 60_000, private readonly maxBytes = 2 * 1024 * 1024) {}
  get(key: string, now = Date.now()): T | null {
    const entry = this.entries.get(key)
    if (!entry) return null
    this.entries.delete(key)
    if (entry.expires <= now) { this.bytes -= entry.bytes; return null }
    this.entries.set(key, entry)
    return entry.value
  }
  set(key: string, value: T, now = Date.now()): void {
    const previous = this.entries.get(key)
    if (previous) this.bytes -= previous.bytes
    this.entries.delete(key)
    const bytes = JSON.stringify(value).length * 2
    if (bytes > this.maxBytes) return
    this.entries.set(key, { value, expires: now + this.maxAgeMs, bytes })
    this.bytes += bytes
    while (this.entries.size > this.maxEntries || this.bytes > this.maxBytes) {
      const oldestKey = this.entries.keys().next().value!
      this.bytes -= this.entries.get(oldestKey)!.bytes
      this.entries.delete(oldestKey)
    }
  }
}

/** The viewport endpoint is deliberately limited to roughly one district. */
export function boundedNearbyBbox(raw: [number, number, number, number] | null): [number, number, number, number] | null {
  if (!raw || !raw.every(Number.isFinite) || raw[0] >= raw[2] || raw[1] >= raw[3]
    || raw[2] - raw[0] > 0.15 || raw[3] - raw[1] > 0.15) return null
  const bbox: [number, number, number, number] = [Math.floor(raw[0] * 10_000) / 10_000, Math.floor(raw[1] * 10_000) / 10_000,
    Math.ceil(raw[2] * 10_000) / 10_000, Math.ceil(raw[3] * 10_000) / 10_000]
  return bbox[2] - bbox[0] <= 0.15 && bbox[3] - bbox[1] <= 0.15 ? bbox : null
}

export function nearbyViewportKey(registrySnapshot: string, mapRun: string, bbox: [number, number, number, number], layers: NearbyCategory[], education: NearbyEducation): string {
  return JSON.stringify([registrySnapshot, mapRun, bbox, layers, education])
}

export function validNearbyViewport(value: NearbyViewport, bbox: [number, number, number, number], layers: NearbyCategory[], education: NearbyEducation, registrySnapshot: string, mapRun: string, developerMode = false): boolean {
  return value.status === 'ok' && value.registry_snapshot_id === registrySnapshot
    && (developerMode ? value.map_run_id === mapRun : value.model_run_id === mapRun)
    && value.education === education && JSON.stringify(value.bbox) === JSON.stringify(bbox)
    && layers.every((layer) => Boolean(value.layers[layer]))
}

export function validNearbyResult(value: NearbySelection, pointId: string, education: NearbyEducation, registrySnapshot: string, mapRun: string, developerMode = false): boolean {
  return value.status === 'ok' && value.point_id === pointId && value.education === education
    && value.registry_snapshot_id === registrySnapshot
    && (developerMode ? value.map_run_id === mapRun : value.model_run_id === mapRun)
}

export function shownNearbyItems(value: NearbySelection | null, visibility: NearbyVisibility): { layer: NearbyCategory; item: NearbyPoi }[] {
  if (!value) return []
  return NEARBY_CATEGORIES.flatMap((layer) => visibility[layer]
    ? displayNearbyItems(layer, value.layers[layer]?.items ?? []).map((item) => ({ layer, item })) : [])
}

export function displayNearbyItems(layer: NearbyCategory, items: NearbyPoi[]): NearbyPoi[] {
  if (layer !== 'education') return items.slice(0, 2)
  const counts = { school: 0, kindergarten: 0 }
  return items.filter((item) => {
    if (item.category !== 'school' && item.category !== 'kindergarten') return false
    if (counts[item.category] >= 2) return false
    counts[item.category] += 1
    return true
  })
}

export function walkingCaption(item: NearbyPoi): string {
  const route = item.route
  if (route?.status === 'contains_ferry') return 'Пеший маршрут не найден: рассчитанный путь включает паром'
  if (route?.status !== 'routed' || !Number.isFinite(route.estimated_minutes) || !Number.isFinite(route.distance_m)
    || route.estimated_minutes === null || route.distance_m === null || route.estimated_minutes < 0 || route.distance_m < 0
    || !route.geometry || route.geometry.length < 2) return 'Пеший маршрут пока не подтверждён'
  return `≈ ${new Intl.NumberFormat('ru-RU').format(Math.max(1, Math.ceil(route.estimated_minutes)))} мин пешком · ${new Intl.NumberFormat('ru-RU').format(route.distance_m)} м`
}

export function nearbyWalkingCaption(layer: NearbyCategory, item: NearbyPoi): string {
  if (layer === 'metro' && item.route?.status === 'routed'
    && Number.isFinite(item.route.estimated_minutes) && item.route.estimated_minutes !== null
    && item.route.estimated_minutes > 40 && validRouteGeometry(item)) return 'Метро далеко'
  return walkingCaption(item)
}

function validGeometry(value: [number, number][] | null | undefined): [number, number][] | null {
  if (!Array.isArray(value) || value.length < 2 || value.length > 512) return null
  return value.every((coordinate) => Array.isArray(coordinate) && coordinate.length === 2
    && Number.isFinite(coordinate[0]) && Number.isFinite(coordinate[1])
    && coordinate[0] >= -180 && coordinate[0] <= 180
    && coordinate[1] >= -90 && coordinate[1] <= 90) ? value : null
}

export function validRouteGeometry(item: NearbyPoi): [number, number][] | null {
  return item.route?.status === 'routed' ? validGeometry(item.route.geometry) : null
}

export function validNetworkRouteGeometry(item: NearbyPoi): [number, number][] | null {
  if (item.route?.status !== 'routed') return null
  return item.route.origin_access ? validGeometry(item.route.network_geometry) : validGeometry(item.route.geometry)
}

export function validOriginAccessGeometry(item: NearbyPoi): [number, number][] | null {
  const access = item.route?.origin_access
  if (item.route?.status !== 'routed' || access?.status !== 'estimated' || access.approximate !== true
    || !Number.isFinite(access.distance_m) || access.distance_m < 0
    || !Number.isFinite(access.estimated_minutes) || access.estimated_minutes < 0) return null
  return validGeometry(access.geometry)
}

export function originAccessCaption(item: NearbyPoi): string | null {
  const access = item.route?.origin_access
  return validOriginAccessGeometry(item) && access
    ? `Примерный подход до улицы: ≈ ${new Intl.NumberFormat('ru-RU').format(Math.round(access.distance_m))} м · ≈ ${new Intl.NumberFormat('ru-RU').format(Math.max(1, Math.ceil(access.estimated_minutes)))} мин`
    : null
}
