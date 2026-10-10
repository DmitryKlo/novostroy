import { useEffect, useRef, useState } from 'react'
import * as Cesium from 'cesium'
import { LocateFixed, Minus, Plus } from 'lucide-react'
import type { Complex, DeveloperModel, MapData, MapMarker, NearbyCategory, NearbyPoi, Tileset } from './api'
import { eligibleSelectedModel } from './selectedDeveloperModel'
import { cityVolumesAtHeight, reconcileTiles, roadDetailAllowed, roadDetailAtHeight, roadScreenSpaceError, stagedCityVersion, TileLoadLedger, tilesToRequest, traversalOptions } from './tileCache'
import { flatRootsResolved, GroundFirstGate, groundViewSignature, isPreciseGroundTile, type GroundGateState } from './groundGate'
import { MapTileHealth, layerLoadText } from './mapTileHealth'
import { AdaptiveQuality, BoundedFrameProbe, citySseForQuality, meaningfulViewChange, probeStartAllowed, ProbeAttemptLedger, qualityAllows, type MapQuality, type ViewPose } from './adaptiveQuality'
import { NEARBY_COLORS, validNetworkRouteGeometry, validOriginAccessGeometry } from './nearby'
import { resolveMapPick, selectionFocusKey, shouldFlyToSelection, shouldStartInitialCameraFlight, type PickCandidate } from './mapInteraction'
import { collapseCoveredGroupMembers, markerEntityChanges, markerIdsOnLoadedGround, type GroundPosition, type VisibleGround } from './markerVisibility'

type Props = {
  data: MapData | null
  selectedId: string | null
  selectedModel?: DeveloperModel | null
  onSelectedModelVisible?: (visible: boolean) => void
  focusOnSelection?: boolean
  selectionRequestId?: number
  visible?: boolean
  markerLabel?: string
  nearbyItems?: { layer: NearbyCategory; item: NearbyPoi }[]
  selectedPoiId?: string | null
  onSelectNearby?: (id: string) => void
  onViewport?: (bbox: [number, number, number, number] | null) => void
  onMarkerScale?: (heightMetres: number) => void
  onSelect: (id: string) => void
  onStatus: (status: string) => void
}

const MOSCOW: [number, number] = [37.6176, 55.7558]
const corpusCount = (count: number) => `${count.toLocaleString('ru-RU')} ${new Intl.PluralRules('ru-RU').select(count) === 'one' ? 'корпус' : new Intl.PluralRules('ru-RU').select(count) === 'few' ? 'корпуса' : 'корпусов'} с точкой`
const NO_NEARBY_ITEMS: { layer: NearbyCategory; item: NearbyPoi }[] = []
const MIB = 1024 * 1024
const CACHE_OVERFLOW_BYTES = 2 * MIB
const tileWeight = (tile: Tileset) => tile.kind === 'ground_buildings' ? 12 : tile.kind === 'city_volumes' ? 8 : tile.kind === 'roads' ? 2 : tile.kind === 'facade' ? 0.25 : 1
const coarseSse = (viewer: Cesium.Viewer) => Math.min(1500, Math.max(24, 1500 * 4200 / Math.max(120, viewer.camera.positionCartographic.height)))
const cameraKey = (viewer: Cesium.Viewer) => {
  const point = viewer.camera.positionWC
  const canvas = viewer.scene.canvas
  return groundViewSignature(
    [point.x, point.y, point.z],
    [viewer.camera.heading, viewer.camera.pitch, viewer.camera.roll],
    [canvas.clientWidth, canvas.clientHeight, canvas.width, canvas.height, viewer.resolutionScale],
    Cesium.Matrix4.toArray(viewer.camera.frustum.projectionMatrix),
  )
}
const viewPose = (viewer: Cesium.Viewer): ViewPose => ({
  x: viewer.camera.positionWC.x, y: viewer.camera.positionWC.y, z: viewer.camera.positionWC.z,
  height: viewer.camera.positionCartographic.height, heading: viewer.camera.heading, pitch: viewer.camera.pitch,
  width: viewer.scene.canvas.clientWidth, canvasHeight: viewer.scene.canvas.clientHeight,
})
type PreparedMarker = { marker: MapMarker; world: Cesium.Cartesian3; longitude: number; latitude: number; groupMembers?: GroundPosition[] }
let savedCamera: { destination: Cesium.Cartesian3; heading: number; pitch: number; roll: number } | null = null

export default function MapView({ data, selectedId, selectedModel = null, onSelectedModelVisible, focusOnSelection = true, selectionRequestId = 0, visible = true, markerLabel = 'ЖК · нажмите, чтобы посмотреть квартиры', nearbyItems = NO_NEARBY_ITEMS, selectedPoiId = null, onSelectNearby, onViewport, onMarkerScale, onSelect, onStatus }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const viewerRef = useRef<Cesium.Viewer | null>(null)
  const nearbySourceRef = useRef<Cesium.CustomDataSource | null>(null)
  const tilesRef = useRef(new Map<string, Cesium.Cesium3DTileset>())
  const loadLedgerRef = useRef(new TileLoadLedger<Cesium.Cesium3DTileset>())
  const wantedRef = useRef(new Set<string>())
  const rootFailuresRef = useRef(new Set<string>())
  const tileHealthRef = useRef(new MapTileHealth())
  const flatContentFailuresRef = useRef(new Set<string>())
  const groundTileFailedRef = useRef(false)
  const groundVisibleRef = useRef(false)
  const groundCoverageRef = useRef<VisibleGround[]>([])
  const lastGroundCoverageCountRef = useRef(0)
  const cameraEpochRef = useRef(0)
  const preparedMarkersRef = useRef<PreparedMarker[]>([])
  const activeMarkerIdsRef = useRef(new Set<string>())
  const groundEventsRef = useRef({ visible: 0, allLoaded: 0, failed: 0, frames: 0 })
  const volumeVisibleRef = useRef({ any: false, detail: false })
  const volumeEventsRef = useRef({ visible: 0, detailVisible: 0, allLoaded: 0, failed: 0, renderedFrames: 0, lastFrameStage: 'none' as 'none' | 'volume' | 'detail', lastFrameCameraKey: '' })
  const groundGateRef = useRef<GroundFirstGate | null>(null)
  const lastFlatKeyRef = useRef<string | null>(null)
  const cityRangeKeyRef = useRef<string | null>(null)
  const cityAtScaleRef = useRef(true)
  const roadDetailAtScaleRef = useRef(false)
  const qualityRef = useRef(new AdaptiveQuality())
  const probeRef = useRef(new BoundedFrameProbe())
  const probeRafRef = useRef<number | null>(null)
  const probeEpochRef = useRef(-1)
  const attemptedProbeRef = useRef(new ProbeAttemptLedger())
  const activeAttemptRef = useRef<{ epoch: number; stage: MapQuality; reason: string } | null>(null)
  const stageProbeStartedRef = useRef(false)
  const stageContentReadyRef = useRef(false)
  const facadeProbeStartedRef = useRef(false)
  const baseReadyRef = useRef(false)
  const lastLegacyFlatKeyRef = useRef<string | null>(null)
  const lastMeaningfulPoseRef = useRef<ViewPose | null>(null)
  const loadRetryUsedRef = useRef(false)
  const visibleRef = useRef(visible)
  const cityStageRef = useRef<{ tile: Cesium.Cesium3DTileset; version: 'v6' | 'v7'; silhouetteReady: boolean; remove: () => void } | null>(null)
  const movingRef = useRef(false)
  const facadeIdsRef = useRef(new WeakMap<object, string>())
  const framedRef = useRef(false)
  const skipInitialSelectionRef = useRef(false)
  const lastSelectionFocusKeyRef = useRef<string | null>(null)
  const highlightedMarkerRef = useRef<string | null>(null)
  const onSelectRef = useRef(onSelect)
  const selectedIdRef = useRef(selectedId)
  const selectedModelVisibleRef = useRef(false)
  const selectedModelTexturesReadyRef = useRef(false)
  const selectedModelPrimitiveRef = useRef<Cesium.Model | null>(null)
  const onSelectNearbyRef = useRef(onSelectNearby)
  const onViewportRef = useRef(onViewport)
  const onMarkerScaleRef = useRef(onMarkerScale)
  const onSelectedModelVisibleRef = useRef(onSelectedModelVisible)
  const dataRef = useRef(data)
  const [ready, setReady] = useState(false)
  const [sceneError, setSceneError] = useState<string | null>(null)
  const [, setLoadedTiles] = useState(0)
  const [overlayAllowedKey, setOverlayAllowedKey] = useState<string | null>(null)
  const [groundPhase, setGroundPhase] = useState<GroundGateState | null>(null)
  const [renderedVolume, setRenderedVolume] = useState<'none' | 'volume' | 'detail'>('none')
  const [cityAtScale, setCityAtScale] = useState(true)
  const [qualityStage, setQualityStage] = useState<MapQuality>('flat')
  const [, refreshQuality] = useState(0)
  const [, refreshTileHealth] = useState(0)
  onSelectRef.current = onSelect
  selectedIdRef.current = selectedId
  onSelectNearbyRef.current = onSelectNearby
  onViewportRef.current = onViewport
  onMarkerScaleRef.current = onMarkerScale
  onSelectedModelVisibleRef.current = onSelectedModelVisible
  dataRef.current = data
  visibleRef.current = visible
  if (data) wantedRef.current = new Set(data.tilesets.map((tile) => tile.url))
  const flatTiles = data?.tilesets.filter((tile) => tile.kind === 'ground_buildings' || tile.kind === 'roads' || tile.kind === 'parks' || tile.kind === 'water') ?? []
  const groundDescriptor = flatTiles.find((tile) => tile.kind === 'ground_buildings')
  if (data) lastFlatKeyRef.current = groundDescriptor ? flatTiles.map((tile) => tile.url).sort().join('|') : null
  const flatKey = lastFlatKeyRef.current
  const activeFailures = tileHealthRef.current.active(data?.tilesets.map((tile) => tile.url) ?? [])
  const deferredCity = flatKey && !cityAtScale ? data?.tilesets.filter((tile) => tile.kind === 'city_volumes') ?? [] : []
  const reportTiles = data?.tilesets.filter((tile) => !deferredCity.includes(tile)
    && qualityAllows(tile.kind, qualityStage)) ?? []
  const connectedAtScale = reportTiles.filter((tile) => tilesRef.current.has(tile.url)).length
  const totalAtScale = reportTiles.length
  const markTileFailure = (tile: Tileset, phase: 'root' | 'content', error: unknown) => {
    const count = tileHealthRef.current.mark(tile, phase, error)
    if (count === 1) refreshTileHealth((version) => version + 1)
    if (count <= 3 || (count & (count - 1)) === 0) console.error(`Map ${tile.kind} ${phase} load error (${count})`, error)
    return count
  }
  const stopProbe = (reason: string) => {
    if ((reason === 'map_hidden' || reason === 'document_hidden' || reason === 'hidden') && probeRef.current.active && activeAttemptRef.current) {
      const attempt = activeAttemptRef.current
      attemptedProbeRef.current.releaseInterrupted(attempt.epoch, attempt.stage, attempt.reason)
    }
    if (probeRafRef.current !== null) cancelAnimationFrame(probeRafRef.current)
    probeRafRef.current = null
    probeRef.current.stop(reason)
    activeAttemptRef.current = null
  }
  const syncRoadLod = (viewer: Cesium.Viewer) => {
    const height = viewer.camera.positionCartographic.height
    roadDetailAtScaleRef.current = roadDetailAtHeight(height, roadDetailAtScaleRef.current)
    const detailAllowed = roadDetailAllowed(roadDetailAtScaleRef.current, movingRef.current, qualityRef.current.stage)
    const sse = roadScreenSpaceError(height, viewer.scene.canvas.height, detailAllowed)
    for (const tile of dataRef.current?.tilesets ?? []) {
      if (tile.kind !== 'roads') continue
      const loaded = tilesRef.current.get(tile.url)
      if (loaded && loaded.maximumScreenSpaceError !== sse) loaded.maximumScreenSpaceError = sse
    }
    return sse
  }
  const reportViewport = (viewer: Cesium.Viewer) => {
    onMarkerScaleRef.current?.(viewer.camera.positionCartographic.height)
    const rectangle = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid)
    if (!rectangle) { onViewportRef.current?.(null); return }
    const bbox: [number, number, number, number] = [rectangle.west, rectangle.south, rectangle.east, rectangle.north].map(Cesium.Math.toDegrees) as [number, number, number, number]
    onViewportRef.current?.(bbox.every(Number.isFinite) && bbox[0] < bbox[2] && bbox[1] < bbox[3] ? bbox : null)
  }
  const setSelectedModelVisible = (viewer: Cesium.Viewer, shown: boolean) => {
    if (viewer.isDestroyed()) { selectedModelVisibleRef.current = false; return }
    if (selectedModelVisibleRef.current === shown) return
    selectedModelVisibleRef.current = shown
    onSelectedModelVisibleRef.current?.(shown)
    if (visibleRef.current) viewer.scene.requestRender()
  }
  const applyQuality = (viewer: Cesium.Viewer) => {
    const stage = qualityRef.current.stage
    setQualityStage(stage)
    stageProbeStartedRef.current = false
    stageContentReadyRef.current = stage === 'flat'
    facadeProbeStartedRef.current = false
    const groundFirst = Boolean(lastFlatKeyRef.current)
    const overlayReady = !groundFirst || groundGateRef.current?.overlayStarted === true
    for (const tile of dataRef.current?.tilesets ?? []) {
      if (tile.kind !== 'city_volumes' && tile.kind !== 'facade') continue
      const existing = tilesRef.current.get(tile.url)
      if (!existing) continue
      existing.show = visibleRef.current && overlayReady && qualityAllows(tile.kind, stage)
        && (tile.kind !== 'city_volumes' || cityAtScaleRef.current)
      if (tile.kind === 'city_volumes' && (cityStageRef.current?.tile !== existing || cityStageRef.current.silhouetteReady))
        existing.maximumScreenSpaceError = citySseForQuality(stage)
    }
    syncRoadLod(viewer)
    if (visibleRef.current) viewer.scene.requestRender()
  }
  const startProbe = (viewer: Cesium.Viewer, reason: string, restart = false): boolean => {
    const baseReady = groundGateRef.current ? groundGateRef.current.overlayStarted : baseReadyRef.current
    if (viewerRef.current !== viewer || !probeStartAllowed({
      visible: visibleRef.current, documentVisible: document.visibilityState !== 'hidden', baseReady,
      cityAtScale: cityAtScaleRef.current, locked: qualityRef.current.locked,
      moving: movingRef.current, contentReady: stageContentReadyRef.current,
    })) return false
    if (probeRef.current.active && !restart) return false
    const epoch = qualityRef.current.epoch
    if (!attemptedProbeRef.current.claim(epoch, qualityRef.current.stage, reason)) return false
    stopProbe('restarted')
    probeRef.current.start(reason)
    probeEpochRef.current = epoch
    activeAttemptRef.current = { epoch, stage: qualityRef.current.stage, reason }
    const pump = () => {
      if (!visibleRef.current || document.visibilityState === 'hidden' || viewerRef.current !== viewer) { stopProbe('hidden'); return }
      if (!probeRef.current.request()) {
        if (probeRef.current.reason === 'request_cap' && qualityRef.current.stage !== 'flat') {
          qualityRef.current.pauseForStress('probe_no_rendered_frames')
          applyQuality(viewer)
        }
        refreshQuality((version) => version + 1)
        return
      }
      viewer.scene.requestRender()
      probeRafRef.current = requestAnimationFrame(pump)
    }
    probeRafRef.current = requestAnimationFrame(pump)
    return true
  }
  const recordProbeFrame = (viewer: Cesium.Viewer) => {
    if (probeRef.current.active && probeEpochRef.current !== qualityRef.current.epoch) { stopProbe('stale_epoch'); return }
    const visibleNow = visibleRef.current && document.visibilityState !== 'hidden' && viewerRef.current === viewer
    if (!visibleNow && probeRef.current.active) { stopProbe('hidden'); return }
    const sample = probeRef.current.rendered(performance.now(), visibleNow)
    if (sample.interval === null) return
    const policy = qualityRef.current
    const wasLocked = policy.locked
    if (policy.observeFrame(sample.interval, visibleNow) || (!wasLocked && policy.locked)) {
      stopProbe('severe_stall')
      applyQuality(viewer)
      refreshQuality((version) => version + 1)
      return
    }
    if (!sample.window) return
    const result = policy.observeWindow({ samples: sample.window, activeProbe: true, visible: visibleNow,
      moving: movingRef.current, contentReady: stageContentReadyRef.current })
    refreshQuality((version) => version + 1)
    if (result.changed) { stopProbe('quality_changed'); applyQuality(viewer) }
    else if (policy.locked) stopProbe('stress_lock')
  }
  const retryAfterLoadSettles = (viewer: Cesium.Viewer) => {
    const gate = groundGateRef.current
    if (!(gate?.overlayStarted || baseReadyRef.current) || !qualityRef.current.locked || loadRetryUsedRef.current || !cityAtScaleRef.current || movingRef.current) return
    const baseUrls = gate?.flatUrls ?? dataRef.current?.tilesets.filter((tile) => tile.kind !== 'city_volumes' && tile.kind !== 'facade').map((tile) => tile.url) ?? []
    if (!flatRootsResolved(baseUrls, (url) => rootFailuresRef.current.has(url) || tilesRef.current.has(url))) return
    loadRetryUsedRef.current = true
    qualityRef.current.onMeaningfulChange()
    refreshQuality((version) => version + 1)
    stageContentReadyRef.current = qualityRef.current.stage === 'flat'
    stageProbeStartedRef.current = startProbe(viewer, 'flat_load_settled')
  }
  const paintSelection = (viewer: Cesium.Viewer, id: string | null) => {
    const map = dataRef.current
    const baseSize = map?.markers ? 9 : 11
    const previous = highlightedMarkerRef.current ? viewer.entities.getById(`complex:${highlightedMarkerRef.current}`) : null
    if (previous?.point) previous.point.pixelSize = new Cesium.ConstantProperty(baseSize)
    const current = id ? viewer.entities.getById(`complex:${id}`) : null
    if (current?.point) current.point.pixelSize = new Cesium.ConstantProperty(16)
    highlightedMarkerRef.current = id
    viewer.scene.requestRender()
  }
  const syncVisibleMarkers = (viewer: Cesium.Viewer, ground: readonly VisibleGround[]) => {
    if (!dataRef.current?.markers) return
    const camera = viewer.camera
    const rectangle = camera.computeViewRectangle(viewer.scene.globe.ellipsoid)
    const candidates = ground.length && visibleRef.current ? preparedMarkersRef.current.flatMap(({ marker, world, longitude, latitude, groupMembers }) => {
      if (rectangle) {
        const margin = 0.0002
        const insideLongitude = rectangle.west <= rectangle.east
          ? longitude >= rectangle.west - margin && longitude <= rectangle.east + margin
          : longitude >= rectangle.west - margin || longitude <= rectangle.east + margin
        if (!insideLongitude || latitude < rectangle.south - margin || latitude > rectangle.north + margin) return []
      }
      const front = (world.x - camera.positionWC.x) * camera.directionWC.x
        + (world.y - camera.positionWC.y) * camera.directionWC.y
        + (world.z - camera.positionWC.z) * camera.directionWC.z > 0
      if (!front) return []
      const point = Cesium.SceneTransforms.worldToWindowCoordinates(viewer.scene, world)
      return [{ id: marker.id, x: point?.x ?? Number.NaN, y: point?.y ?? Number.NaN, front,
        longitude, latitude, world: [world.x, world.y, world.z] as const, groupMembers }]
    }) : []
    const loaded = markerIdsOnLoadedGround(candidates, ground, {
      epoch: cameraEpochRef.current, width: viewer.scene.canvas.clientWidth,
      height: viewer.scene.canvas.clientHeight, visible: visibleRef.current,
      requirePrecise: viewer.camera.positionCartographic.height <= 8_000,
    })
    const desired = collapseCoveredGroupMembers(loaded, preparedMarkersRef.current.map(({ marker }) => marker))
    const changes = markerEntityChanges(activeMarkerIdsRef.current, desired)
    for (const id of changes.remove) {
      viewer.entities.removeById(`complex:${id}`)
      activeMarkerIdsRef.current.delete(id)
    }
    const toAdd = new Set(changes.add)
    for (const { marker, world } of preparedMarkersRef.current) {
      if (!toAdd.has(marker.id)) continue
      viewer.entities.add({ id: `complex:${marker.id}`, position: world,
        point: { pixelSize: marker.point_type === 'complex_group' ? 13 : 9,
          color: Cesium.Color.fromCssColorString(marker.point_type === 'complex_group' ? '#8c4f40' : marker.point_type === 'mapped_construction' ? '#a86c54' : '#c96747'),
          outlineColor: Cesium.Color.WHITE, outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY },
      })
      activeMarkerIdsRef.current.add(marker.id)
    }
    if (changes.remove.length || changes.add.length) paintSelection(viewer, selectedIdRef.current)
  }
  const syncCityRange = (viewer: Cesium.Viewer, groundKey: string | null) => {
    const previous = cityRangeKeyRef.current === groundKey ? cityAtScaleRef.current : false
    const next = groundKey ? cityVolumesAtHeight(viewer.camera.positionCartographic.height, previous) : true
    cityRangeKeyRef.current = groundKey
    if (next === cityAtScaleRef.current) return next
    cityAtScaleRef.current = next
    setCityAtScale(next)
    if (!next) setRenderedVolume('none')
    if (!next) stopProbe('far_view')
    else if (groundKey) { qualityRef.current.resetToFlat('range_return_probe'); applyQuality(viewer) }
    for (const tile of dataRef.current?.tilesets ?? []) {
      if (tile.kind !== 'city_volumes') continue
      const existing = tilesRef.current.get(tile.url)
      if (existing) existing.show = visibleRef.current && next && (!groundGateRef.current || groundGateRef.current.overlayStarted)
        && qualityAllows(tile.kind, qualityRef.current.stage)
    }
    if (visibleRef.current) viewer.scene.requestRender()
    return next
  }

  useEffect(() => {
    if (!containerRef.current) return
    let viewer: Cesium.Viewer
    let handler: Cesium.ScreenSpaceEventHandler | undefined
    let observer: ResizeObserver | undefined
    let removeVisibilityListener: (() => void) | undefined
    const frameTimes: number[] = []
    let removeFrameListener: (() => void) | undefined
    let removeGroundFrameListener: (() => void) | undefined
    try {
      viewer = new Cesium.Viewer(containerRef.current, {
        baseLayer: false,
        terrainProvider: new Cesium.EllipsoidTerrainProvider(),
        animation: false, timeline: false, geocoder: false, homeButton: false,
        sceneModePicker: false, baseLayerPicker: false, navigationHelpButton: false,
        fullscreenButton: false, infoBox: false, selectionIndicator: false,
        skyBox: false, skyAtmosphere: false, contextOptions: { webgl: { alpha: false, antialias: true } },
        useBrowserRecommendedResolution: false,
        requestRenderMode: true,
        maximumRenderTimeChange: Number.POSITIVE_INFINITY,
      })
      viewerRef.current = viewer
      const nearbySource = new Cesium.CustomDataSource('nearby-poi')
      nearbySourceRef.current = nearbySource
      void viewer.dataSources.add(nearbySource)
      wantedRef.current = new Set(dataRef.current?.tilesets.map((tile) => tile.url) ?? [])
      setOverlayAllowedKey(null)
      setGroundPhase(null)
      setRenderedVolume('none')
      stopProbe('new_viewer')
      qualityRef.current.resetToFlat('awaiting_ground_frame')
      setQualityStage('flat')
      viewer.resolutionScale = Math.min(1, 1.5 / window.devicePixelRatio)
      viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString('#deddd6')
      viewer.scene.backgroundColor = Cesium.Color.fromCssColorString('#deddd6')
      viewer.scene.globe.showGroundAtmosphere = false
      viewer.scene.fog.enabled = false
      viewer.scene.screenSpaceCameraController.minimumZoomDistance = 120
      viewer.scene.screenSpaceCameraController.maximumZoomDistance = 65000
      viewer.scene.screenSpaceCameraController.enableTilt = true
      if (savedCamera) {
        viewer.camera.setView({ destination: savedCamera.destination, orientation: { heading: savedCamera.heading, pitch: savedCamera.pitch, roll: savedCamera.roll } })
        framedRef.current = true
        skipInitialSelectionRef.current = Boolean(selectedId)
      } else {
        viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(...MOSCOW, 5200), orientation: { heading: 0, pitch: Cesium.Math.toRadians(-90), roll: 0 } })
      }
      syncCityRange(viewer, flatKey)
      lastMeaningfulPoseRef.current = viewPose(viewer)
      viewer.scene.renderError.addEventListener((_scene, error) => {
        console.error('Map render error', error)
        groundTileFailedRef.current = true
        setSceneError('Попробуйте обновить страницу.')
        onStatus('Карта временно недоступна')
      })
      removeGroundFrameListener = viewer.scene.postRender.addEventListener(() => {
        const visibleGround = groundCoverageRef.current
        groundCoverageRef.current = []
        lastGroundCoverageCountRef.current = visibleGround.length
        syncVisibleMarkers(viewer, visibleGround)
        const volumeFrame = volumeVisibleRef.current
        const volumeEvents = volumeEventsRef.current
        volumeEvents.lastFrameStage = volumeFrame.detail ? 'detail' : volumeFrame.any ? 'volume' : 'none'
        if (volumeFrame.any) volumeEvents.renderedFrames++
        volumeEvents.lastFrameCameraKey = cameraKey(viewer)
        setRenderedVolume(volumeEvents.lastFrameStage)
        volumeVisibleRef.current = { any: false, detail: false }
        recordProbeFrame(viewer)
        const gate = groundGateRef.current
        const hadVisibleGround = groundVisibleRef.current
        groundVisibleRef.current = false
        if (!gate) {
          if (!baseReadyRef.current && dataRef.current && visibleRef.current) {
            const baseUrls = dataRef.current.tilesets.filter((tile) => tile.kind !== 'city_volumes' && tile.kind !== 'facade').map((tile) => tile.url)
            if (flatRootsResolved(baseUrls, (url) => rootFailuresRef.current.has(url) || tilesRef.current.has(url))) {
              baseReadyRef.current = true
              stageContentReadyRef.current = true
              stageProbeStartedRef.current = startProbe(viewer, 'legacy_base_frame')
            }
          }
          return
        }
        if (gate.overlayStarted) return
        groundEventsRef.current.frames++
        const ground = tilesRef.current.get(gate.groundUrl)
        const flatSettled = flatRootsResolved(gate.flatUrls, (url) => rootFailuresRef.current.has(url) || tilesRef.current.has(url))
        const outcome = gate.frame({
          visible: visibleRef.current,
          cameraKey: cameraKey(viewer),
          groundVisible: hadVisibleGround,
          groundLoaded: Boolean(ground?.tilesLoaded),
          flatSettled,
          groundFailed: rootFailuresRef.current.has(gate.groundUrl) || groundTileFailedRef.current,
        })
        setGroundPhase(gate.state)
        if (outcome.requestFrame) viewer.scene.requestRender()
        if (outcome.startOverlay) {
          setOverlayAllowedKey(gate.key)
          if (cityAtScaleRef.current) { stageContentReadyRef.current = true; stageProbeStartedRef.current = startProbe(viewer, 'ground_frame_ready') }
        }
      })
      const diagnostics = import.meta.env.DEV || (window.location.hostname === '127.0.0.1' && new URLSearchParams(window.location.search).has('__map_test'))
      if (diagnostics) removeFrameListener = viewer.scene.postRender.addEventListener(() => { frameTimes.push(performance.now()); if (frameTimes.length > 600) frameTimes.shift() })
      handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas)
      handler.setInputAction((event: Cesium.ScreenSpaceEventHandler.PositionedEvent) => {
        const facadeIdFor = (object: object) => facadeIdsRef.current.get(object)
        const primary = viewer.scene.pick(event.position) as PickCandidate | undefined
        let target = resolveMapPick(primary ? [primary] : [], selectedIdRef.current, facadeIdFor)
        if (!target || target.kind === 'facade') {
          const expanded = viewer.scene.pick(event.position, 14, 14) as PickCandidate | undefined
          const widenedTarget = resolveMapPick(expanded ? [expanded] : [], selectedIdRef.current, facadeIdFor)
          if (widenedTarget?.kind === 'marker' || widenedTarget?.kind === 'nearby') target = widenedTarget
        }
        if (!target || target.kind === 'facade' || target.kind === 'nearby') {
          const stack = viewer.scene.drillPick(event.position, 8, 14, 14) as PickCandidate[]
          target = resolveMapPick(stack, selectedIdRef.current, facadeIdFor) ?? target
        }
        if (target?.kind === 'nearby') onSelectNearbyRef.current?.(target.id)
        else if (target) {
          viewer.camera.cancelFlight()
          framedRef.current = true
          const marker = dataRef.current?.markers?.find((item) => item.id === target.id)
          if (marker?.point_type === 'complex_group') {
            const sphere = Cesium.BoundingSphere.fromPoints(marker.group_member_locations.map(([lon, lat]) => Cesium.Cartesian3.fromDegrees(lon, lat, 36)))
            sphere.radius = Math.max(100, sphere.radius * 1.15)
            viewer.camera.flyToBoundingSphere(sphere, { offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-55), 0), duration: 0.8 })
          }
          paintSelection(viewer, target.id); onSelectRef.current(target.id)
        }
      }, Cesium.ScreenSpaceEventType.LEFT_CLICK)
      observer = new ResizeObserver(() => {
        if (!viewer.useDefaultRenderLoop) return
        viewer.resolutionScale = Math.min(1, 1.5 / window.devicePixelRatio)
        viewer.resize(); syncRoadLod(viewer); reportViewport(viewer); viewer.scene.requestRender()
        const before = lastMeaningfulPoseRef.current
        const after = viewPose(viewer)
        if (before && (Math.abs(before.width - after.width) >= 24 || Math.abs(before.canvasHeight - after.canvasHeight) >= 24)) {
          cameraEpochRef.current++
          groundCoverageRef.current = []
          stopProbe('viewport_changed')
          lastMeaningfulPoseRef.current = after
          loadRetryUsedRef.current = false
          qualityRef.current.onMeaningfulChange()
          refreshQuality((version) => version + 1)
          stageProbeStartedRef.current = false
          stageContentReadyRef.current = qualityRef.current.stage === 'flat'
          if (groundGateRef.current?.overlayStarted || baseReadyRef.current) stageProbeStartedRef.current = startProbe(viewer, 'viewport_changed', true)
        }
      })
      observer.observe(containerRef.current)
      const onDocumentVisibility = () => {
        if (document.visibilityState === 'hidden') stopProbe('document_hidden')
        else if (visibleRef.current && (groundGateRef.current?.overlayStarted || baseReadyRef.current)) startProbe(viewer, 'document_visible', true)
      }
      document.addEventListener('visibilitychange', onDocumentVisibility)
      removeVisibilityListener = () => document.removeEventListener('visibilitychange', onDocumentVisibility)
      if (diagnostics) {
        (window as unknown as { __novoMapTest?: unknown }).__novoMapTest = {
          project: (id: string, height = 36) => {
            const location = dataRef.current?.markers?.find((item) => item.id === id)?.location
              ?? dataRef.current?.complexes.find((item) => item.complex_id === id)?.location
            if (!location) return null
            const point = Cesium.SceneTransforms.worldToWindowCoordinates(viewer.scene, Cesium.Cartesian3.fromDegrees(...location, height))
            return point ? [point.x, point.y] : null
          },
          cameraHeight: () => viewer.camera.positionCartographic.height,
          cameraPose: () => ({ longitude: Cesium.Math.toDegrees(viewer.camera.positionCartographic.longitude), latitude: Cesium.Math.toDegrees(viewer.camera.positionCartographic.latitude), height: viewer.camera.positionCartographic.height, heading: Cesium.Math.toDegrees(viewer.camera.heading), pitch: Cesium.Math.toDegrees(viewer.camera.pitch) }),
          markerCount: () => activeMarkerIdsRef.current.size || viewer.entities.values.filter((entity) => Boolean(entity.point)).length,
          markerActive: (id: string) => activeMarkerIdsRef.current.has(id),
          markerVisibility: () => ({ cameraEpoch: cameraEpochRef.current,
            sourceMarkers: preparedMarkersRef.current.length, activeMarkers: activeMarkerIdsRef.current.size,
            labelsNear: false, selectedMarkerActive: selectedIdRef.current ? activeMarkerIdsRef.current.has(selectedIdRef.current) : false,
            groupMarkers: preparedMarkersRef.current.filter(({ marker }) => marker.point_type === 'complex_group').length,
            lastGroundCoverageCount: lastGroundCoverageCountRef.current }),
          viewerIdentity: () => viewer.scene.canvas,
          renderLoop: () => viewer.useDefaultRenderLoop,
          cityStage: () => cityStageRef.current ? { version: cityStageRef.current.version, silhouetteReady: cityStageRef.current.silhouetteReady, maximumScreenSpaceError: cityStageRef.current.tile.maximumScreenSpaceError } : null,
          cityRange: () => ({ heightMetres: viewer.camera.positionCartographic.height, hasGround: Boolean(lastFlatKeyRef.current), cityEnabled: cityAtScaleRef.current, enableAtMetres: 12_000, disableAtMetres: 16_000 }),
          roadLod: () => ({ heightMetres: viewer.camera.positionCartographic.height, nearScale: roadDetailAtScaleRef.current,
            maximumScreenSpaceError: dataRef.current?.tilesets.filter((tile) => tile.kind === 'roads').map((tile) => tilesRef.current.get(tile.url)?.maximumScreenSpaceError ?? null) ?? [],
            qualityStage: qualityRef.current.stage, moving: movingRef.current }),
          adaptiveQuality: () => ({ ...qualityRef.current.diagnostic(), probeEpoch: probeEpochRef.current, probe: probeRef.current.diagnostic(), metric: 'active postRender frame interval; not GPU time or guaranteed FPS' }),
          groundStage: () => groundGateRef.current ? { key: groundGateRef.current.key, state: groundGateRef.current.state, cameraEpoch: groundGateRef.current.epoch, completedGroundFrames: groundGateRef.current.readyFrames, overlayStarted: groundGateRef.current.overlayStarted, events: { ...groundEventsRef.current }, groundRootFailed: rootFailuresRef.current.has(groundGateRef.current.groundUrl), groundTileFailed: groundTileFailedRef.current, degradedFlatLayers: groundGateRef.current.flatUrls.filter((url) => flatContentFailuresRef.current.has(url) || rootFailuresRef.current.has(url)).length } : null,
          flatLayerStates: () => dataRef.current?.tilesets.filter((tile) => tile.kind === 'ground_buildings' || tile.kind === 'roads' || tile.kind === 'parks' || tile.kind === 'water').map((tile) => ({
            kind: tile.kind, url: tile.url, rootLoaded: tilesRef.current.has(tile.url), tilesLoaded: tilesRef.current.get(tile.url)?.tilesLoaded ?? false,
            failed: rootFailuresRef.current.has(tile.url) || flatContentFailuresRef.current.has(tile.url),
            selectedCount: ((tilesRef.current.get(tile.url) as unknown as { _selectedTiles?: unknown[] } | undefined)?._selectedTiles?.length ?? 0),
          })) ?? [],
          volumeStage: () => ({ ...volumeEventsRef.current }),
          selectedModelState: () => ({ loaded: Boolean(selectedModelPrimitiveRef.current?.ready), texturesReady: selectedModelTexturesReadyRef.current, visible: selectedModelVisibleRef.current,
            cityVolumes: (dataRef.current?.tilesets.filter((tile) => tile.kind === 'city_volumes') ?? []).map((tile) => ({
              loaded: tilesRef.current.has(tile.url), shown: tilesRef.current.get(tile.url)?.show ?? false,
              expectedWithoutSelection: visibleRef.current && cityAtScaleRef.current
                && (!groundGateRef.current || groundGateRef.current.overlayStarted)
                && qualityAllows(tile.kind, qualityRef.current.stage),
            })) }),
          failedLayers: () => tileHealthRef.current.active(dataRef.current?.tilesets.map((tile) => tile.url) ?? []),
          cacheBudget: () => [...tilesRef.current.values()].map((tile) => ({ cacheBytes: tile.cacheBytes, maximumCacheOverflowBytes: tile.maximumCacheOverflowBytes, totalMemoryUsageInBytes: tile.totalMemoryUsageInBytes })),
          tileSelection: () => [...tilesRef.current.entries()].map(([url, tile]) => {
            const raw = tile as unknown as { _selectedTiles?: { _contentResource?: { getUrlComponent?: () => string } }[]; _memoryAdjustedScreenSpaceError?: number }
            return { url, cacheBytes: tile.cacheBytes, totalMemoryUsageInBytes: tile.totalMemoryUsageInBytes, maximumScreenSpaceError: tile.maximumScreenSpaceError, memoryAdjustedScreenSpaceError: raw._memoryAdjustedScreenSpaceError, selectedCount: raw._selectedTiles?.length ?? 0, selectedContent: raw._selectedTiles?.slice(0, 20).map((item) => item._contentResource?.getUrlComponent?.() ?? null) ?? [] }
          }),
          frameTimes: () => frameTimes.slice(),
          clearFrameTimes: () => { frameTimes.length = 0 },
          inspectPick: (x: number, y: number) => {
            const position = new Cesium.Cartesian2(x, y)
            const primary = viewer.scene.pick(position) as PickCandidate | undefined
            const stack = viewer.scene.drillPick(position, 8, 14, 14) as PickCandidate[]
            return { primaryEntity: typeof primary?.id === 'string' ? primary.id : primary?.id?.id ?? null,
              target: resolveMapPick(stack, selectedIdRef.current, (object) => facadeIdsRef.current.get(object)),
              stack: stack.map((item) => ({ entity: typeof item.id === 'string' ? item.id : item.id?.id ?? null,
                primitiveType: item.primitive?.constructor.name ?? null })) }
          },
        }
      }
      setReady(true)
    } catch (error) {
      console.error('Map initialization error', error)
      setSceneError('Попробуйте обновить страницу.')
      onStatus('Карта временно недоступна')
      return
    }
    return () => {
      savedCamera = { destination: Cesium.Cartesian3.clone(viewer.camera.positionWC), heading: viewer.camera.heading, pitch: viewer.camera.pitch, roll: viewer.camera.roll }
      observer?.disconnect(); handler?.destroy(); removeFrameListener?.(); removeGroundFrameListener?.(); removeVisibilityListener?.()
      stopProbe('viewer_destroyed')
      cityStageRef.current?.remove(); cityStageRef.current = null
      selectedModelPrimitiveRef.current = null; selectedModelVisibleRef.current = false; selectedModelTexturesReadyRef.current = false
      delete (window as unknown as { __novoMapTest?: unknown }).__novoMapTest
      loadLedgerRef.current.reset()
      viewerRef.current = null
      nearbySourceRef.current = null
      tilesRef.current.clear(); wantedRef.current.clear(); rootFailuresRef.current.clear(); flatContentFailuresRef.current.clear()
      preparedMarkersRef.current = []; activeMarkerIdsRef.current.clear(); groundCoverageRef.current = []
      lastGroundCoverageCountRef.current = 0; cameraEpochRef.current = 0
      tileHealthRef.current.clear(); refreshTileHealth((version) => version + 1)
      groundTileFailedRef.current = false; groundVisibleRef.current = false; groundGateRef.current = null
      baseReadyRef.current = false; lastLegacyFlatKeyRef.current = null
      cityRangeKeyRef.current = null; cityAtScaleRef.current = true; setCityAtScale(true)
      roadDetailAtScaleRef.current = false
      lastMeaningfulPoseRef.current = null; loadRetryUsedRef.current = false
      qualityRef.current.resetToFlat('viewer_destroyed'); setQualityStage('flat')
      groundEventsRef.current = { visible: 0, allLoaded: 0, failed: 0, frames: 0 }
      volumeVisibleRef.current = { any: false, detail: false }
      volumeEventsRef.current = { visible: 0, detailVisible: 0, allLoaded: 0, failed: 0, renderedFrames: 0, lastFrameStage: 'none', lastFrameCameraKey: '' }
      framedRef.current = false
      setOverlayAllowedKey(null); setGroundPhase(null); setRenderedVolume('none'); setReady(false)
      viewer.destroy()
    }
  }, [])

  useEffect(() => {
    const viewer = viewerRef.current
    if (!viewer) return
    viewer.useDefaultRenderLoop = visible
    if (visible) {
      cameraEpochRef.current++
      groundCoverageRef.current = []
      const stage = cityStageRef.current
      if (stage?.silhouetteReady && !movingRef.current) stage.tile.maximumScreenSpaceError = citySseForQuality(qualityRef.current.stage)
      viewer.resize()
      syncRoadLod(viewer)
      reportViewport(viewer)
      viewer.scene.requestRender()
      if (groundGateRef.current?.overlayStarted || baseReadyRef.current) startProbe(viewer, 'map_resumed')
    } else {
      syncVisibleMarkers(viewer, [])
      stopProbe('map_hidden')
      const gate = groundGateRef.current
      if (gate) gate.frame({ visible: false, cameraKey: cameraKey(viewer), groundVisible: false, groundLoaded: false, flatSettled: false, groundFailed: false })
    }
  }, [visible, ready])

  useEffect(() => {
    const viewer = viewerRef.current
    if (!viewer || !ready || !visible || !selectedId || !eligibleSelectedModel(selectedModel)) return
    const modelData = selectedModel
    const matrix = Cesium.Matrix4.fromArray(modelData.model_transform)
    const center = Cesium.Matrix4.getTranslation(matrix, new Cesium.Cartesian3())
    let disposed = false
    let attached: Cesium.Model | null = null
    let removeTexturesReady: (() => void) | null = null
    selectedModelTexturesReadyRef.current = false
    const syncVisibility = () => {
      if (disposed || !attached || viewerRef.current !== viewer) return
      const pixel = Cesium.SceneTransforms.worldToWindowCoordinates(viewer.scene, center)
      const canvas = viewer.scene.canvas
      const onScreen = pixel && pixel.x >= 0 && pixel.x <= canvas.clientWidth && pixel.y >= 0 && pixel.y <= canvas.clientHeight
      setSelectedModelVisible(viewer, Boolean(attached.ready && onScreen && visibleRef.current && document.visibilityState !== 'hidden'))
    }
    const removeRender = viewer.scene.postRender.addEventListener(syncVisibility)
    void Cesium.Model.fromGltfAsync({ url: modelData.asset_url, modelMatrix: matrix, forwardAxis: Cesium.Axis.X,
      incrementallyLoadTextures: true, allowPicking: false, shadows: Cesium.ShadowMode.DISABLED }).then((model) => {
      if (disposed || viewerRef.current !== viewer) { model.destroy(); return }
      removeTexturesReady = model.texturesReadyEvent.addEventListener(() => {
        selectedModelTexturesReadyRef.current = true
        viewer.scene.requestRender()
      })
      attached = viewer.scene.primitives.add(model)
      selectedModelPrimitiveRef.current = attached
      viewer.scene.requestRender()
    }).catch(() => {
      if (!disposed) { setSelectedModelVisible(viewer, false); console.warn('Selected reconstructed model did not load') }
    })
    return () => {
      disposed = true
      removeRender()
      removeTexturesReady?.()
      setSelectedModelVisible(viewer, false)
      if (attached && !viewer.isDestroyed()) viewer.scene.primitives.remove(attached)
      selectedModelPrimitiveRef.current = null
      selectedModelTexturesReadyRef.current = false
    }
  }, [ready, visible, selectedId, selectedModel])

  useEffect(() => {
    if (!ready || !data) return
    if (!flatKey || !groundDescriptor) {
      groundGateRef.current = null
      setGroundPhase(null)
      return
    }
    if (groundGateRef.current?.key === flatKey) return
    stopProbe('new_ground')
    qualityRef.current.resetToFlat('awaiting_ground_frame')
    loadRetryUsedRef.current = false
    groundGateRef.current = new GroundFirstGate(flatKey, groundDescriptor.url, flatTiles.map((tile) => tile.url))
    if (viewerRef.current) applyQuality(viewerRef.current)
    groundTileFailedRef.current = false
    groundVisibleRef.current = false
    groundEventsRef.current = { visible: 0, allLoaded: 0, failed: 0, frames: 0 }
    setOverlayAllowedKey(null)
    setGroundPhase('waiting')
    viewerRef.current?.scene.requestRender()
  }, [flatKey, ready, data])

  useEffect(() => {
    const viewer = viewerRef.current
    if (!viewer) return
    viewer.entities.removeAll()
    highlightedMarkerRef.current = null
    activeMarkerIdsRef.current.clear()
    preparedMarkersRef.current = []
    if (!data) { viewer.scene.requestRender(); return }
    if (data.markers) {
      preparedMarkersRef.current = data.markers.filter((marker) => Number.isFinite(marker.location[0]) && Number.isFinite(marker.location[1])
        && marker.location[0] >= -180 && marker.location[0] <= 180 && marker.location[1] >= -90 && marker.location[1] <= 90)
        .map((marker) => ({ marker, world: Cesium.Cartesian3.fromDegrees(...marker.location, 36),
          longitude: Cesium.Math.toRadians(marker.location[0]), latitude: Cesium.Math.toRadians(marker.location[1]),
          ...(marker.point_type === 'complex_group' ? { groupMembers: marker.group_member_locations.map(([lon, lat]) => {
            const world = Cesium.Cartesian3.fromDegrees(lon, lat, 36)
            return { world: [world.x, world.y, world.z] as const,
              longitude: Cesium.Math.toRadians(lon), latitude: Cesium.Math.toRadians(lat) }
          }) } : {}) }))
      viewer.scene.requestRender()
      return
    }
    const facadeComplexIds = new Set(data.tilesets.filter((tile) => tile.kind === 'facade').map((tile) => tile.id))
    const markers = data.complexes.map((complex) => ({ id: complex.complex_id, name: complex.name, location: complex.location }))
    for (const marker of markers) {
      const [lon, lat] = marker.location
      if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue
      viewer.entities.add({
        id: `complex:${marker.id}`,
        position: Cesium.Cartesian3.fromDegrees(lon, lat, 36),
        point: { pixelSize: 11, color: Cesium.Color.fromCssColorString('#c96747'), outlineColor: Cesium.Color.WHITE, outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY, heightReference: Cesium.HeightReference.NONE, distanceDisplayCondition: facadeComplexIds.has(marker.id) ? new Cesium.DistanceDisplayCondition(1000, 65000) : undefined },
        label: { text: marker.name, font: '600 14px sans-serif', fillColor: Cesium.Color.fromCssColorString('#22302b'), showBackground: true, backgroundColor: Cesium.Color.fromCssColorString('#fffdf8').withAlpha(0.94), backgroundPadding: new Cesium.Cartesian2(9, 6), pixelOffset: new Cesium.Cartesian2(0, -31), verticalOrigin: Cesium.VerticalOrigin.BOTTOM, distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 1000), disableDepthTestDistance: Number.POSITIVE_INFINITY },
      })
    }
    viewer.scene.requestRender()
  }, [data?.markers, data?.complexes, data?.tilesets, ready])

  useEffect(() => {
    const viewer = viewerRef.current
    const source = nearbySourceRef.current
    if (!viewer || !source) return
    source.entities.removeAll()
    for (const { layer, item } of nearbyItems) {
      const { lon, lat } = item.coordinate
      if (!Number.isFinite(lon) || !Number.isFinite(lat) || lon < -180 || lon > 180 || lat < -90 || lat > 90) continue
      const selected = item.id === selectedPoiId
      const color = Cesium.Color.fromCssColorString(NEARBY_COLORS[layer])
      const networkGeometry = validNetworkRouteGeometry(item)
      const accessGeometry = validOriginAccessGeometry(item)
      if (networkGeometry && selected) source.entities.add({ id: `nearby-route:${item.id}`, polyline: {
        positions: networkGeometry.map(([x, y]) => Cesium.Cartesian3.fromDegrees(x, y, 3)),
        width: selected ? 3.5 : 2, material: color.withAlpha(selected ? 0.96 : 0.8),
      } })
      if (accessGeometry && selected) source.entities.add({ id: `nearby-origin-access:${item.id}`, polyline: {
        positions: accessGeometry.map(([x, y]) => Cesium.Cartesian3.fromDegrees(x, y, 4)),
        width: 3, material: new Cesium.PolylineDashMaterialProperty({ color: color.withAlpha(0.86), dashLength: 12 }),
      } })
      source.entities.add({ id: `nearby:${item.id}`, position: Cesium.Cartesian3.fromDegrees(lon, lat, 12),
        point: { pixelSize: selected ? 13 : 9, color, outlineColor: Cesium.Color.WHITE, outlineWidth: selected ? 3 : 2.5,
          disableDepthTestDistance: Number.POSITIVE_INFINITY },
        ...(selected ? { label: { text: item.name, font: '600 12px sans-serif', fillColor: Cesium.Color.fromCssColorString('#22302b'),
          showBackground: true, backgroundColor: Cesium.Color.fromCssColorString('#fffdf8').withAlpha(0.96),
          backgroundPadding: new Cesium.Cartesian2(7, 4), pixelOffset: new Cesium.Cartesian2(0, -24),
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM, disableDepthTestDistance: Number.POSITIVE_INFINITY } } : {}),
      })
    }
    if (visibleRef.current) viewer.scene.requestRender()
  }, [nearbyItems, selectedPoiId, ready])

  useEffect(() => {
    const viewer = viewerRef.current
    if (!viewer || !visible) return
    if (!data) { setLoadedTiles(0); viewer.scene.requestRender(); return }
    if (!flatKey) {
      const legacyFlatKey = data.tilesets.filter((tile) => tile.kind !== 'city_volumes' && tile.kind !== 'facade').map((tile) => tile.url).sort().join('|')
      if (lastLegacyFlatKeyRef.current !== legacyFlatKey) {
        lastLegacyFlatKeyRef.current = legacyFlatKey
        baseReadyRef.current = false
        stopProbe('legacy_base_changed')
        qualityRef.current.resetToFlat('awaiting_legacy_base_frame')
        applyQuality(viewer)
      }
    }
    const cityAllowedAtScale = syncCityRange(viewer, flatKey)
    const roadSse = syncRoadLod(viewer)
    const wanted = new Set(data.tilesets.map((tile) => tile.url))
    const dormantCount = reconcileTiles(tilesRef.current, wanted,
      (tile) => facadeIdsRef.current.has(tile),
      (tile) => { viewer.scene.primitives.remove(tile) })
    if (cityStageRef.current && ![...tilesRef.current.values()].includes(cityStageRef.current.tile)) { cityStageRef.current.remove(); cityStageRef.current = null }
    const groundFirst = Boolean(flatKey)
    const overlayAllowed = !groundFirst || (overlayAllowedKey === flatKey && groundGateRef.current?.overlayStarted === true)
    const activeQuality = qualityRef.current.stage
    for (const tile of data.tilesets.filter((item) => item.kind === 'city_volumes' || item.kind === 'facade')) {
      const existing = tilesRef.current.get(tile.url)
      if (existing) {
        existing.show = overlayAllowed && qualityAllows(tile.kind, activeQuality)
          && (tile.kind !== 'city_volumes' || cityAllowedAtScale)
        if (tile.kind === 'city_volumes' && (cityStageRef.current?.tile !== existing || cityStageRef.current.silhouetteReady))
          existing.maximumScreenSpaceError = citySseForQuality(activeQuality)
      }
    }
    const totalBudget = (window.innerWidth <= 700 ? 128 : 160) * MIB
    const cachePool = Math.max(0, totalBudget - (data.tilesets.length + dormantCount) * CACHE_OVERFLOW_BYTES)
    const weightTotal = data.tilesets.reduce((sum, tile) => sum + tileWeight(tile), 0) || 1
    const cacheBytes = (tile: Tileset) => Math.floor(cachePool * tileWeight(tile) / weightTotal)
    for (const [url, tile] of tilesRef.current) if (!wanted.has(url)) { tile.cacheBytes = 0; tile.maximumCacheOverflowBytes = CACHE_OVERFLOW_BYTES }
    for (const tile of data.tilesets) {
      const existing = tilesRef.current.get(tile.url)
      if (existing) { existing.cacheBytes = cacheBytes(tile); existing.maximumCacheOverflowBytes = CACHE_OVERFLOW_BYTES }
    }
    const activeCount = () => dataRef.current?.tilesets.filter((tile) => tilesRef.current.has(tile.url)).length ?? 0
    setLoadedTiles(activeCount())
    const ensureTile = (tile: Tileset) => {
      if (tilesRef.current.has(tile.url) || rootFailuresRef.current.has(tile.url)) return
      const stageVersion = !groundFirst && tile.kind === 'city_volumes' ? stagedCityVersion(tile.url) : null
      loadLedgerRef.current.ensure(tile.url, () => Cesium.Cesium3DTileset.fromUrl(tile.url, {
        maximumScreenSpaceError: tile.kind === 'ground_buildings' ? 2 : tile.kind === 'roads' ? roadSse : stageVersion ? coarseSse(viewer)
          : tile.kind === 'city_volumes' ? citySseForQuality(activeQuality) : tile.kind === 'facade' ? 8 : 24,
        ...traversalOptions(tile.kind),
        preloadFlightDestinations: false,
        cacheBytes: cacheBytes(tile), maximumCacheOverflowBytes: CACHE_OVERFLOW_BYTES,
      }), (tileset) => {
        if (viewerRef.current !== viewer || !wantedRef.current.has(tile.url)) { tileset.destroy(); return null }
        if (tile.kind === 'roads') tileset.maximumScreenSpaceError = syncRoadLod(viewer)
        tileset.show = visibleRef.current && ((tile.kind !== 'city_volumes' && tile.kind !== 'facade') || !groundGateRef.current || groundGateRef.current.overlayStarted)
          && qualityAllows(tile.kind, qualityRef.current.stage)
          && (tile.kind !== 'city_volumes' || cityAtScaleRef.current)
        if (tile.kind === 'ground_buildings') {
          tileset.tileVisible.addEventListener((visibleTile) => {
            if (!visibleRef.current || groundGateRef.current?.groundUrl !== tile.url) return
            if (visibleTile.hasRenderableContent && visibleTile.contentReady) {
              const volume = visibleTile as unknown as { boundingVolume?: { rectangle?: { west: number; south: number; east: number; north: number } }; boundingSphere?: Cesium.BoundingSphere }
              const rectangle = volume.boundingVolume?.rectangle
              const sphere = volume.boundingSphere
              if (rectangle && sphere && [rectangle.west, rectangle.south, rectangle.east, rectangle.north,
                sphere.center.x, sphere.center.y, sphere.center.z, sphere.radius].every(Number.isFinite)) {
                groundCoverageRef.current.push({ epoch: cameraEpochRef.current,
                  center: [sphere.center.x, sphere.center.y, sphere.center.z], radius: sphere.radius,
                  region: [rectangle.west, rectangle.south, rectangle.east, rectangle.north],
                  geometricError: visibleTile.geometricError })
              }
              if (isPreciseGroundTile(visibleTile.geometricError)) {
                groundVisibleRef.current = true
                groundEventsRef.current.visible++
              }
            }
          })
          tileset.allTilesLoaded.addEventListener(() => { groundEventsRef.current.allLoaded++; retryAfterLoadSettles(viewer); if (visibleRef.current) viewer.scene.requestRender() })
          tileset.tileFailed.addEventListener((error) => {
            groundTileFailedRef.current = true
            groundEventsRef.current.failed++
            if (markTileFailure(tile, 'content', error) === 1) onStatus('Часть контуров зданий недоступна')
            if (visibleRef.current) viewer.scene.requestRender()
          })
        } else if (groundFirst && tile.kind !== 'city_volumes' && tile.kind !== 'facade') {
          tileset.allTilesLoaded.addEventListener(() => { retryAfterLoadSettles(viewer); if (visibleRef.current) viewer.scene.requestRender() })
          tileset.tileFailed.addEventListener((error) => { flatContentFailuresRef.current.add(tile.url); groundEventsRef.current.failed++; if (markTileFailure(tile, 'content', error) === 1) onStatus('Часть плоской карты недоступна'); if (visibleRef.current) viewer.scene.requestRender() })
        }
        if (stageVersion) {
          const stage: { tile: Cesium.Cesium3DTileset; version: 'v6' | 'v7'; silhouetteReady: boolean; remove: () => void } = { tile: tileset, version: stageVersion, silhouetteReady: false, remove: () => {} }
          stage.remove = tileset.allTilesLoaded.addEventListener(() => {
            if (stage.silhouetteReady) return
            stage.silhouetteReady = true
            if (!movingRef.current && viewer.useDefaultRenderLoop) { tileset.maximumScreenSpaceError = citySseForQuality(qualityRef.current.stage); viewer.scene.requestRender() }
          })
          cityStageRef.current = stage
        }
        if (tile.kind === 'city_volumes') {
          tileset.tileVisible.addEventListener((visibleTile) => {
            if (!visibleRef.current || !visibleTile.hasRenderableContent || !visibleTile.contentReady) return
            if (qualityRef.current.stage === 'coarse' && !stageProbeStartedRef.current
              && (!stageVersion || visibleTile.geometricError < 650)) {
              stageContentReadyRef.current = true
              stageProbeStartedRef.current = startProbe(viewer, 'coarse_city_visible') || probeRef.current.active
            }
            const contentUrl = (visibleTile as unknown as { _contentResource?: { getUrlComponent?: () => string } })._contentResource?.getUrlComponent?.() ?? ''
            const detail = /(?:^|\/)cell_[^/]+\.glb(?:\?.*)?$/.test(contentUrl)
            volumeVisibleRef.current.any = true
            volumeVisibleRef.current.detail ||= detail
            volumeEventsRef.current.visible++
            if (detail) volumeEventsRef.current.detailVisible++
          })
          tileset.allTilesLoaded.addEventListener(() => {
            volumeEventsRef.current.allLoaded++
            retryAfterLoadSettles(viewer)
            if ((qualityRef.current.stage === 'balanced' || qualityRef.current.stage === 'detail')
              && !stageProbeStartedRef.current && volumeEventsRef.current.visible > 0) {
              stageContentReadyRef.current = true
              stageProbeStartedRef.current = startProbe(viewer, 'city_refinement_settled') || probeRef.current.active
            }
          })
          tileset.tileFailed.addEventListener((error) => { volumeEventsRef.current.failed++; if (markTileFailure(tile, 'content', error) === 1) onStatus('Часть объёмной карты недоступна') })
        }
        if (tile.kind === 'facade') {
          tileset.tileVisible.addEventListener((visibleTile) => {
            if (!visibleRef.current || !visibleTile.hasRenderableContent || !visibleTile.contentReady) return
            if (qualityRef.current.stage === 'detail' && !facadeProbeStartedRef.current) {
              stageContentReadyRef.current = true
              facadeProbeStartedRef.current = startProbe(viewer, 'facade_visible') || probeRef.current.active
            }
          })
          tileset.tileFailed.addEventListener((error) => { if (markTileFailure(tile, 'content', error) === 1) onStatus('Часть фасадов недоступна') })
        }
        viewer.scene.primitives.add(tileset)
        tilesRef.current.set(tile.url, tileset)
        if (tile.kind === 'facade') facadeIdsRef.current.set(tileset, tile.id)
        setLoadedTiles(activeCount())
        if (visibleRef.current) viewer.scene.requestRender()
        return tileset
      }, (tileset) => { tileset.destroy() }, (error) => {
        if (viewerRef.current === viewer && wantedRef.current.has(tile.url)) {
          rootFailuresRef.current.add(tile.url)
          if (tile.kind === 'ground_buildings') setGroundPhase('ground_failed')
          markTileFailure(tile, 'root', error)
          onStatus(tile.kind === 'ground_buildings' ? 'Контуры зданий временно недоступны' : 'Часть карты временно недоступна')
          if (visibleRef.current) viewer.scene.requestRender()
        }
      })
    }
    const requested = tilesToRequest(data.tilesets, groundFirst, overlayAllowed, cityAllowedAtScale)
      .filter((tile) => qualityAllows(tile.kind, activeQuality))
    for (const tile of requested) ensureTile(tile)
    if (!framedRef.current && data.markers) {
      // The registry already has a usable opening pose. A delayed startup flight can steal a user's first point click.
      framedRef.current = true
    } else if (shouldStartInitialCameraFlight(Boolean(data.markers), framedRef.current)) {
      const featured = data.complexes.find((complex) => data.tilesets.some((tile) => tile.kind === 'facade' && tile.id === complex.complex_id))
      const [lon, lat] = featured?.location ?? data.center ?? MOSCOW
      viewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(Cesium.Cartesian3.fromDegrees(lon, lat, featured ? 30 : 0), 100), { offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(featured ? -50 : -60), featured ? 800 : 4200), duration: 1.3 })
      framedRef.current = true
    }
    viewer.scene.requestRender()
  }, [data, visible, flatKey, overlayAllowedKey, cityAtScale, qualityStage])

  useEffect(() => {
    const viewer = viewerRef.current
    if (!viewer) return
    const start = () => { movingRef.current = true; cameraEpochRef.current++; groundCoverageRef.current = []; syncCityRange(viewer, lastFlatKeyRef.current); syncRoadLod(viewer); groundGateRef.current?.cameraMoved(); groundTileFailedRef.current = false; stopProbe('camera_motion'); const stage = cityStageRef.current; if (stage) { stage.silhouetteReady = false; stage.tile.maximumScreenSpaceError = coarseSse(viewer); viewer.scene.requestRender() } }
    const changed = () => { syncCityRange(viewer, lastFlatKeyRef.current); syncRoadLod(viewer); if (movingRef.current) groundGateRef.current?.cameraMoved(); const stage = cityStageRef.current; if (movingRef.current && stage) { stage.silhouetteReady = false; stage.tile.maximumScreenSpaceError = coarseSse(viewer) } }
    const end = () => {
      movingRef.current = false
      cameraEpochRef.current++
      groundCoverageRef.current = []
      syncCityRange(viewer, lastFlatKeyRef.current)
      syncRoadLod(viewer)
      reportViewport(viewer)
      stopProbe('camera_settled')
      const after = viewPose(viewer)
      const meaningful = !lastMeaningfulPoseRef.current || meaningfulViewChange(lastMeaningfulPoseRef.current, after)
      if (meaningful) {
        lastMeaningfulPoseRef.current = after
        loadRetryUsedRef.current = false
        qualityRef.current.onMeaningfulChange()
        refreshQuality((version) => version + 1)
        stageProbeStartedRef.current = false
        stageContentReadyRef.current = qualityRef.current.stage === 'flat'
      }
      const stage = cityStageRef.current
      if (stage?.silhouetteReady) stage.tile.maximumScreenSpaceError = citySseForQuality(qualityRef.current.stage)
      viewer.scene.requestRender()
      if (meaningful && (groundGateRef.current?.overlayStarted || baseReadyRef.current))
        stageProbeStartedRef.current = startProbe(viewer, 'camera_settled')
    }
    const removeStart = viewer.camera.moveStart.addEventListener(start)
    const removeChanged = viewer.camera.changed.addEventListener(changed)
    const removeEnd = viewer.camera.moveEnd.addEventListener(end)
    return () => { removeStart(); removeChanged(); removeEnd() }
  }, [ready])

  useEffect(() => {
    const viewer = viewerRef.current
    if (!viewer || !data) return
    if (!selectedId) {
      lastSelectionFocusKeyRef.current = null
      paintSelection(viewer, null)
      return
    }
    const skipFlight = skipInitialSelectionRef.current
    skipInitialSelectionRef.current = false
    const marker = data.markers?.find((item) => item.id === selectedId)
    const complex: Complex | undefined = data.complexes.find((item) => item.complex_id === selectedId)
    const location = marker?.location ?? complex?.location
    paintSelection(viewer, selectedId)
    if (!location) return
    const [lon, lat] = location
    const focusKey = selectionFocusKey(selectedId, selectionRequestId)
    if (shouldFlyToSelection(focusKey, lastSelectionFocusKeyRef.current, focusOnSelection, skipFlight)) {
      viewer.camera.cancelFlight()
      framedRef.current = true
      const developerPoint = marker?.point_type === 'developer_building'
      const mobileDeveloper = developerPoint && window.innerWidth <= 700
      viewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(Cesium.Cartesian3.fromDegrees(lon, lat + (mobileDeveloper ? 0.0005 : 0), 30), 100), { offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(developerPoint ? -34 : -48), developerPoint ? 280 : 470), duration: 0.9 })
    }
    if (focusOnSelection || skipFlight) lastSelectionFocusKeyRef.current = focusKey
  }, [selectedId, data, focusOnSelection, selectionRequestId, ready])

  const zoom = (direction: 1 | -1) => {
    const viewer = viewerRef.current
    if (!viewer) return
    const amount = Math.max(150, viewer.camera.positionCartographic.height * 0.35)
    if (direction === 1) viewer.camera.zoomIn(amount)
    else viewer.camera.zoomOut(amount)
  }
  const recenter = () => {
    const viewer = viewerRef.current
    if (!viewer) return
    const [lon, lat] = data?.center ?? MOSCOW
    viewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(Cesium.Cartesian3.fromDegrees(lon, lat, 0), 100), { offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-60), 4200), duration: 0.8 })
  }
  const fitAllPoints = () => {
    const viewer = viewerRef.current
    const locations = data?.markers?.filter((marker) => Number.isFinite(marker.location[0]) && Number.isFinite(marker.location[1])).map((marker) => Cesium.Cartesian3.fromDegrees(...marker.location)) ?? []
    if (!viewer || locations.length === 0) return
    const sphere = Cesium.BoundingSphere.fromPoints(locations)
    sphere.radius = Math.max(100, sphere.radius * 1.12)
    viewer.camera.flyToBoundingSphere(sphere, { offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-60), 0), duration: 0.9 })
  }

  const stageLabel = selectedModelVisibleRef.current ? 'реконструкция выбранного корпуса'
    : deferredCity.length ? 'плоская карта · 3D при приближении'
    : groundPhase && groundPhase !== 'overlay_ready' ? 'плоская подложка'
      : qualityStage === 'flat' && data?.tilesets.some((tile) => tile.kind === 'city_volumes' || tile.kind === 'facade')
        ? qualityRef.current.locked ? '3D приостановлено: карта перегружена'
          : probeRef.current.active ? 'проверяем плавность карты'
            : probeRef.current.reason === 'request_cap' ? '3D отложено: нет кадров для проверки'
              : qualityRef.current.windows > 0 ? '3D отложено по плавности' : '3D ожидает проверки плавности'
        : renderedVolume === 'detail' ? 'детали зданий'
          : renderedVolume === 'volume' ? 'объёмы зданий'
            : data?.tilesets.some((tile) => tile.kind === 'city_volumes') ? 'загружаем здания' : 'плоская карта'

  return <div className="map-shell" data-testid="map">
    <div className="cesium-host" ref={containerRef} />
    {sceneError && <div className="map-fallback" role="status"><strong>Карта временно недоступна</strong><span>{sceneError}</span></div>}
    <div className="map-top"><span className="map-live-dot" /><span className="map-top-title">Карта Москвы</span><span className="map-top-muted">· {stageLabel} · {layerLoadText(connectedAtScale, totalAtScale, activeFailures)}</span><span className="map-top-mobile">{data?.counts.developer_building_points !== undefined ? corpusCount(data.counts.developer_building_points) : layerLoadText(connectedAtScale, totalAtScale, activeFailures)}</span></div>
    {activeFailures.length > 0 && <div className="map-health-alert" role="status"><strong>Часть карты не загрузилась</strong><span>Ошибка тайлов: {activeFailures.map((failure) => failure.label).join(', ')}. Обновите страницу; если ошибка повторится, проверьте данные карты.</span></div>}
    <div className="map-controls"><button aria-label="Приблизить карту" onClick={() => zoom(1)} disabled={!ready}><Plus size={18} /></button><button aria-label="Отдалить карту" onClick={() => zoom(-1)} disabled={!ready}><Minus size={18} /></button><button aria-label="К центру Москвы" onClick={recenter} disabled={!ready}><LocateFixed size={18} /></button></div>
    {data?.markers && <button className="map-fit-all" type="button" onClick={fitAllPoints} disabled={!ready || data.markers.length === 0}>Показать все новостройки</button>}
    {data && <div className="map-key">{data.markers ? data.counts.developer_building_points !== undefined
      ? <><span className="key-dot" /> Корпуса с подтверждённой точкой: {data.counts.developer_building_points ?? 0} <span className="key-divider" /> {markerLabel}</>
      : <><span className="key-dot" /> Реестр: {data.counts.registry_points ?? 0} <span className="key-dot key-dot-mapped" /> Картографические стройки: {data.counts.mapped_construction_points ?? 0} <span className="key-divider" /> {markerLabel}</>
      : <><span className="key-dot" /> {markerLabel} <span className="key-divider" /> {data.tilesets.some((t) => t.model_quality === 'synthetic_massing') ? 'Объёмы зданий схематичны' : 'Данные карты'}</>}</div>}
  </div>
}
