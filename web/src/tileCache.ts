/** Reconcile live Cesium tilesets without mutating the Map iterator being visited. */
export function stagedCityVersion(url: string): 'v6' | 'v7' | null {
  const match = /^\/assets\/public-citywide-(v6|v7)\/tileset\.json(?:\?.*)?$/.exec(url)
  return match ? match[1] as 'v6' | 'v7' : null
}

export function cityVolumesAtHeight(heightMetres: number, wasEnabled: boolean): boolean {
  if (!Number.isFinite(heightMetres)) return false
  return wasEnabled ? heightMetres < 16_000 : heightMetres <= 12_000
}

/** Exact road cells are useful at block scale; keep the coarse region visible while zooming. */
export function roadDetailAtHeight(heightMetres: number, wasEnabled: boolean): boolean {
  if (!Number.isFinite(heightMetres)) return false
  return wasEnabled ? heightMetres < 1_500 : heightMetres <= 1_000
}

export function roadDetailAllowed(nearScale: boolean, moving: boolean, qualityStage: string): boolean {
  return nearScale && !moving && (qualityStage === 'balanced' || qualityStage === 'detail')
}

/** roads-v2 has an empty GE 5000 root and GE 450 renderable regions. Keep root traversal
 * possible at overview heights while stopping before the region's exact cell children. */
export function roadScreenSpaceError(heightMetres: number, canvasHeight: number, detailAllowed: boolean): number {
  if (detailAllowed) return 8
  if (!Number.isFinite(heightMetres) || !Number.isFinite(canvasHeight)) return 24
  return Math.max(4, Math.ceil(3 * 450 * Math.max(1, canvasHeight) / Math.max(100, heightMetres)))
}

/** Base REPLACE traversal preloads invisible children of renderable ground/road parents. */
export function traversalOptions(kind: string): { skipLevelOfDetail: boolean; loadSiblings: boolean } {
  return { skipLevelOfDetail: kind === 'ground_buildings' || kind === 'roads', loadSiblings: false }
}

export function tilesToRequest<T extends { kind: string }>(tiles: T[], groundFirst: boolean, overlayAllowed: boolean, cityAtScale = true): T[] {
  if (!groundFirst) return tiles
  return tiles.filter((tile) =>
    (overlayAllowed || (tile.kind !== 'city_volumes' && tile.kind !== 'facade'))
    && (cityAtScale || tile.kind !== 'city_volumes'),
  )
}

/** Root-JSON loads may outlive a Viewer; an old result must never join a new scene. */
export class TileLoadLedger<T> {
  generation = 0
  readonly pending = new Map<string, Promise<T | null>>()

  reset(): void {
    this.generation++
    this.pending.clear()
  }

  ensure(
    url: string,
    create: () => Promise<T>,
    accept: (value: T) => T | null,
    discard: (value: T) => void,
    onError: (error: unknown) => void,
  ): Promise<T | null> {
    const existing = this.pending.get(url)
    if (existing) return existing
    const generation = this.generation
    const task = create().then((value) => {
      if (generation !== this.generation) { discard(value); return null }
      return accept(value)
    }).catch((error) => { if (generation === this.generation) onError(error); return null })
      .finally(() => { if (this.pending.get(url) === task) this.pending.delete(url) })
    this.pending.set(url, task)
    return task
  }
}

export function reconcileTiles<T extends { show: boolean }>(
  tiles: Map<string, T>,
  wanted: Set<string>,
  retainDormant: (tile: T) => boolean,
  remove: (tile: T) => void,
  maxDormant = 3,
): number {
  for (const [url, tile] of [...tiles]) {
    tile.show = wanted.has(url)
    if (tile.show) {
      // An active tileset is recent. Iterate over a snapshot so this reorder is finite.
      tiles.delete(url)
      tiles.set(url, tile)
    } else if (!retainDormant(tile)) {
      remove(tile)
      tiles.delete(url)
    }
  }
  const dormant = [...tiles].filter(([url]) => !wanted.has(url))
  const excess = Math.max(0, dormant.length - maxDormant)
  for (const [url, tile] of dormant.slice(0, excess)) {
    remove(tile)
    tiles.delete(url)
  }
  return dormant.length - excess
}
