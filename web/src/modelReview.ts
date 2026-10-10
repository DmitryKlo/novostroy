import type { ModelPreviewGeometry } from './api'

export const MODEL_PAGE_SIZE = 24

export type ModelDecision = 'ready' | 'review' | 'rejected' | 'unavailable'
export type ModelReviewFilters = {
  decision: ModelDecision | ''
  reason_code: string
  source_group: string
  registry_id: string
  query: string
  offset: number
}

export const decisionLabels: Record<ModelDecision, string> = {
  ready: 'Можно показать',
  review: 'Нужна проверка',
  rejected: 'Отклонена',
  unavailable: 'Нет модели',
}

export const reasonLabels: Record<string, string> = {
  no_corpus_link: 'Не установлена связь с корпусом',
  no_measured_geometry: 'Недостаточно размеров',
  unverified_location: 'Местоположение не подтверждено',
  outdated_version: 'Устаревшая версия',
  rights_unconfirmed: 'Права не подтверждены',
  crs_or_datum_unknown: 'Неизвестна система координат или высот',
  conflicting_evidence: 'Источники противоречат друг другу',
  not_moscow_residential: 'Не жилой объект Москвы',
  non_building_infrastructure: 'Инфраструктура, а не жилое здание',
  source_unavailable: 'Исходный материал недоступен',
}

export const reasonNextSteps: Record<string, string> = {
  no_corpus_link: 'Нужен документ, который однозначно связывает контур с корпусом или секцией.',
  no_measured_geometry: 'Нужны проверенные контур и размеры из плана или разрешённой модели.',
  unverified_location: 'Нужна геопривязка по плану и контрольным точкам.',
  outdated_version: 'Нужна актуальная редакция проекта с датой.',
  rights_unconfirmed: 'Нужно разрешение правообладателя на хранение, переработку и показ.',
  crs_or_datum_unknown: 'Нужны система координат, единицы и высотная основа.',
  conflicting_evidence: 'Нужно сверить противоречивые версии документов и выбрать подтверждённую.',
  not_moscow_residential: 'Нужно подтвердить тип объекта и его расположение.',
  non_building_infrastructure: 'Для показа жилого корпуса нужен отдельный проверенный контур здания; инфраструктурная запись им не является.',
  source_unavailable: 'Нужен доступный разрешённый исходный документ или файл.',
}

const knownDecisions = new Set<ModelDecision>(['ready', 'review', 'rejected', 'unavailable'])

export function parseModelReviewQuery(search: string): ModelReviewFilters {
  const params = new URLSearchParams(search)
  const rawDecision = params.get('decision') ?? 'rejected'
  const decision = rawDecision === '' || knownDecisions.has(rawDecision as ModelDecision) ? rawDecision as ModelDecision | '' : 'rejected'
  const rawOffset = Number(params.get('offset') ?? 0)
  return {
    decision,
    reason_code: (params.get('reason_code') ?? '').slice(0, 80),
    source_group: (params.get('source_group') ?? '').slice(0, 80),
    registry_id: /^registry-[a-f0-9]{24}$/.test(params.get('registry_id') ?? '') ? params.get('registry_id')! : '',
    query: (params.get('query') ?? '').trim().slice(0, 120),
    offset: Number.isSafeInteger(rawOffset) && rawOffset >= 0 ? Math.floor(rawOffset / MODEL_PAGE_SIZE) * MODEL_PAGE_SIZE : 0,
  }
}

export function modelReviewSearch(filters: ModelReviewFilters): string {
  const params = new URLSearchParams()
  params.set('decision', filters.decision)
  if (filters.reason_code) params.set('reason_code', filters.reason_code)
  if (filters.source_group) params.set('source_group', filters.source_group)
  if (filters.registry_id) params.set('registry_id', filters.registry_id)
  if (filters.query) params.set('query', filters.query)
  if (filters.offset) params.set('offset', String(filters.offset))
  return params.toString()
}

export function safeModelNextStep(reasonCodes: readonly string[], fromApi?: string | null): string {
  if (fromApi?.trim()) return fromApi.trim()
  return reasonCodes.map((code) => reasonNextSteps[code]).filter(Boolean).join(' ') || 'Нужны подтверждённые сведения о модели; проверьте запись позже.'
}

export function isRenderableDecision(decision: string): boolean {
  return decision === 'ready'
}

/** A selected 3D root may start only after a real flat frame and healthy active-frame promotion. */
export function selectedModelVolumeEligible(depiction: string | null, renderHeightM: number | null | undefined): boolean {
  return depiction === 'schematic_massing' && typeof renderHeightM === 'number' && Number.isFinite(renderHeightM) && renderHeightM > 0
}

/** Keep every polygon and courtyard ring intact; the selected API bounds this to 512 coordinates. */
export function selectedPreviewPaths(geometry: ModelPreviewGeometry | null, fallback: [number, number][] | null, parts: [number, number][][] = []): { polygons: string[]; parts: string[] } | null {
  const polygons = geometry?.type === 'Polygon' ? [geometry.coordinates]
    : geometry?.type === 'MultiPolygon' ? geometry.coordinates : fallback ? [[fallback]] : null
  if (!polygons?.length) return null
  const rings = [...polygons.flat(), ...parts]
  const points = rings.flat()
  if (!rings.length || points.length > 512 || points.length < 3 || polygons.some((polygon) => !polygon.length)
    || rings.some((ring) => ring.length < 3 || ring.some(([lon, lat]) => !Number.isFinite(lon) || !Number.isFinite(lat) || lon < -180 || lon > 180 || lat < -90 || lat > 90))) return null
  const latitude = points.reduce((sum, point) => sum + point[1], 0) / points.length
  const scaleX = Math.max(0.1, Math.cos(latitude * Math.PI / 180))
  const xs = points.map(([lon]) => lon * scaleX)
  const ys = points.map(([, lat]) => lat)
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys)
  const factor = Math.min(280 / Math.max(maxX - minX, 0.000001), 160 / Math.max(maxY - minY, 0.000001))
  const point = ([lon, lat]: [number, number]) => `${(160 + (lon * scaleX - (minX + maxX) / 2) * factor).toFixed(1)} ${(100 - (lat - (minY + maxY) / 2) * factor).toFixed(1)}`
  return {
    polygons: polygons.map((polygon) => polygon.map((ring) => `M ${ring.map(point).join(' L ')} Z`).join(' ')),
    parts: parts.map((ring) => `M ${ring.map(point).join(' L ')} Z`),
  }
}

export function selectedModelRequestAllowed(input: { flatPresented: boolean; visible: boolean; decision: string; assetReadiness: string; qualityStage: string; hasVerifiedFootprint: boolean; depiction: string | null; renderHeightM: number | null | undefined }): boolean {
  return input.flatPresented && input.visible && input.decision === 'ready' && input.assetReadiness === 'ready_to_render'
    && input.qualityStage !== 'flat' && input.hasVerifiedFootprint && selectedModelVolumeEligible(input.depiction, input.renderHeightM)
}

export function modelProgress(scanned: number, counts: { ready?: number; review?: number; rejected?: number; unavailable?: number }): { assessed: number; pending: number; renderable: number } {
  const renderable = Math.max(0, counts.ready ?? 0)
  const assessed = Math.max(0, renderable + (counts.review ?? 0) + (counts.rejected ?? 0) + (counts.unavailable ?? 0))
  return { assessed, pending: Math.max(0, scanned - assessed), renderable }
}
