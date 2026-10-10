import type { DeveloperModel } from './api'

/** A selected model is an isolated hash-addressed asset, never a URL from source data. */
export function eligibleSelectedModel(model: DeveloperModel | null): model is DeveloperModel & { asset_url: string; asset_sha256: string; model_transform: number[] } {
  if (!model || model.status !== 'ready' || model.depiction !== 'schematic_massing'
    || !model.asset_url || !model.asset_sha256 || !Array.isArray(model.model_transform)
    || !/^[a-f0-9]{64}$/.test(model.asset_sha256)
    || model.asset_url !== `/v1/developer-model-assets/${model.asset_sha256}`
    || model.model_transform.length !== 16 || !model.model_transform.every(Number.isFinite)) return false
  const transform = model.model_transform
  return Math.abs(transform[3]) < 1e-6 && Math.abs(transform[7]) < 1e-6
    && Math.abs(transform[11]) < 1e-6 && Math.abs(transform[15] - 1) < 1e-6
    && model.height_m !== null && Number.isFinite(model.height_m) && model.height_m > 0
}
