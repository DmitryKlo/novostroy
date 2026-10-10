export type GroundGateState = 'waiting' | 'hidden' | 'loading_flat' | 'no_visible_ground' | 'ground_failed' | 'first_ground_frame' | 'overlay_ready'

export type GroundFrame = {
  visible: boolean
  cameraKey: string
  groundVisible: boolean
  groundLoaded: boolean
  flatSettled: boolean
  groundFailed: boolean
}

export type GroundGateResult = { startOverlay: boolean; requestFrame: boolean }

/** Ancillary roots must be known, but their detail refinement cannot hold the 3D gate. */
export function flatRootsResolved(urls: readonly string[], isResolved: (url: string) => boolean): boolean {
  return urls.every(isResolved)
}

/** The global 47 m raster is a far-view fallback; 3D waits for 2 m region outlines or exact cells. */
export function isPreciseGroundTile(geometricError: number): boolean {
  return Number.isFinite(geometricError) && geometricError <= 2
}

/** Position alone is insufficient when an orientation or viewport changes before the barrier. */
export function groundViewSignature(position: readonly number[], orientation: readonly number[], viewport: readonly number[], projection: readonly number[]): string {
  return position.map((value) => value.toFixed(2)).join(',')
    + ':' + orientation.map((value) => value.toFixed(6)).join(',')
    + ':' + viewport.join(',')
    + ':' + projection.map((value) => value.toFixed(6)).join(',')
}

/** A ground frame must complete twice at a stable camera before 3D may start. */
export class GroundFirstGate {
  readonly key: string
  readonly groundUrl: string
  readonly flatUrls: string[]
  state: GroundGateState = 'waiting'
  epoch = 0
  readyFrames = 0
  overlayStarted = false
  private cameraKey: string | null = null

  constructor(key: string, groundUrl: string, flatUrls: string[]) {
    this.key = key
    this.groundUrl = groundUrl
    this.flatUrls = flatUrls
  }

  cameraMoved(): void {
    if (this.overlayStarted) return
    this.epoch++
    this.readyFrames = 0
    this.cameraKey = null
    this.state = 'waiting'
  }

  frame(input: GroundFrame): GroundGateResult {
    if (this.overlayStarted) return { startOverlay: false, requestFrame: false }
    if (!input.visible) {
      this.readyFrames = 0
      this.state = 'hidden'
      return { startOverlay: false, requestFrame: false }
    }
    if (this.cameraKey !== input.cameraKey) {
      this.epoch++
      this.cameraKey = input.cameraKey
      this.readyFrames = 0
    }
    if (input.groundFailed) {
      this.readyFrames = 0
      this.state = 'ground_failed'
      return { startOverlay: false, requestFrame: false }
    }
    if (!input.groundLoaded || !input.flatSettled) {
      this.readyFrames = 0
      this.state = 'loading_flat'
      return { startOverlay: false, requestFrame: false }
    }
    if (!input.groundVisible) {
      this.readyFrames = 0
      this.state = 'no_visible_ground'
      return { startOverlay: false, requestFrame: false }
    }
    this.readyFrames++
    if (this.readyFrames === 1) {
      this.state = 'first_ground_frame'
      return { startOverlay: false, requestFrame: true }
    }
    this.overlayStarted = true
    this.state = 'overlay_ready'
    return { startOverlay: true, requestFrame: false }
  }
}
