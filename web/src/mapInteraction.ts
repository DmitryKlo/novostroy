export type PickCandidate = {
  id?: { id?: unknown } | string
  getProperty?: (name: string) => unknown
  tileset?: object
  content?: { tileset?: object }
  primitive?: { tileset?: object } | object
}

export type MapPick = { kind: 'marker' | 'nearby'; id: string } | { kind: 'facade'; id: string } | null

/** Prefer an actual point hit over a road, selected label, or 3D tile in the same pick stack. */
export function resolveMapPick(picks: readonly PickCandidate[], _selectedId: string | null, facadeIdFor: (object: object) => string | undefined): MapPick {
  for (const picked of picks) {
    const entityId = typeof picked.id === 'string' ? picked.id : picked.id?.id
    if (typeof entityId !== 'string') continue
    if (entityId.startsWith('complex:')) return { kind: 'marker', id: entityId.slice(8) }
  }
  for (const picked of picks) {
    const entityId = typeof picked.id === 'string' ? picked.id : picked.id?.id
    if (typeof entityId === 'string' && entityId.startsWith('nearby:')) return { kind: 'nearby', id: entityId.slice(7) }
  }
  for (const picked of picks) {
    const featureId = picked.getProperty?.('complex_id')
    if (typeof featureId === 'string' && featureId) return { kind: 'facade', id: featureId }
    const tileset = picked.tileset ?? picked.content?.tileset ?? ('tileset' in (picked.primitive ?? {}) ? (picked.primitive as { tileset?: object }).tileset : undefined) ?? picked.primitive
    if (tileset) {
      const id = facadeIdFor(tileset)
      if (id) return { kind: 'facade', id }
    }
  }
  return null
}

/** A request token makes a repeated explicit selection different from a passive data refresh. */
export function selectionFocusKey(id: string | null, requestId: number): string | null {
  return id ? `${id}:${requestId}` : null
}

export function shouldFlyToSelection(key: string | null, lastKey: string | null, focus: boolean, restoreCamera: boolean): boolean {
  return Boolean(key && key !== lastKey && focus && !restoreCamera)
}

/** Registry marker maps open at a stable Moscow pose; their first data response must not start a competing flight. */
export function shouldStartInitialCameraFlight(hasRegistryMarkers: boolean, alreadyFramed: boolean): boolean {
  return !hasRegistryMarkers && !alreadyFramed
}
