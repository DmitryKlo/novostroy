import type { DeveloperUnitCriteria, RegistryFilters } from './api'

export function selectedDeveloperUnitCriteria(filters: RegistryFilters): DeveloperUnitCriteria {
  const { price_min_rub, price_max_rub, price_per_m2_min_rub, price_per_m2_max_rub, rooms, area_min_m2, area_max_m2, unit_floor_min, unit_floor_max, district, okrug_id, max_noise_index } = filters
  return { price_min_rub, price_max_rub, price_per_m2_min_rub, price_per_m2_max_rub, rooms, area_min_m2, area_max_m2, unit_floor_min, unit_floor_max, district, okrug_id, max_noise_index }
}

export type DeveloperUnitFilterDraft = {
  priceMinMln: string; priceMaxMln: string; pricePerM2MinThousand: string; pricePerM2MaxThousand: string;
  rooms: string; areaMinM2: string; areaMaxM2: string; floorMin: string; floorMax: string;
  okrugId: string; noiseMax: string
}

export const emptyDeveloperUnitFilters = (): DeveloperUnitFilterDraft => ({
  priceMinMln: '', priceMaxMln: '', pricePerM2MinThousand: '', pricePerM2MaxThousand: '',
  rooms: '', areaMinM2: '', areaMaxM2: '', floorMin: '', floorMax: '', okrugId: '', noiseMax: '',
})

export const developerUnitFilterCount = (draft: DeveloperUnitFilterDraft): number => Object.values(draft).filter(Boolean).length

export function parseDeveloperUnitFilters(draft: DeveloperUnitFilterDraft): { filters: RegistryFilters; error: string | null } {
  const fields: { key: keyof DeveloperUnitFilterDraft; target: keyof RegistryFilters; label: string; factor: number; integer: boolean; positive: boolean }[] = [
    { key: 'priceMinMln', target: 'price_min_rub', label: 'Цена от', factor: 1_000_000, integer: false, positive: false },
    { key: 'priceMaxMln', target: 'price_max_rub', label: 'Цена до', factor: 1_000_000, integer: false, positive: false },
    { key: 'pricePerM2MinThousand', target: 'price_per_m2_min_rub', label: 'Цена за м² от', factor: 1_000, integer: false, positive: false },
    { key: 'pricePerM2MaxThousand', target: 'price_per_m2_max_rub', label: 'Цена за м² до', factor: 1_000, integer: false, positive: false },
    { key: 'rooms', target: 'rooms', label: 'Комнаты', factor: 1, integer: true, positive: false },
    { key: 'areaMinM2', target: 'area_min_m2', label: 'Площадь от', factor: 1, integer: false, positive: true },
    { key: 'areaMaxM2', target: 'area_max_m2', label: 'Площадь до', factor: 1, integer: false, positive: true },
    { key: 'floorMin', target: 'unit_floor_min', label: 'Этаж квартиры от', factor: 1, integer: true, positive: true },
    { key: 'floorMax', target: 'unit_floor_max', label: 'Этаж квартиры до', factor: 1, integer: true, positive: true },
  ]
  const filters: RegistryFilters = {}
  for (const field of fields) {
    const raw = draft[field.key].trim()
    if (!raw) continue
    if (!/^\d+(?:[.,]\d+)?$/.test(raw)) return { filters: {}, error: `${field.label}: укажите число без дополнительных символов.` }
    const value = Number(raw.replace(',', '.'))
    const converted = value * field.factor
    if (!Number.isFinite(converted) || !Number.isSafeInteger(Math.round(converted)) || (field.integer && !Number.isInteger(value))
      || (field.positive ? value <= 0 : value < 0)) return { filters: {}, error: `${field.label}: значение вне допустимого диапазона.` }
    if (field.factor > 1 && Math.abs(converted - Math.round(converted)) > 1e-5) return { filters: {}, error: `${field.label}: точность меньше одного рубля не поддерживается.` }
    Object.assign(filters, { [field.target]: field.factor > 1 ? Math.round(converted) : value })
  }
  for (const [min, max, label] of [
    ['price_min_rub', 'price_max_rub', 'Цена'],
    ['price_per_m2_min_rub', 'price_per_m2_max_rub', 'Цена за м²'],
    ['area_min_m2', 'area_max_m2', 'Площадь'],
    ['unit_floor_min', 'unit_floor_max', 'Этаж квартиры'],
  ] as const) {
    const low = filters[min]; const high = filters[max]
    if (low !== undefined && high !== undefined && low > high) return { filters: {}, error: `${label}: нижняя граница выше верхней.` }
  }
  const okrugId = draft.okrugId.trim()
  if (okrugId) filters.okrug_id = okrugId
  const noiseMax = draft.noiseMax.trim()
  if (noiseMax) {
    if (!/^\d+$/.test(noiseMax) || Number(noiseMax) > 100) return { filters: {}, error: 'Индекс шума: укажите целое число от 0 до 100.' }
    filters.max_noise_index = Number(noiseMax)
  }
  return { filters, error: null }
}

export function developerUnitFilterSummary(filters: RegistryFilters): string | null {
  const clauses: string[] = []
  if (filters.price_min_rub != null || filters.price_max_rub != null) clauses.push(`цена в снимке ${filters.price_min_rub != null ? `от ${filters.price_min_rub / 1_000_000}` : ''}${filters.price_min_rub != null && filters.price_max_rub != null ? ' ' : ''}${filters.price_max_rub != null ? `до ${filters.price_max_rub / 1_000_000}` : ''} млн ₽`)
  if (filters.price_per_m2_min_rub != null || filters.price_per_m2_max_rub != null) clauses.push(`за м² ${filters.price_per_m2_min_rub != null ? `от ${filters.price_per_m2_min_rub / 1_000}` : ''}${filters.price_per_m2_min_rub != null && filters.price_per_m2_max_rub != null ? ' ' : ''}${filters.price_per_m2_max_rub != null ? `до ${filters.price_per_m2_max_rub / 1_000}` : ''} тыс ₽`)
  if (filters.rooms != null) clauses.push(`${filters.rooms} комн.`)
  if (filters.area_min_m2 != null || filters.area_max_m2 != null) clauses.push(`площадь ${filters.area_min_m2 != null ? `от ${filters.area_min_m2}` : ''}${filters.area_min_m2 != null && filters.area_max_m2 != null ? ' ' : ''}${filters.area_max_m2 != null ? `до ${filters.area_max_m2}` : ''} м²`)
  if (filters.unit_floor_min != null || filters.unit_floor_max != null) clauses.push(`этаж квартиры ${filters.unit_floor_min != null ? `от ${filters.unit_floor_min}` : ''}${filters.unit_floor_min != null && filters.unit_floor_max != null ? ' ' : ''}${filters.unit_floor_max != null ? `до ${filters.unit_floor_max}` : ''}`)
  return clauses.length ? clauses.join(' · ') : null
}
