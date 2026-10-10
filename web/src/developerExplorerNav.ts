export type ExplorerStage =
  | { kind: 'building' }
  | { kind: 'floor'; floorId: string | null; sectionId: string | null }
  | { kind: 'unit'; unitId: string; floorId: string | null; sectionId: string | null; parent: 'floor' | 'building' }

export type ExplorerState = { stage: ExplorerStage; menuOpen: boolean }

export const initialExplorerState = (): ExplorerState => ({ stage: { kind: 'building' }, menuOpen: false })

export function openExplorerFloor(state: ExplorerState, floorId: string | null, sectionId: string | null): ExplorerState {
  return { ...state, stage: { kind: 'floor', floorId, sectionId }, menuOpen: false }
}

export function openExplorerUnit(state: ExplorerState, unitId: string, floorId: string | null, sectionId: string | null): ExplorerState {
  return { ...state, stage: { kind: 'unit', unitId, floorId, sectionId,
    parent: state.stage.kind === 'floor' ? 'floor' : 'building' }, menuOpen: false }
}

export function explorerBack(state: ExplorerState): { state: ExplorerState; close: boolean } {
  if (state.menuOpen) return { state: { ...state, menuOpen: false }, close: false }
  if (state.stage.kind === 'unit') return {
    state: { stage: state.stage.parent === 'floor'
      ? { kind: 'floor', floorId: state.stage.floorId, sectionId: state.stage.sectionId }
      : { kind: 'building' }, menuOpen: false },
    close: false,
  }
  if (state.stage.kind === 'floor') return { state: initialExplorerState(), close: false }
  return { state, close: true }
}
