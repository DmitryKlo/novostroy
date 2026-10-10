export type MapQuality = 'flat' | 'coarse' | 'balanced' | 'detail'

const STAGES: MapQuality[] = ['flat', 'coarse', 'balanced', 'detail']
const rank = (stage: MapQuality) => STAGES.indexOf(stage)

export function citySseForQuality(stage: MapQuality): number {
  return stage === 'coarse' ? 128 : stage === 'balanced' ? 64 : 24
}

export function qualityAllows(kind: string, stage: MapQuality): boolean {
  if (kind === 'city_volumes') return stage !== 'flat'
  if (kind === 'facade') return stage === 'detail'
  return true
}

export type ProbeReadiness = {
  visible: boolean
  documentVisible: boolean
  baseReady: boolean
  cityAtScale: boolean
  locked: boolean
  moving: boolean
  contentReady: boolean
}

export function probeStartAllowed(state: ProbeReadiness): boolean {
  return state.visible && state.documentVisible && state.baseReady && state.cityAtScale
    && !state.locked && !state.moving && state.contentReady
}

export type FrameWindow = { samples: readonly number[]; activeProbe: boolean; visible: boolean; moving: boolean; contentReady: boolean }
export type FrameWindowResult = { changed: boolean; stage: MapQuality; p75: number | null; reason: string }

export type ViewPose = { x: number; y: number; z: number; height: number; heading: number; pitch: number; width: number; canvasHeight: number }

export function meaningfulViewChange(before: ViewPose, after: ViewPose): boolean {
  return Math.abs(before.width - after.width) >= 24 || Math.abs(before.canvasHeight - after.canvasHeight) >= 24
    || Math.hypot(after.x - before.x, after.y - before.y, after.z - before.z) >= 100
    || Math.abs(after.height - before.height) >= 100
    || Math.abs(after.heading - before.heading) >= 0.03
    || Math.abs(after.pitch - before.pitch) >= 0.03
}

/** Frame intervals are a responsiveness proxy measured only during active render probes. */
export class AdaptiveQuality {
  stage: MapQuality = 'flat'
  ceiling: MapQuality = 'detail'
  locked = false
  healthyWindows = 0
  epoch = 0
  reason = 'awaiting_flat_probe'
  lastP75: number | null = null
  lastSampleCount = 0
  windows = 0
  private severeFrameStreak = 0

  onMeaningfulChange(): void {
    this.epoch++
    this.locked = false
    this.ceiling = 'detail'
    this.healthyWindows = 0
    this.severeFrameStreak = 0
    this.reason = 'view_changed'
  }

  resetToFlat(reason: string): void {
    this.onMeaningfulChange()
    this.stage = 'flat'
    this.reason = reason
  }

  pauseForStress(reason: string): void {
    this.stage = 'flat'
    this.ceiling = 'flat'
    this.locked = true
    this.healthyWindows = 0
    this.reason = reason
  }

  observeFrame(intervalMs: number, visible: boolean): boolean {
    if (!visible || !Number.isFinite(intervalMs) || intervalMs <= 0) { this.severeFrameStreak = 0; return false }
    this.severeFrameStreak = intervalMs > 100 ? this.severeFrameStreak + 1 : 0
    if (this.severeFrameStreak < 2) return false
    const changed = this.stage !== 'flat'
    this.stage = 'flat'
    this.ceiling = 'flat'
    this.locked = true
    this.healthyWindows = 0
    this.reason = 'two_severe_frames'
    return changed
  }

  observeWindow(input: FrameWindow): FrameWindowResult {
    const unchanged = (): FrameWindowResult => ({ changed: false, stage: this.stage, p75: this.lastP75, reason: this.reason })
    if (!input.visible) { this.reason = 'hidden'; this.healthyWindows = 0; return unchanged() }
    if (!input.activeProbe || input.samples.length < 10) { this.reason = 'insufficient_active_frames'; return unchanged() }
    const values = [...input.samples].filter((value) => Number.isFinite(value) && value > 0).sort((a, b) => a - b)
    if (values.length < 10) { this.reason = 'insufficient_active_frames'; return unchanged() }
    const p75 = values[Math.ceil(values.length * 0.75) - 1]
    this.lastP75 = p75
    this.lastSampleCount = values.length
    this.windows++
    const slowFrames = values.filter((value) => value > 60).length
    const severe = p75 > 80 || values.filter((value) => value > 100).length >= 2 || values.at(-1)! > 180
    const stressed = severe || p75 > 48 || slowFrames >= 2
    if (stressed) {
      this.healthyWindows = 0
      const previous = this.stage
      if (severe || rank(this.stage) <= 1) {
        this.stage = 'flat'
        this.ceiling = 'flat'
        this.locked = true
        this.reason = severe ? 'severe_frame_window' : 'coarse_stress'
      } else {
        this.stage = 'coarse'
        this.ceiling = 'coarse'
        this.reason = 'detail_stress'
      }
      return { changed: this.stage !== previous, stage: this.stage, p75, reason: this.reason }
    }
    if (p75 <= 28 && !input.moving && input.contentReady && !this.locked && rank(this.stage) < rank(this.ceiling)) {
      this.healthyWindows++
      this.reason = 'measuring_headroom'
      if (this.healthyWindows >= 2) {
        this.stage = STAGES[rank(this.stage) + 1]
        this.healthyWindows = 0
        this.reason = `promoted_${this.stage}`
        return { changed: true, stage: this.stage, p75, reason: this.reason }
      }
    } else {
      this.healthyWindows = 0
      this.reason = input.moving ? 'moving_no_upgrade' : !input.contentReady ? 'waiting_visible_content' : this.locked ? 'paused_after_stress' : 'neutral_frame_budget'
    }
    return { changed: false, stage: this.stage, p75, reason: this.reason }
  }

  diagnostic() {
    return { stage: this.stage, ceiling: this.ceiling, locked: this.locked, healthyWindows: this.healthyWindows, epoch: this.epoch,
      reason: this.reason, lastP75Ms: this.lastP75, lastSampleCount: this.lastSampleCount, windows: this.windows }
  }
}

/** Explicitly requested renders only; ordinary idle postRender gaps are never samples. */
export class BoundedFrameProbe {
  active = false
  reason = 'idle'
  requests = 0
  windows = 0
  private lastRenderAt: number | null = null
  private samples: number[] = []

  start(reason: string): void {
    this.active = true
    this.reason = reason
    this.requests = 0
    this.windows = 0
    this.lastRenderAt = null
    this.samples = []
  }

  stop(reason: string): void {
    this.active = false
    this.reason = reason
    this.lastRenderAt = null
    this.samples = []
  }

  request(): boolean {
    if (!this.active) return false
    if (this.requests >= 80) { this.stop('request_cap'); return false }
    this.requests++
    return true
  }

  rendered(now: number, visible: boolean): { interval: number | null; window: number[] | null } {
    if (!this.active) return { interval: null, window: null }
    if (!visible) { this.stop('hidden'); return { interval: null, window: null } }
    if (this.requests === 0) return { interval: null, window: null }
    const interval = this.lastRenderAt === null ? null : now - this.lastRenderAt
    this.lastRenderAt = now
    if (interval === null || !Number.isFinite(interval) || interval <= 0) return { interval: null, window: null }
    this.samples.push(interval)
    if (this.samples.length < 10) return { interval, window: null }
    const window = this.samples
    this.samples = []
    this.lastRenderAt = null
    this.windows++
    if (this.windows >= 2) this.stop('window_cap')
    return { interval, window }
  }

  diagnostic() { return { active: this.active, reason: this.reason, requests: this.requests, windows: this.windows, partialSamples: this.samples.length } }
}

/** A capped probe is not silently retried on the same view and quality stage. */
export class ProbeAttemptLedger {
  private epoch = -1
  private readonly attempted = new Set<string>()

  claim(epoch: number, stage: MapQuality, reason: string): boolean {
    if (epoch !== this.epoch) { this.epoch = epoch; this.attempted.clear() }
    const bucket = reason === 'camera_motion' ? 'motion' : reason === 'facade_visible' ? 'facade' : 'stage'
    const key = `${stage}:${bucket}`
    if (this.attempted.has(key)) return false
    this.attempted.add(key)
    return true
  }

  releaseInterrupted(epoch: number, stage: MapQuality, reason: string): void {
    if (epoch !== this.epoch) return
    const bucket = reason === 'camera_motion' ? 'motion' : reason === 'facade_visible' ? 'facade' : 'stage'
    this.attempted.delete(`${stage}:${bucket}`)
  }
}
