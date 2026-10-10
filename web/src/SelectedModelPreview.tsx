import { useEffect, useMemo, useRef, useState } from 'react'
import * as Cesium from 'cesium'
import type { ModelCandidate, ModelCandidateGeometry } from './api'
import { AdaptiveQuality, BoundedFrameProbe, citySseForQuality, ProbeAttemptLedger, type MapQuality } from './adaptiveQuality'
import { selectedModelRequestAllowed, selectedModelVolumeEligible, selectedPreviewPaths } from './modelReview'

const validRing = (ring: [number, number][] | null): ring is [number, number][] => Boolean(ring && ring.length >= 3 && ring.length <= 128
  && ring.every(([lon, lat]) => Number.isFinite(lon) && Number.isFinite(lat) && lon >= -180 && lon <= 180 && lat >= -90 && lat <= 90))

const evidenceLabel = (basis: string | null | undefined) => basis === 'mapped_height_tag' ? 'Высота указана в картографических данных'
  : basis === 'mapped_floors_times_3m' ? 'Высота оценена по этажности' : 'Подтверждённой высоты нет'

export default function SelectedModelPreview({ item, geometry }: { item: ModelCandidate; geometry: ModelCandidateGeometry }) {
  const flatRef = useRef<HTMLDivElement>(null)
  const viewerHostRef = useRef<HTMLDivElement>(null)
  const [flatPresented, setFlatPresented] = useState(false)
  const [inView, setInView] = useState(false)
  const inViewRef = useRef(false)
  const syncViewerVisibilityRef = useRef<(() => void) | null>(null)
  const [stage, setStage] = useState('Показываем проверенный контур')
  const [error, setError] = useState<string | null>(null)
  const parts = useMemo(() => (geometry.parts ?? []).filter((part) => validRing(part.footprint)).slice(0, 16), [geometry.parts])
  const outline = useMemo(() => selectedPreviewPaths(geometry.preview_geometry, validRing(geometry.footprint) ? geometry.footprint : null,
    parts.map((part) => part.footprint)), [geometry.preview_geometry, geometry.footprint, parts])
  const hasPosition = Boolean(outline && geometry.center && geometry.center.length === 2 && geometry.center.every(Number.isFinite)
    && geometry.bbox && geometry.bbox.length === 4 && geometry.bbox.every(Number.isFinite))
  const tileset = geometry.model_tileset
  const hasVolumeEvidence = selectedModelVolumeEligible(item.depiction, item.render_height_m)

  useEffect(() => {
    setFlatPresented(false)
    setInView(false)
    const element = flatRef.current
    if (!element || !outline) return
    let firstFrame: number | null = null
    let secondFrame: number | null = null
    let visible = false
    const paint = () => {
      if (!visible || document.visibilityState === 'hidden') return
      firstFrame = requestAnimationFrame(() => { secondFrame = requestAnimationFrame(() => {
        if (visible && document.visibilityState !== 'hidden' && element.isConnected) setFlatPresented(true)
      }) })
    }
    const observer = new IntersectionObserver((entries) => {
      visible = entries.some((entry) => entry.isIntersecting && entry.intersectionRatio > 0)
      inViewRef.current = visible
      setInView(visible)
      syncViewerVisibilityRef.current?.()
      if (visible) paint()
    }, { threshold: 0.01 })
    observer.observe(element)
    const visibility = () => { if (document.visibilityState !== 'hidden') paint() }
    document.addEventListener('visibilitychange', visibility)
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', visibility); if (firstFrame !== null) cancelAnimationFrame(firstFrame); if (secondFrame !== null) cancelAnimationFrame(secondFrame) }
  }, [item.candidate_id, outline])

  useEffect(() => {
    const host = viewerHostRef.current
    if (!flatPresented || !inView || !hasPosition || !hasVolumeEvidence || !tileset?.url || !host) return
    let disposed = false
    let renderRaf: number | null = null
    let content: Cesium.Cesium3DTileset | null = null
    let viewer: Cesium.Viewer
    const quality = new AdaptiveQuality()
    const probe = new BoundedFrameProbe()
    const attempts = new ProbeAttemptLedger()
    let activeAttempt: { epoch: number; stage: MapQuality; reason: string } | null = null
    let contentVisible = false
    let probeStartedForStage = false
    let rootRequested = false
    const usable = () => !disposed && inViewRef.current && document.visibilityState !== 'hidden' && host.isConnected && host.getBoundingClientRect().width > 0
    const stopProbe = (reason: string) => {
      if (reason === 'hidden' && activeAttempt) attempts.releaseInterrupted(activeAttempt.epoch, activeAttempt.stage, activeAttempt.reason)
      if (renderRaf !== null) cancelAnimationFrame(renderRaf)
      renderRaf = null
      probe.stop(reason)
      activeAttempt = null
    }
    const applyQuality = () => {
      if (disposed) return
      if (content) {
        content.show = quality.stage !== 'flat'
        content.maximumScreenSpaceError = citySseForQuality(quality.stage)
      }
      viewer.scene.requestRender()
      if (quality.stage === 'flat' && quality.locked) setStage('3D отложено: клиенту не хватает плавности; контур остаётся')
      else if (quality.stage === 'coarse') setStage('Показываем лёгкую модель')
      else if (quality.stage === 'balanced') setStage('Уточняем видимую модель')
      else if (quality.stage === 'detail') setStage('Детали доступны')
    }
    const startProbe = (reason: string) => {
      if (!usable() || quality.locked || probe.active || !attempts.claim(quality.epoch, quality.stage, reason)) return
      probe.start(reason)
      activeAttempt = { epoch: quality.epoch, stage: quality.stage, reason }
      const pump = () => {
        if (!usable()) { stopProbe('hidden'); return }
        if (!probe.request()) {
          if (probe.reason === 'request_cap' && quality.stage !== 'flat') { quality.pauseForStress('probe_no_rendered_frames'); applyQuality() }
          else if (quality.stage === 'flat') setStage('3D отложено: кадры для проверки не получены')
          return
        }
        viewer.scene.requestRender()
        renderRaf = requestAnimationFrame(pump)
      }
      renderRaf = requestAnimationFrame(pump)
    }
    const loadSelected = () => {
      if (rootRequested || disposed || !selectedModelRequestAllowed({ flatPresented, visible: usable(), decision: item.decision,
        assetReadiness: item.asset_readiness, qualityStage: quality.stage, hasVerifiedFootprint: hasPosition,
        depiction: item.depiction, renderHeightM: item.render_height_m })) return
      rootRequested = true
      setStage('Загружаем только выбранную модель')
      void Cesium.Cesium3DTileset.fromUrl(tileset.url, {
        maximumScreenSpaceError: 128, skipLevelOfDetail: false, loadSiblings: false,
        preloadFlightDestinations: false, cacheBytes: 16 * 1024 * 1024, maximumCacheOverflowBytes: 2 * 1024 * 1024,
      }).then((loaded) => {
        if (disposed) { loaded.destroy(); return }
        content = loaded
        loaded.tileVisible.addEventListener((tile) => {
          if (!tile.hasRenderableContent || !tile.contentReady || !usable()) return
          contentVisible = true
          if (quality.stage === 'coarse' && !probeStartedForStage) { probeStartedForStage = true; startProbe('selected_content_visible') }
        })
        loaded.allTilesLoaded.addEventListener(() => {
          if (!contentVisible || (quality.stage !== 'balanced' && quality.stage !== 'detail') || probeStartedForStage) return
          probeStartedForStage = true
          startProbe('selected_refinement_settled')
        })
        loaded.tileFailed.addEventListener(() => { if (!disposed) { setError('Часть выбранной модели не загрузилась; проверенный контур остаётся доступен.'); quality.pauseForStress('selected_tile_failed'); applyQuality(); setStage('3D-контент не загрузился; показываем контур') } })
        viewer.scene.primitives.add(loaded)
        applyQuality()
      }).catch(() => { if (!disposed) { setError('Выбранная модель не загрузилась; проверенный контур остаётся доступен.'); setStage('Показываем проверенный контур') } })
    }
    try {
      viewer = new Cesium.Viewer(host, {
        baseLayer: false, terrainProvider: new Cesium.EllipsoidTerrainProvider(), animation: false, timeline: false,
        geocoder: false, homeButton: false, sceneModePicker: false, baseLayerPicker: false,
        navigationHelpButton: false, fullscreenButton: false, infoBox: false, selectionIndicator: false,
        skyBox: false, skyAtmosphere: false, useBrowserRecommendedResolution: false,
        requestRenderMode: true, maximumRenderTimeChange: Number.POSITIVE_INFINITY,
      })
      viewer.resolutionScale = Math.min(1, 1.5 / window.devicePixelRatio)
      viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString('#deddd6')
      viewer.scene.backgroundColor = Cesium.Color.fromCssColorString('#deddd6')
      viewer.scene.globe.showGroundAtmosphere = false
      viewer.scene.fog.enabled = false
      viewer.scene.screenSpaceCameraController.enableInputs = false
      const [lon, lat] = geometry.center!
      const bbox = geometry.bbox
      const spanMetres = bbox ? Math.max(Math.abs(bbox[2] - bbox[0]) * 63_000, Math.abs(bbox[3] - bbox[1]) * 111_000) : 100
      viewer.camera.lookAt(Cesium.Cartesian3.fromDegrees(lon, lat), new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-57), Math.max(220, spanMetres * 2.6)))
      viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY)
      const renderRemove = viewer.scene.postRender.addEventListener(() => {
        if (disposed || !usable()) return
        if (!probe.active && quality.stage === 'flat' && !rootRequested) { startProbe('flat_preview_ready'); return }
        const sample = probe.rendered(performance.now(), true)
        if (sample.interval === null) return
        const wasLocked = quality.locked
        if (quality.observeFrame(sample.interval, true) || (!wasLocked && quality.locked)) { stopProbe('severe_frame'); applyQuality(); return }
        if (!sample.window) return
        const outcome = quality.observeWindow({ samples: sample.window, activeProbe: true, visible: true, moving: false, contentReady: quality.stage === 'flat' || contentVisible })
        if (outcome.changed) {
          stopProbe('stage_changed'); probeStartedForStage = false; applyQuality()
          if (quality.stage === 'coarse') loadSelected()
        } else if (quality.locked) { stopProbe('stress_lock'); applyQuality() }
      })
      const onVisibility = () => {
        if (!usable()) { viewer.useDefaultRenderLoop = false; stopProbe('hidden') }
        else { viewer.useDefaultRenderLoop = true; viewer.resize(); viewer.scene.requestRender(); if (!quality.locked && (!rootRequested || contentVisible)) startProbe(rootRequested ? 'resumed_content' : 'resumed_flat') }
      }
      syncViewerVisibilityRef.current = onVisibility
      document.addEventListener('visibilitychange', onVisibility)
      const resize = new ResizeObserver(() => { if (disposed) return; viewer.resize(); viewer.scene.requestRender() })
      resize.observe(host)
      viewer.scene.requestRender()
      return () => {
        disposed = true
        syncViewerVisibilityRef.current = null
        document.removeEventListener('visibilitychange', onVisibility)
        resize.disconnect(); renderRemove(); stopProbe('destroyed')
        viewer.destroy()
      }
    } catch {
      setError('3D-предпросмотр недоступен на этом устройстве; проверенный контур остаётся доступен.')
      return () => { disposed = true; syncViewerVisibilityRef.current = null; stopProbe('destroyed'); if (viewer! && !viewer.isDestroyed()) viewer.destroy() }
    }
  }, [flatPresented, hasPosition, hasVolumeEvidence, tileset?.url, item.candidate_id, geometry.center, geometry.bbox])

  return <div className="selected-model-preview" data-testid="selected-model-preview">
    <div className="model-preview-title"><strong>Предпросмотр выбранной геометрии</strong><span>{evidenceLabel(item.height_evidence?.render_height_basis)}</span></div>
    <div className="model-preview-grid">
      <div className="model-flat-preview" ref={flatRef}><span className="model-preview-overline">Проверенное пятно</span>{outline ? <svg viewBox="0 0 320 200" role="img" aria-label="Контур выбранной стройки"><rect width="320" height="200" fill="#e9eee7" />{outline.polygons.map((path, index) => <path key={index} d={path} fill="#aab2af" fillRule="evenodd" stroke="#566576" strokeWidth="2" />)}{outline.parts.map((path, index) => <path key={index} d={path} fill="none" stroke="#c07155" strokeWidth="2" strokeDasharray="4 3" />)}</svg> : <p>Подтверждённый контур для плоского предпросмотра не передан.</p>}</div>
      <div className="model-volume-preview"><span className="model-preview-overline">{hasVolumeEvidence ? 'Оценочный объём' : 'Картографический контур'}</span>{hasVolumeEvidence && hasPosition && tileset ? <div className="model-preview-canvas" ref={viewerHostRef} aria-label="3D-предпросмотр выбранной модели" /> : <p>{!hasVolumeEvidence ? 'Высота не указана: показываем только проверенный плоский контур без 3D-загрузки.' : 'Нет подтверждённой геопривязки или разрешённого файла для 3D-показа.'}</p>}<span className="model-preview-stage" role="status">{stage}</span></div>
    </div>
    {error && <p className="model-preview-error" role="alert">{error}</p>}
    {tileset?.attribution && <p className="model-preview-credit">{tileset.attribution}</p>}
    <p className="model-preview-note">{item.depiction === 'construction_outline' ? 'Контур стройки не является проектной моделью корпуса.' : item.depiction === 'schematic_massing' ? 'Объём оценочный; фасад и внутренняя планировка не подтверждены.' : 'Показывается только разрешённая геометрия выбранного кандидата.'}</p>
  </div>
}
