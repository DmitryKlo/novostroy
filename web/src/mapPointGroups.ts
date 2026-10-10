import type { MapPoint, MapPointsResult } from './api'

/** Guard against a malformed group key spanning most of the city; this is not a project-size claim. */
export const MAX_COMPLEX_GROUP_DIAMETER_M = 10_000
export const MAX_COMPLEX_GROUP_MEMBERS = 1_000

/** Group as the camera starts leaving block scale; keep hysteresis while scrolling. */
export function expandGroupsForCameraHeight(previous: boolean, height: number): boolean {
  if (!Number.isFinite(height)) return previous
  return height <= 1_800 ? true : height >= 2_500 ? false : previous
}

export type MapPointGroupIdentityBasis =
  | 'source_complex_id'
  | 'explicit_relation_membership'
  | 'verified_crosswalk'

/** Supplemental identity evidence; labels and proximity alone are never membership evidence. */
export type MapPointGroupMembership = {
  group_id: string
  group_scope: 'residential_complex'
  identity_basis: MapPointGroupIdentityBasis
  group_label: string
  point_ids: readonly string[]
}

export type MapPointGroupEvidenceBundle = {
  schema_version: 1
  visibility: 'public'
  registry_snapshot_id: string
  model_run_id?: string
  map_run_id?: string
  groups: readonly MapPointGroupMembership[]
}

export type ProvenMapPointGroup = {
  /** Opaque stable ID derived from private source identity; never route/source identity. */
  group_id: string
  group_label: string
  identity_basis: MapPointGroupIdentityBasis
  members: MapPoint[]
  member_ids: string[]
  member_count: number
  bounds: [west: number, south: number, east: number, north: number]
  /** Visual anchor only: never use it as a map-point ID or nearby-route origin. */
  visual_anchor: [longitude: number, latitude: number]
  visual_anchor_basis: 'derived_from_members_for_visual_placement_only'
  max_member_distance_m: number
}

export type MapPointGrouping = {
  groups: ProvenMapPointGroup[]
  individuals: MapPoint[]
}

export type MapPointDisplayItem =
  | { kind: 'complex_group'; group: ProvenMapPointGroup }
  | { kind: 'point'; point: MapPoint }

export type MapPointGroupDisplayOptions = {
  expanded_group_ids?: readonly string[]
  selected_group_id?: string | null
  selected_point_id?: string | null
}

const validCoordinate = (point: MapPoint): boolean => Array.isArray(point.location)
  && point.location.length === 2
  && Number.isFinite(point.location[0]) && Number.isFinite(point.location[1])
  && point.location[0] >= -180 && point.location[0] <= 180
  && point.location[1] >= -90 && point.location[1] <= 90

const validMembership = (membership: MapPointGroupMembership): boolean =>
  typeof membership.group_id === 'string'
  && /^group_[a-f0-9]{24}$/.test(membership.group_id)
  && membership.group_scope === 'residential_complex'
  && ['source_complex_id', 'explicit_relation_membership', 'verified_crosswalk']
    .includes(membership.identity_basis)
  && typeof membership.group_label === 'string'
  && membership.group_label.trim().length > 0
  && membership.group_label.length <= 240
  && Array.isArray(membership.point_ids)
  && membership.point_ids.length >= 2
  && membership.point_ids.length <= MAX_COMPLEX_GROUP_MEMBERS
  && membership.point_ids.every((id) => typeof id === 'string' && id.length > 0)
  && new Set(membership.point_ids).size === membership.point_ids.length

/** Parse the versioned public-safe projection, not a raw OSM/EstateLine source dump. */
export function parseMapPointGroupEvidenceBundle(value: unknown): MapPointGroupEvidenceBundle | null {
  if (typeof value !== 'object' || value === null) return null
  const record = value as Record<string, unknown>
  if (record.schema_version !== 1 || record.visibility !== 'public'
      || typeof record.registry_snapshot_id !== 'string'
      || (typeof record.model_run_id !== 'string' && typeof record.map_run_id !== 'string')
      || !Array.isArray(record.groups)) return null
  const groups = record.groups.filter((item): item is MapPointGroupMembership =>
    typeof item === 'object' && item !== null && validMembership(item as MapPointGroupMembership))
  return {
    schema_version: 1,
    visibility: 'public',
    registry_snapshot_id: record.registry_snapshot_id,
    ...(typeof record.model_run_id === 'string' ? { model_run_id: record.model_run_id } : {}),
    ...(typeof record.map_run_id === 'string' ? { map_run_id: record.map_run_id } : {}),
    groups,
  }
}

const distanceMeters = (a: MapPoint, b: MapPoint): number => {
  const radians = Math.PI / 180
  const [lon1, lat1] = a.location
  const [lon2, lat2] = b.location
  const phi1 = lat1 * radians
  const phi2 = lat2 * radians
  const dPhi = (lat2 - lat1) * radians
  const dLambda = (lon2 - lon1) * radians
  const sinPhi = Math.sin(dPhi / 2)
  const sinLambda = Math.sin(dLambda / 2)
  const haversine = sinPhi * sinPhi + Math.cos(phi1) * Math.cos(phi2) * sinLambda * sinLambda
  return 6_371_000 * 2 * Math.asin(Math.sqrt(Math.min(1, haversine)))
}

function buildGroup(
  membership: MapPointGroupMembership,
  members: MapPoint[],
): ProvenMapPointGroup | null {
  let maxDistance = 0
  for (let i = 0; i < members.length; i += 1) {
    for (let j = i + 1; j < members.length; j += 1) {
      maxDistance = Math.max(maxDistance, distanceMeters(members[i], members[j]))
    }
  }
  if (!Number.isFinite(maxDistance) || maxDistance > MAX_COMPLEX_GROUP_DIAMETER_M) return null

  const stableMembers = [...members].sort((left, right) => left.point_id.localeCompare(right.point_id))
  const longitudes = stableMembers.map((point) => point.location[0])
  const latitudes = stableMembers.map((point) => point.location[1])
  const visualAnchor: [number, number] = [
    longitudes.reduce((sum, value) => sum + value, 0) / members.length,
    latitudes.reduce((sum, value) => sum + value, 0) / members.length,
  ]
  return {
    group_id: membership.group_id,
    group_label: membership.group_label,
    identity_basis: membership.identity_basis,
    members: stableMembers,
    member_ids: stableMembers.map((point) => point.point_id),
    member_count: stableMembers.length,
    bounds: [Math.min(...longitudes), Math.min(...latitudes), Math.max(...longitudes), Math.max(...latitudes)],
    visual_anchor: visualAnchor,
    visual_anchor_basis: 'derived_from_members_for_visual_placement_only',
    max_member_distance_m: maxDistance,
  }
}

/**
 * Collapse only current-snapshot members backed by a source-owned complex identity.
 * Missing, stale, ambiguous, conflicting, singleton, or geographically implausible
 * memberships leave every original point independently selectable.
 */
export function groupMapPoints(
  points: MapPointsResult,
  evidence: MapPointGroupEvidenceBundle | null | undefined,
): MapPointGrouping {
  const items = Array.isArray(points?.items) ? points.items : []
  const allIndividuals = (): MapPointGrouping => ({ groups: [], individuals: [...items] })
  if (!evidence
      || evidence.schema_version !== 1
      || evidence.visibility !== 'public'
      || evidence.registry_snapshot_id !== points.registry_snapshot_id
      || (points.dataset_id === 'developer_map_points'
        ? !points.map_run_id || evidence.map_run_id !== points.map_run_id
        : !points.model_run_id || evidence.model_run_id !== points.model_run_id)
      || !Array.isArray(evidence.groups)
      || evidence.groups.length === 0) return allIndividuals()

  const pointCounts = new Map<string, number>()
  const pointById = new Map<string, MapPoint>()
  for (const point of items) {
    pointCounts.set(point.point_id, (pointCounts.get(point.point_id) ?? 0) + 1)
    pointById.set(point.point_id, point)
  }

  // A duplicated group ID or a point claimed by multiple group IDs is ambiguous;
  // fail closed for all involved memberships instead of picking one arbitrarily.
  const groupIdCounts = new Map<string, number>()
  const pointClaims = new Map<string, Set<string>>()
  for (const membership of evidence.groups as readonly MapPointGroupMembership[]) {
    if (!validMembership(membership)) continue
    groupIdCounts.set(membership.group_id, (groupIdCounts.get(membership.group_id) ?? 0) + 1)
    for (const pointId of membership.point_ids) {
      const claims = pointClaims.get(pointId) ?? new Set<string>()
      claims.add(membership.group_id)
      pointClaims.set(pointId, claims)
    }
  }

  const accepted: ProvenMapPointGroup[] = []
  const groupedPointIds = new Set<string>()
  for (const membership of evidence.groups as readonly MapPointGroupMembership[]) {
    if (!validMembership(membership) || groupIdCounts.get(membership.group_id) !== 1
      || (points.dataset_id === 'developer_map_points' && membership.identity_basis !== 'source_complex_id')) continue
    if (membership.point_ids.some((id: string) => (pointClaims.get(id)?.size ?? 0) !== 1
      || pointCounts.get(id) !== 1)) continue
    const members: MapPoint[] = membership.point_ids.map((id: string) => pointById.get(id)!)
    if (members.length < 2 || members.some((point: MapPoint) => !validCoordinate(point))) continue
    const group = buildGroup(membership, members)
    if (!group) continue
    accepted.push(group)
    for (const id of membership.point_ids) groupedPointIds.add(id)
  }

  return {
    groups: accepted.sort((left, right) => left.group_id.localeCompare(right.group_id)),
    individuals: items.filter((point) => !groupedPointIds.has(point.point_id)),
  }
}

/** Convenience for a raw API/file response; malformed or private data keeps all points independent. */
export function projectMapPointGroups(points: MapPointsResult, value: unknown): MapPointGrouping {
  return groupMapPoints(points, parseMapPointGroupEvidenceBundle(value))
}

/** Keep one group marker at overview; zoom or selection returns the original point identities. */
export function mapPointDisplayItems(
  grouping: MapPointGrouping,
  options: MapPointGroupDisplayOptions = {},
): MapPointDisplayItem[] {
  const expanded = new Set(options.expanded_group_ids ?? [])
  const output: MapPointDisplayItem[] = []
  for (const group of grouping.groups) {
    const expand = expanded.has(group.group_id)
      || options.selected_group_id === group.group_id
      || (options.selected_point_id !== null && options.selected_point_id !== undefined
        && group.member_ids.includes(options.selected_point_id))
    if (expand) output.push(...group.members.map((point) => ({ kind: 'point' as const, point })))
    else output.push({ kind: 'complex_group', group })
  }
  output.push(...grouping.individuals.map((point) => ({ kind: 'point' as const, point })))
  return output
}
