export type ProjectedMarker = {
  id: string
  x: number
  y: number
  front: boolean
  longitude: number
  latitude: number
  world: readonly [number, number, number]
  /** Every source member needs loaded ground before one visual group may replace them. */
  groupMembers?: readonly GroundPosition[]
}

export type GroundPosition = {
  longitude: number
  latitude: number
  world: readonly [number, number, number]
}

export type VisibleGround = {
  epoch: number
  center: readonly [number, number, number]
  radius: number
  region: readonly [number, number, number, number] | null
  geometricError: number
}

export function markerIdsOnLoadedGround(
  markers: readonly ProjectedMarker[],
  ground: readonly VisibleGround[],
  view: { epoch: number; width: number; height: number; visible: boolean; requirePrecise: boolean },
): Set<string> {
  const result = new Set<string>()
  if (!view.visible || view.width <= 0 || view.height <= 0) return result
  const currentGround = ground.filter((tile) => tile.epoch === view.epoch && Number.isFinite(tile.radius) && tile.radius > 0 && tile.region
    && (!view.requirePrecise || tile.geometricError <= 2))
  if (!currentGround.length) return result
  const onGround = (position: GroundPosition): boolean => currentGround.some((tile) => {
    const [x, y, z] = position.world
    const [cx, cy, cz] = tile.center
    const radius = tile.radius + 50 // Ground is below a 36 m point glyph; include that visual height.
    if ((x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2 > radius ** 2) return false
    const [west, south, east, north] = tile.region!
    const epsilon = 1e-7 // Tile-edge rounding tolerance only, about 0.6 m at Moscow latitude.
    const longitudeInside = west <= east
      ? position.longitude >= west - epsilon && position.longitude <= east + epsilon
      : position.longitude >= west - epsilon || position.longitude <= east + epsilon
    return longitudeInside && position.latitude >= south - epsilon && position.latitude <= north + epsilon
  })
  for (const marker of markers) {
    if (!marker.front || !Number.isFinite(marker.x) || !Number.isFinite(marker.y)
      || marker.x < 0 || marker.x > view.width || marker.y < 0 || marker.y > view.height) continue
    if (onGround(marker) && (!marker.groupMembers || marker.groupMembers.every(onGround))) result.add(marker.id)
  }
  return result
}

export function markerEntityChanges(active: ReadonlySet<string>, desired: ReadonlySet<string>): { remove: string[]; add: string[] } {
  return {
    remove: [...active].filter((id) => !desired.has(id)),
    add: [...desired].filter((id) => !active.has(id)),
  }
}

/** Retain covered original points until the visual group itself has complete loaded-ground coverage. */
export function collapseCoveredGroupMembers(
  loaded: ReadonlySet<string>,
  markers: readonly { id: string; fallback_group_id?: string }[],
): Set<string> {
  const visible = new Set(loaded)
  for (const marker of markers) if (marker.fallback_group_id && visible.has(marker.fallback_group_id)) visible.delete(marker.id)
  return visible
}
