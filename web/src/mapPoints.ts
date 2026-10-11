import type { MapMarker, MapPoint, MapPointsResult } from './api'
import type { MapPointDisplayItem, ProvenMapPointGroup } from './mapPointGroups'
import { developerDisplayCaption } from './developerBuildingFacts'

const validLocation = (point: MapPoint): boolean => point.location.length === 2
  && Number.isFinite(point.location[0]) && Number.isFinite(point.location[1])
  && point.location[0] >= -180 && point.location[0] <= 180 && point.location[1] >= -90 && point.location[1] <= 90

const markerForPoint = (point: MapPoint): Extract<MapMarker, { point_type: 'registry' | 'mapped_construction' | 'developer_building' }> => ({
    id: point.point_id, point_type: point.point_type, name: point.name,
    label: point.name === 'Строящийся объект на карте'
      ? point.complex_label ?? point.address ?? point.name : point.name,
    location: point.location,
  })

export function pointMarkers(points: MapPointsResult): MapMarker[] {
  return points.items.filter(validLocation).map(markerForPoint)
}

/** Group markers are visual-only; their IDs can never be used as a nearby origin. */
export const groupMarkerId = (group: ProvenMapPointGroup): string => `group:${group.group_id}`

export function pointDisplayMarkers(items: readonly MapPointDisplayItem[]): MapMarker[] {
  return items.flatMap<MapMarker>((item): MapMarker[] => {
    if (item.kind === 'point') return validLocation(item.point) ? [markerForPoint(item.point)] : []
    const { group } = item
    const id = groupMarkerId(group)
    return [{ id, point_type: 'complex_group' as const,
      name: group.group_label, location: group.visual_anchor,
      group_member_locations: group.members.map((member) => member.location) },
    ...group.members.filter(validLocation).map((point) => ({ ...markerForPoint(point), fallback_group_id: id }))]
  })
}

/** Exact coordinates are grouped only for selection; distinct source identities are never merged. */
export function coincidentPoints(items: readonly MapPoint[], pointId: string): MapPoint[] {
  const selected = items.find((point) => point.point_id === pointId)
  if (!selected) return []
  return items.filter((point) => point.location[0] === selected.location[0] && point.location[1] === selected.location[1])
}

export function pointAction(point: MapPoint): { kind: 'registry'; id: string } | { kind: 'candidate'; id: string } {
  return point.point_type !== 'mapped_construction' ? { kind: 'registry', id: point.registry_id }
    : { kind: 'candidate', id: point.candidate_id }
}

export function pointAddressCaption(point: MapPoint): string {
  if (!point.address) return 'Адрес не указан'
  if (point.point_type === 'developer_building') return `${point.address_complete === false ? 'Частичный адрес' : 'Адрес'}: ${point.address}`
  if (point.address_basis === 'registry_source') return `Адрес по реестру: ${point.address}`
  if (point.address_basis === 'osm_street_house') return `Адрес на карте: ${point.address}`
  return `Ориентир на карте: ${point.address}`
}

export function pointComplexCaption(point: MapPoint): string | null {
  if (point.point_type !== 'mapped_construction' || !point.complex_label) return null
  return `Подпись ЖК на контуре: ${point.complex_label}`
}

export function pointComplexCardCaption(point: MapPoint, group: ProvenMapPointGroup | null): string {
  if (point.point_type === 'developer_building') return point.complex_label
    ? `ЖК: ${point.complex_label}` : group?.member_ids.includes(point.point_id)
      ? `ЖК: ${group.group_label}` : 'ЖК не указан'
  return group?.member_ids.includes(point.point_id)
    ? `Подтверждённый ЖК: ${group.group_label}`
    : pointComplexCaption(point) ?? 'Название ЖК не указано в источнике'
}

export function pointDeveloperCardCaption(point: MapPoint): string | null {
  return point.point_type === 'developer_building'
    ? developerDisplayCaption(point) : null
}

export function pointRegistryLinkCaption(point: MapPoint, group: ProvenMapPointGroup | null): string | null {
  if (point.point_type !== 'mapped_construction') return null
  return group?.member_ids.includes(point.point_id) && group.identity_basis === 'verified_crosswalk'
    ? 'Связь с реестром подтверждена' : 'Связь с реестром не подтверждена'
}

export function unlocatedRegistryCount(registryTotal: number, registryPointTotal: number): number {
  return Math.max(0, registryTotal - registryPointTotal)
}
