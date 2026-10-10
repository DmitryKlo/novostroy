import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRight, Building2, ChevronDown, Compass, MapPin, RefreshCw, Search, SlidersHorizontal, X } from 'lucide-react'
import MapView from './MapView'
import DataDashboard from './DataDashboard'
import NearbyPanel from './NearbyPanel'
import DeveloperExplorer from './DeveloperExplorer'
import DeveloperFilterPanel from './DeveloperFilterPanel'
import useMobileViewport from './useMobileViewport'
import { api, RegistrySnapshotChanged, type DeveloperModel, type Health, type MapData, type MapPoint, type MapPointsResult, type NearbyCategory, type NearbyEducation, type NearbySelection, type NearbyViewport, type RegistryBuilding, type RegistryFilters, type RegistryList, type RegistryMap } from './api'
import { coincidentPoints, groupMarkerId, pointAction, pointAddressCaption, pointComplexCardCaption, pointDeveloperCardCaption, pointDisplayMarkers, pointRegistryLinkCaption, unlocatedRegistryCount } from './mapPoints'
import { expandGroupsForCameraHeight, groupMapPoints, mapPointDisplayItems } from './mapPointGroups'
import { developerUnitFilterCount, developerUnitFilterSummary, emptyDeveloperUnitFilters, parseDeveloperUnitFilters, selectedDeveloperUnitCriteria, type DeveloperUnitFilterDraft } from './developerUnitFilters'
import { boundedNearbyBbox, NearbyCache, nearbyKey, nearbyViewportKey, NEARBY_CATEGORIES, noNearbyLayers, shownNearbyItems, validNearbyResult, validNearbyViewport, type NearbyVisibility } from './nearby'

const PAGE_SIZE = 48
const number = (value: number) => new Intl.NumberFormat('ru-RU').format(value)
const known = (value: string | number | null | undefined) => value === null || value === undefined || value === '' ? 'Не указано' : String(value)
const snapshotDate = (value: string | null | undefined) => {
  if (!value) return 'дата не указана'
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(parsed)
}
const validLocation = (item: RegistryBuilding): item is RegistryBuilding & { location: [number, number] } => item.location_quality === 'verified' && !!item.location && item.location.length === 2 && item.location.every(Number.isFinite)
const kindLabel = (kind: string | null) => ({ multifamily: 'Многоквартирный дом', apartment_complex: 'Апартаментный комплекс', low_rise: 'Малоэтажный дом' })[kind ?? ''] ?? known(kind)
const WALK_LIMITS = [5, 10, 15, 20, 30, 45, 60]

function WalkFilter({ id, label, value, onChange }: { id: string; label: string; value: string; onChange: (value: string) => void }) {
  return <div className="filter-field"><label htmlFor={id}>{label}</label><select id={id} value={value} onChange={(event) => onChange(event.target.value)}><option value="">Без ограничения</option>{WALK_LIMITS.map((minutes) => <option key={minutes} value={minutes}>До {minutes} мин</option>)}</select></div>
}

function BuildingCard({ item, selected, onSelect, developerMode }: { item: RegistryBuilding; selected: boolean; onSelect: () => void; developerMode: boolean }) {
  return <article className={`registry-card ${selected ? 'selected' : ''}`} data-testid={`building-${item.registry_id}`}>
    <button type="button" className="registry-card-button" onClick={onSelect} aria-pressed={selected}>
      <span className="registry-card-kicker"><Building2 size={15} /> {developerMode ? 'Корпус из каталога квартир' : kindLabel(item.record_kind)}</span>
      <strong className="registry-card-title">{item.name}</strong>
      <span className="registry-card-address"><MapPin size={14} /> {known(item.address)}{developerMode && item.address_complete === false ? ' · частичный адрес' : ''}</span>
      {developerMode ? <span className="registry-card-source-facts"><span>ЖК: {known(item.complex_label)}</span><span>Застройщик: {known(item.developer_name)}</span>{item.matching_units_count != null && <span>Квартир, подходящих под фильтры: {number(item.matching_units_count)}</span>}</span>
        : <span className="registry-card-facts"><span><small>Статус по выгрузке</small><b>{known(item.status)}</b></span><span><small>Срок сдачи по выгрузке</small><b>{known(item.handover)}</b></span><span><small>Этажность</small><b>{item.floors == null ? 'Не указана' : `${item.floors} эт.`}</b></span></span>}
      <span className="registry-card-foot"><span>{validLocation(item) ? 'Точка корпуса подтверждена' : 'Точка корпуса не подтверждена'}</span><span>Сведения: {snapshotDate(developerMode ? item.observed_at : item.latest_known_date)}</span></span>
    </button>
  </article>
}

export default function RegistryApp({ health, active = true, routeView = 'catalog', onCatalog, onData, onModels }: { health: Health; active?: boolean; routeView?: 'catalog' | 'data'; onCatalog?: () => void; onData?: () => void; onModels?: (registryId?: string, candidateId?: string) => void }) {
  const developerMode = health.catalog_view === 'developer_buildings'
  const mobile = useMobileViewport()
  const [view, setView] = useState<'catalog' | 'data'>(routeView)
  const [mobilePanel, setMobilePanel] = useState<'results' | 'map'>('results')
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('')
  const [kind, setKind] = useState('')
  const [district, setDistrict] = useState('')
  const [handoverYear, setHandoverYear] = useState('')
  const [handoverQuarter, setHandoverQuarter] = useState('')
  const [floorsMin, setFloorsMin] = useState('')
  const [floorsMax, setFloorsMax] = useState('')
  const [walkMetro, setWalkMetro] = useState('')
  const [walkSchool, setWalkSchool] = useState('')
  const [walkKindergarten, setWalkKindergarten] = useState('')
  const [walkMedical, setWalkMedical] = useState('')
  const [filterError, setFilterError] = useState<string | null>(null)
  const [filtersOpen, setFiltersOpen] = useState(developerMode)
  const [unitDraft, setUnitDraft] = useState<DeveloperUnitFilterDraft>(emptyDeveloperUnitFilters)
  const [applied, setApplied] = useState<RegistryFilters>({})
  const [list, setList] = useState<RegistryList | null>(null)
  const [map, setMap] = useState<RegistryMap | null>(null)
  const [points, setPoints] = useState<MapPointsResult | null>(null)
  const [selected, setSelected] = useState<RegistryBuilding | null>(null)
  const [explorerBuilding, setExplorerBuilding] = useState<RegistryBuilding | null>(null)
  const [selectedPoint, setSelectedPoint] = useState<MapPoint | null>(null)
  const [selectedModel, setSelectedModel] = useState<DeveloperModel | null>(null)
  const [selectedModelVisible, setSelectedModelVisible] = useState(false)
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null)
  const [expandGroupsNear, setExpandGroupsNear] = useState(true)
  const [focusOnSelection, setFocusOnSelection] = useState(false)
  const [selectionRequestId, setSelectionRequestId] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [snapshotStale, setSnapshotStale] = useState(false)
  const [mapStatus, setMapStatus] = useState<string | null>(null)
  const [nearbyVisibility, setNearbyVisibility] = useState<NearbyVisibility>(noNearbyLayers)
  const [nearbyEducation, setNearbyEducation] = useState<NearbyEducation>('all')
  const [nearbyResult, setNearbyResult] = useState<{ key: string; data: NearbySelection } | null>(null)
  const [nearbyLoading, setNearbyLoading] = useState(false)
  const [nearbyError, setNearbyError] = useState<string | null>(null)
  const [selectedPoiId, setSelectedPoiId] = useState<string | null>(null)
  const [viewportBbox, setViewportBbox] = useState<[number, number, number, number] | null>(null)
  const [viewportResult, setViewportResult] = useState<{ key: string; data: NearbyViewport } | null>(null)
  const [viewportLoading, setViewportLoading] = useState(false)
  const [viewportError, setViewportError] = useState<string | null>(null)
  const nearbyCache = useRef(new NearbyCache())
  const viewportCache = useRef(new NearbyCache<NearbyViewport>(8, 30_000, 1024 * 1024))
  const nearbyRequestId = useRef(0)
  const viewportRequestId = useRef(0)
  const requestId = useRef(0)
  const activeAbort = useRef<AbortController | null>(null)
  const pageAbort = useRef<AbortController | null>(null)
  const selectionId = useRef(0)
  const explorerOpener = useRef<HTMLElement | null>(null)
  const sidebarRef = useRef<HTMLElement>(null)
  const appliedKey = JSON.stringify(applied)
  const currentKey = useRef(appliedKey)
  currentKey.current = appliedKey
  useEffect(() => setView(routeView), [routeView])
  const showCatalog = () => { setView('catalog'); onCatalog?.() }
  const showData = () => { setExplorerBuilding(null); setView('data'); onData?.() }

  const reload = useCallback(async (filters: RegistryFilters, signal: AbortSignal, id: number) => {
    ++selectionId.current
    pageAbort.current?.abort()
    setLoading(true); setLoadingMore(false); setError(null); setSnapshotStale(false)
    setList(null); setMap(null); setPoints(null); setSelected(null); setSelectedPoint(null); setSelectedGroupId(null); setExplorerBuilding(null); setNearbyVisibility(noNearbyLayers())
    try {
      const { list: nextList, map: nextMap, points: nextPoints } = await api.registryPointView(filters, health.registry_snapshot_id, signal, developerMode ? 'developer_buildings' : 'building_registry', developerMode ? health.map_run_id : null, developerMode ? health.unit_run_id : null)
      if (signal.aborted || id !== requestId.current) return
      setList(nextList); setMap(nextMap); setPoints(nextPoints)
      setSelected(null); setSelectedPoint(null); setSelectedGroupId(null)
    } catch (reason) {
      if (!signal.aborted && id === requestId.current) { setSnapshotStale(reason instanceof RegistrySnapshotChanged); setError(reason instanceof Error ? reason.message : 'Не удалось открыть реестр') }
    } finally { if (!signal.aborted && id === requestId.current) setLoading(false) }
  }, [health.registry_snapshot_id, health.map_run_id, health.unit_run_id, developerMode])
  useEffect(() => {
    const controller = new AbortController()
    activeAbort.current?.abort()
    activeAbort.current = controller
    const id = ++requestId.current
    void reload(applied, controller.signal, id)
    return () => { controller.abort(); ++selectionId.current }
  }, [appliedKey, reload])
  useEffect(() => () => pageAbort.current?.abort(), [])
  useEffect(() => {
    setNearbyVisibility(selectedPoint ? { metro: true, education: true, medical: true } : noNearbyLayers())
    setSelectedPoiId(null)
  }, [selectedPoint?.point_id])
  const nearbyPointId = selectedPoint?.point_id ?? null
  const nearbySnapshot = points?.registry_snapshot_id ?? null
  const nearbyMapRun = developerMode ? points?.map_run_id ?? null : points?.model_run_id ?? null
  const nearbyAnyVisible = nearbyVisibility.metro || nearbyVisibility.education || nearbyVisibility.medical
  const mapVisible = active && view === 'catalog' && !explorerBuilding && (!mobile || mobilePanel === 'map')
  const modelRegistryId = selected?.registry_id ?? (selectedPoint?.point_type === 'developer_building' ? selectedPoint.registry_id : null)
  useEffect(() => {
    setSelectedModel(null)
    if (!developerMode || !mapVisible || !modelRegistryId || selectedGroupId) return
    const controller = new AbortController()
    void api.developerModel(modelRegistryId, controller.signal).then((value) => {
      if (controller.signal.aborted) return
      if (value.registry_id !== modelRegistryId || value.registry_snapshot_id !== health.registry_snapshot_id
        || value.map_run_id !== health.map_run_id || (list?.unit_run_id && value.unit_run_id !== list.unit_run_id)) return
      setSelectedModel(value)
    }).catch(() => { if (!controller.signal.aborted) setSelectedModel(null) })
    return () => controller.abort()
  }, [developerMode, mapVisible, modelRegistryId, selectedGroupId, health.registry_snapshot_id, health.map_run_id, list?.unit_run_id])
  const viewportLayers = NEARBY_CATEGORIES.filter((layer) => nearbyVisibility[layer])
  const viewportLayersKey = viewportLayers.join(',')
  const activeNearbyKey = nearbyPointId && nearbySnapshot && nearbyMapRun
    ? nearbyKey(nearbySnapshot, nearbyMapRun, nearbyPointId, nearbyEducation) : null
  const activeViewportKey = !nearbyPointId && nearbySnapshot && nearbyMapRun && viewportBbox && viewportLayers.length && mapVisible
    ? nearbyViewportKey(nearbySnapshot, nearbyMapRun, viewportBbox, viewportLayers, nearbyEducation) : null
  useEffect(() => {
    const id = ++nearbyRequestId.current
    if (!active || view !== 'catalog' || !nearbyPointId || !nearbySnapshot || !nearbyMapRun || !nearbyAnyVisible || !activeNearbyKey) {
      setNearbyLoading(false); setNearbyError(null)
      return
    }
    const cached = nearbyCache.current.get(activeNearbyKey)
    if (cached && validNearbyResult(cached, nearbyPointId, nearbyEducation, nearbySnapshot, nearbyMapRun, developerMode)) {
      setNearbyResult({ key: activeNearbyKey, data: cached }); setNearbyLoading(false); setNearbyError(null)
      return
    }
    const controller = new AbortController()
    setNearbyResult(null); setNearbyLoading(true); setNearbyError(null)
    void api.nearbyPoint(nearbyPointId, nearbyEducation, controller.signal).then((data) => {
      if (controller.signal.aborted || id !== nearbyRequestId.current) return
      if (!validNearbyResult(data, nearbyPointId, nearbyEducation, nearbySnapshot, nearbyMapRun, developerMode)) throw new RegistrySnapshotChanged('Снимок объектов рядом обновился. Обновите страницу.')
      nearbyCache.current.set(activeNearbyKey, data)
      setNearbyResult({ key: activeNearbyKey, data }); setNearbyLoading(false)
    }).catch((reason) => {
      if (controller.signal.aborted || id !== nearbyRequestId.current) return
      setNearbyError(reason instanceof Error ? reason.message : 'Не удалось загрузить объекты рядом')
      setNearbyLoading(false)
    })
    return () => { controller.abort(); ++nearbyRequestId.current }
  }, [active, view, nearbyPointId, nearbySnapshot, nearbyMapRun, nearbyEducation, nearbyAnyVisible, activeNearbyKey, developerMode])
  useEffect(() => {
    const id = ++viewportRequestId.current
    if (!activeViewportKey || !viewportBbox || !nearbySnapshot || !nearbyMapRun) {
      setViewportLoading(false); setViewportError(null)
      return
    }
    const requestedLayers = viewportLayersKey.split(',') as NearbyCategory[]
    const cached = viewportCache.current.get(activeViewportKey)
    if (cached && validNearbyViewport(cached, viewportBbox, requestedLayers, nearbyEducation, nearbySnapshot, nearbyMapRun, developerMode)) {
      setViewportResult({ key: activeViewportKey, data: cached }); setViewportLoading(false); setViewportError(null)
      return
    }
    const controller = new AbortController()
    setViewportResult(null); setViewportLoading(true); setViewportError(null)
    const timer = window.setTimeout(() => {
      void api.nearbyViewport(viewportBbox, requestedLayers, nearbyEducation, controller.signal).then((data) => {
        if (controller.signal.aborted || id !== viewportRequestId.current) return
        if (!validNearbyViewport(data, viewportBbox, requestedLayers, nearbyEducation, nearbySnapshot, nearbyMapRun, developerMode)) throw new RegistrySnapshotChanged('Снимок объектов на карте обновился. Обновите страницу.')
        viewportCache.current.set(activeViewportKey, data)
        setViewportResult({ key: activeViewportKey, data }); setViewportLoading(false)
      }).catch((reason) => {
        if (controller.signal.aborted || id !== viewportRequestId.current) return
        setViewportError(reason instanceof Error ? reason.message : 'Не удалось загрузить объекты на карте')
        setViewportLoading(false)
      })
    }, 180)
    return () => { window.clearTimeout(timer); controller.abort(); ++viewportRequestId.current }
  }, [activeViewportKey, viewportBbox, viewportLayersKey, nearbyEducation, nearbySnapshot, nearbyMapRun, developerMode])

  const grouping = useMemo(() => points ? groupMapPoints(points, points.group_evidence) : { groups: [], individuals: [] }, [points])
  const selectedGroup = grouping.groups.find((group) => group.group_id === selectedGroupId) ?? null
  const selectedPointGroup = selectedPoint ? grouping.groups.find((group) => group.member_ids.includes(selectedPoint.point_id)) ?? null : null
  const displayItems = useMemo(() => mapPointDisplayItems(grouping, {
    expanded_group_ids: expandGroupsNear ? grouping.groups.map((group) => group.group_id) : [],
    selected_group_id: selectedGroupId,
    selected_point_id: selectedPoint?.point_id,
  }), [grouping, expandGroupsNear, selectedGroupId, selectedPoint?.point_id])
  const mapData = useMemo<MapData | null>(() => map && points ? {
    snapshot_id: map.snapshot_id,
    data_mode: health.data_mode,
    center: map.center,
    tilesets: map.tilesets,
    complexes: [],
    markers: pointDisplayMarkers(displayItems),
    counts: { ...points.counts, offers: 0 },
  } : null, [map, points, health.data_mode, displayItems])

  const submit = (event?: React.FormEvent) => {
    event?.preventDefault()
    if (!developerMode && floorsMin && floorsMax && Number(floorsMin) > Number(floorsMax)) {
      setFilterError('Минимальная этажность не может быть выше максимальной.')
      return
    }
    const developerFilters = developerMode ? parseDeveloperUnitFilters(unitDraft) : null
    if (developerFilters?.error) { setFilterError(developerFilters.error); return }
    setFilterError(null)
    ++selectionId.current
    setView('catalog'); setMobilePanel('results'); setSelected(null); setSelectedPoint(null); setSelectedGroupId(null); setNearbyVisibility(noNearbyLayers())
    const next = developerMode ? { query: query.trim() || undefined, ...developerFilters!.filters } : { query: query.trim() || undefined, status: status || undefined, kind: kind || undefined, district: district || undefined,
      handover_year: handoverYear ? Number(handoverYear) : undefined,
      handover_quarter: handoverQuarter ? Number(handoverQuarter) : undefined,
      floors_min: floorsMin ? Number(floorsMin) : undefined,
      floors_max: floorsMax ? Number(floorsMax) : undefined,
      walk_metro_max_min: walkMetro ? Number(walkMetro) : undefined,
      walk_school_max_min: walkSchool ? Number(walkSchool) : undefined,
      walk_kindergarten_max_min: walkKindergarten ? Number(walkKindergarten) : undefined,
      walk_medical_max_min: walkMedical ? Number(walkMedical) : undefined }
    if (JSON.stringify(next) !== currentKey.current) { setList(null); setMap(null); setPoints(null) }
    setApplied(next)
  }
  const closeSelection = () => { ++selectionId.current; setSelected(null); setSelectedPoint(null); setSelectedGroupId(null); setExplorerBuilding(null); setNearbyVisibility(noNearbyLayers()) }
  const clear = () => { ++selectionId.current; setQuery(''); setStatus(''); setKind(''); setDistrict(''); setHandoverYear(''); setHandoverQuarter(''); setFloorsMin(''); setFloorsMax(''); setWalkMetro(''); setWalkSchool(''); setWalkKindergarten(''); setWalkMedical(''); setUnitDraft(emptyDeveloperUnitFilters()); setFilterError(null); setSelected(null); setSelectedPoint(null); setSelectedGroupId(null); setExplorerBuilding(null); setNearbyVisibility(noNearbyLayers()); if (currentKey.current !== '{}') { setList(null); setMap(null); setPoints(null) }; setApplied({}) }
  const updateUnitDraft = (key: keyof DeveloperUnitFilterDraft, value: string) => setUnitDraft((current) => ({ ...current, [key]: value }))
  const select = (item: RegistryBuilding, fromMap = false) => {
    ++selectionId.current
    setExplorerBuilding(null)
    setSelected(item); setSelectedPoint(points?.items.find((point) => point.registry_id === item.registry_id) ?? null); setSelectedGroupId(null); setFocusOnSelection(validLocation(item)); setSelectionRequestId((value) => value + 1); setView('catalog')
    if (!fromMap && validLocation(item)) setMobilePanel('map')
    if (fromMap) { setMobilePanel('map'); sidebarRef.current?.scrollTo({ top: 0, behavior: 'smooth' }) }
  }
  const closeExplorer = () => {
    setExplorerBuilding(null)
    requestAnimationFrame(() => {
      if (explorerOpener.current?.isConnected) explorerOpener.current.focus()
      else document.querySelector<HTMLElement>('[data-testid="open-developer-explorer"]')?.focus()
    })
  }
  const openDeveloperExplorer = (item: RegistryBuilding, trigger: HTMLElement) => {
    explorerOpener.current = trigger
    setExplorerBuilding(item)
  }
  const openRegistryPoint = (point: MapPoint, openUnits = false, trigger?: HTMLElement) => {
    if (point.point_type !== 'registry' && point.point_type !== 'developer_building') return
    const found = list?.items.find((item) => item.registry_id === point.registry_id) ?? map?.items.find((item) => item.registry_id === point.registry_id)
    if (found) { select(found, true); if (openUnits && trigger) openDeveloperExplorer(found, trigger); return }
    const idAtStart = ++selectionId.current
    void api.registryBuilding(point.registry_id).then((item) => { if (idAtStart === selectionId.current) { select(item, true); if (openUnits && trigger) openDeveloperExplorer(item, trigger) } })
      .catch(() => { if (idAtStart === selectionId.current) setError('Не удалось открыть запись здания') })
  }
  const selectMarker = (id: string) => {
    const visualGroup = grouping.groups.find((group) => groupMarkerId(group) === id)
    if (visualGroup) {
      ++selectionId.current
      setSelectedGroupId(visualGroup.group_id); setSelectedPoint(null); setSelected(null)
      setNearbyVisibility(noNearbyLayers()); setView('catalog'); setMobilePanel('map')
      return
    }
    const group = coincidentPoints(points?.items ?? [], id)
    if (!group.length) return
    const picked = group.find((point) => point.point_id === id)!
    ++selectionId.current
    setSelectedPoint(picked); setSelected(null); setSelectedGroupId(null); setFocusOnSelection(true); setSelectionRequestId((value) => value + 1); setView('catalog'); setMobilePanel('map')
    if (group.length === 1 && pointAction(picked).kind === 'registry') openRegistryPoint(picked)
  }
  const loadMore = async () => {
    if (!list || loadingMore || list.items.length >= list.records_total) return
    const key = currentKey.current
    const controller = new AbortController()
    pageAbort.current = controller
    setLoadingMore(true)
    try {
      const page = await api.registryBuildings({ ...applied, limit: PAGE_SIZE, offset: list.items.length }, controller.signal)
      if (currentKey.current !== key) return
      if (page.snapshot_id !== list.snapshot_id || page.dataset_id !== list.dataset_id
        || (developerMode && (page.map_run_id !== list.map_run_id || page.unit_run_id !== list.unit_run_id))) {
        setSnapshotStale(true); setError('Реестр обновился. Обновите страницу, чтобы получить новый снимок.')
        return
      }
      setList((current) => current && current.snapshot_id === page.snapshot_id ? { ...current, items: [...current.items, ...page.items] } : current)
    } catch (reason) { if (!controller.signal.aborted && currentKey.current === key) setError(reason instanceof Error ? reason.message : 'Не удалось загрузить следующие записи') }
    finally { if (pageAbort.current === controller) pageAbort.current = null; if (currentKey.current === key) setLoadingMore(false) }
  }
  const shown = selected ? [selected] : list?.items ?? []
  const selectedMarkerId = selectedPoint?.point_id ?? (selected ? points?.items.find((point) => point.registry_id === selected.registry_id)?.point_id : null) ?? null
  const coincident = selectedPoint ? coincidentPoints(points?.items ?? [], selectedPoint.point_id) : []
  const unlocated = list && points ? unlocatedRegistryCount(list.records_total, points.counts.registry_points) : null
  const nearbyData = activeNearbyKey && nearbyResult?.key === activeNearbyKey ? nearbyResult.data : null
  const activeUnitFilterSummary = developerMode ? developerUnitFilterSummary(applied) : null
  const viewportData = activeViewportKey && viewportResult?.key === activeViewportKey ? viewportResult.data : null
  const nearbyItems = useMemo(() => nearbyPointId ? shownNearbyItems(nearbyData, nearbyVisibility)
    : NEARBY_CATEGORIES.flatMap((layer) => nearbyVisibility[layer]
      ? (viewportData?.layers[layer]?.items ?? []).map((item) => ({ layer, item })) : []),
  [nearbyPointId, nearbyData, viewportData, nearbyVisibility])
  const toggleNearby = (layer: NearbyCategory) => setNearbyVisibility((current) => ({ ...current, [layer]: !current[layer] }))

  return <div className="app-shell registry-app">
    <header className="site-header"><a className="brand" href="/" onClick={(event) => { event.preventDefault(); showCatalog(); closeSelection() }}><span className="brand-mark"><Building2 size={19} strokeWidth={2.4} /></span><span>ново<span className="brand-period">.</span></span></a><span className="brand-descriptor">Реестр новостроек Москвы</span><nav className="main-nav" aria-label="Основная навигация"><a href="/" className={view === 'catalog' ? 'nav-link active' : 'nav-link'} aria-current={view === 'catalog' ? 'page' : undefined} onClick={(event) => { event.preventDefault(); showCatalog() }}>Каталог</a>{!developerMode && <a className="nav-link" href="/models?decision=rejected" onClick={(event) => { if (onModels && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) { event.preventDefault(); onModels() } }}>Модели</a>}<a href="/data" className={view === 'data' ? 'nav-link active' : 'nav-link'} aria-current={view === 'data' ? 'page' : undefined} onClick={(event) => { event.preventDefault(); showData() }}>Данные</a></nav><div className="header-location"><MapPin size={15} /> Москва <ChevronDown size={14} /></div></header>
    {view === 'data' && <DataDashboard snapshotId={health.snapshot_id} registrySnapshotId={health.registry_snapshot_id} criteria={[]} onCatalog={showCatalog} registryMode developerMode={developerMode} />}
    <div className="app-grid" hidden={view === 'data'} inert={view === 'data'} aria-hidden={view === 'data'}>
      <aside ref={sidebarRef} className={`sidebar ${mobilePanel === 'map' ? 'mobile-hide' : ''}`}>
        <div className="sidebar-intro"><div className="eyebrow"><span className="eyebrow-line" /> {developerMode ? 'МОСКВА · ДОМА ИЗ КАТАЛОГА КВАРТИР' : 'МОСКВА · РЕЕСТР ЗДАНИЙ'}</div><h1>Новые дома<br /><i>на карте Москвы.</i></h1>{developerMode ? <p>Корпуса, найденные в каталоге квартир. Адрес, ЖК и застройщик приведены по данным снимка; текущее наличие квартир и цены не подтверждены.</p> : <p>Частичная выгрузка зданий: название, адрес и сведения о корпусе. Квартиры и цены отсутствуют.</p>}{list && (developerMode ? <p className="registry-visibility-note">В каталоге {number(list.records_total)} записей по текущему запросу. Точки показаны только для корпусов с подтверждённой геопривязкой.</p> : <p className="registry-visibility-note">Показано {number(list.dataset_total ?? 0)} из {number(list.source_total)} записей. Ещё {number(list.archived_total)} скрыто из активного каталога: старые наблюдения или записи о ремонтных работах. Это не подтверждает завершение или снос здания.</p>)}</div>
        <form className="search-form" onSubmit={submit}><label className="search-box"><Search size={19} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={developerMode ? 'ЖК, застройщик, корпус или адрес' : 'Название или адрес'} aria-label="Поиск зданий" /><button className="search-submit" type="submit" aria-label="Найти здания"><ArrowRight size={20} /></button></label><button className={`filter-toggle ${filtersOpen ? 'open' : ''}`} type="button" onClick={() => setFiltersOpen(!filtersOpen)} aria-expanded={filtersOpen} aria-controls={developerMode ? 'developer-filter-panel' : undefined}><SlidersHorizontal size={17} /> {developerMode ? 'Фильтры квартир' : 'Фильтры'} <span>{developerMode ? developerUnitFilterCount(unitDraft) || 'Настроить' : [status, kind, district, handoverYear, handoverQuarter, floorsMin, floorsMax, walkMetro, walkSchool, walkKindergarten, walkMedical].filter(Boolean).length || 'Настроить'}</span><ChevronDown size={16} /></button>
          {developerMode && filtersOpen && <DeveloperFilterPanel draft={unitDraft} onChange={updateUnitDraft} onClear={clear} error={filterError} observedAt={list?.snapshot_at ? snapshotDate(list.snapshot_at) : null} />}
          {!developerMode && filtersOpen && <div className="filters-panel registry-filters-panel">
            <p className="registry-filter-scope">Условия относятся к записи здания по выгрузке на {snapshotDate(list?.snapshot_at)}. Они не подтверждают нынешний этап строительства.</p>
            <div className="filter-field"><label htmlFor="registry-status">Статус по выгрузке</label><select id="registry-status" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">Все статусы</option>{list?.facets?.statuses?.map((facet) => <option key={facet.value} value={facet.value}>{facet.value}</option>)}</select></div>
            <div className="filter-field"><label htmlFor="registry-kind">Тип записи</label><select id="registry-kind" value={kind} onChange={(event) => setKind(event.target.value)}><option value="">Все типы</option>{list?.facets?.kinds?.map((facet) => <option key={facet.value} value={facet.value}>{kindLabel(facet.value)}</option>)}</select></div>
            <div className="filter-field registry-district-field"><label htmlFor="registry-district">Район по картографическим границам</label><select id="registry-district" value={district} onChange={(event) => setDistrict(event.target.value)}><option value="">Все районы, включая неизвестные</option>{list?.facets?.districts?.map((facet) => <option key={facet.value} value={facet.value}>{facet.value} · {number(facet.count)}</option>)}</select></div>
            <div className="filter-field"><label htmlFor="registry-handover-year">Плановый год сдачи</label><select id="registry-handover-year" value={handoverYear} onChange={(event) => setHandoverYear(event.target.value)}><option value="">Любой срок, включая неизвестный</option>{list?.facets?.handover_years?.map((facet) => <option key={facet.value} value={facet.value}>{facet.value} · {number(facet.count)}</option>)}</select></div>
            <div className="filter-field"><label htmlFor="registry-handover-quarter">Плановый квартал</label><select id="registry-handover-quarter" value={handoverQuarter} onChange={(event) => setHandoverQuarter(event.target.value)}><option value="">Любой срок, включая неизвестный</option>{[1, 2, 3, 4].map((quarter) => <option key={quarter} value={quarter}>{quarter} квартал</option>)}</select></div>
            <div className="filter-field registry-floors-field"><span className="field-label">Этажность здания, по выгрузке</span><div className="field-range"><input type="number" min="1" max="300" step="1" placeholder="От" aria-label="Этажность здания от" value={floorsMin} onChange={(event) => setFloorsMin(event.target.value)} /><span>—</span><input type="number" min="1" max="300" step="1" placeholder="До" aria-label="Этажность здания до" value={floorsMax} onChange={(event) => setFloorsMax(event.target.value)} /></div></div>
            <p className="registry-walk-scope">Пешком до объекта, оценочное время по локальному маршруту</p>
            <WalkFilter id="registry-walk-metro" label="Метро" value={walkMetro} onChange={setWalkMetro} />
            <WalkFilter id="registry-walk-school" label="Школа" value={walkSchool} onChange={setWalkSchool} />
            <WalkFilter id="registry-walk-kindergarten" label="Детский сад" value={walkKindergarten} onChange={setWalkKindergarten} />
            <WalkFilter id="registry-walk-medical" label="Клиника" value={walkMedical} onChange={setWalkMedical} />
            {filterError && <p className="registry-filter-error" role="alert">{filterError}</p>}
            <p className="registry-filter-coverage">Район указан для {number(list?.facets?.district_coverage_count ?? 0)} из {number(list?.dataset_total ?? 0)} зданий. Пеший маршрут рассчитан для {number(list?.facets?.walking_origin_count ?? 0)} зданий с подтверждённой точкой. Год сдачи указан у {number(list?.facets?.handover_years?.reduce((count, facet) => count + facet.count, 0) ?? 0)} записей; числовая этажность — у {number(list?.facets?.numeric_floors_count ?? 0)}. Записи с неизвестным значением не проходят соответствующий фильтр.</p>
            <details className="registry-unavailable"><summary>Остальные критерии сейчас без данных</summary><p>В реестре нет квартир и подтверждённых для каждой записи цены, комнат, площади, этажа квартиры, отделки и планировок. Удобства дома, маршруты к парку, аптеке, магазину и остановке, шум и качество воздуха не покрывают весь реестр. Их отсутствие в фильтрах не означает отсутствия у конкретного дома.</p></details>
            <div className="filter-buttons"><button className="primary-button" type="submit">Показать здания <ArrowRight size={16} /></button><button className="text-button" type="button" onClick={clear}>Сбросить</button></div>
          </div>}
        </form>
        {error && <div className="error-card" role="alert"><strong>Не удалось загрузить данные</strong><p>{error}</p><button onClick={() => { if (snapshotStale) window.location.reload(); else { activeAbort.current?.abort(); const controller = new AbortController(); activeAbort.current = controller; void reload(applied, controller.signal, ++requestId.current) } }}><RefreshCw size={15} /> {snapshotStale ? 'Обновить страницу' : 'Повторить'}</button></div>}
        <div className="sidebar-content"><section className="results">{selected && !selectedPoint && <p className="nearby-no-origin">Для этой записи нет подтверждённой точки. Объекты рядом и пешие маршруты не рассчитаны.</p>}<div className="section-heading"><div><span className="section-kicker">{selected ? 'ЗАПИСЬ РЕЕСТРА' : developerMode ? 'ДОМА ИЗ КАТАЛОГА КВАРТИР' : 'КАТАЛОГ ЗДАНИЙ'}</span><h2>{selected ? selected.name : developerMode ? 'Дома с найденными квартирами' : 'Новостройки и корпуса'}</h2></div><span className="count-badge">{selected ? 1 : list ? `${number(list.items.length)} из ${number(list.records_total)}` : '—'}</span></div>{selected ? <div className="registry-selection-note"><span>{developerMode ? 'Текущее наличие и цены не подтверждены' : 'Цены и квартиры не представлены'}</span>{developerMode && <button type="button" className="developer-explorer-launch" data-testid="open-developer-explorer" onClick={(event) => openDeveloperExplorer(selected, event.currentTarget)}>Этажи и квартиры</button>}{!developerMode && <a href={`/models?decision=&registry_id=${encodeURIComponent(selected.registry_id)}`} onClick={(event) => { if (onModels && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) { event.preventDefault(); onModels(selected.registry_id) } }}>Проверить модели</a>}<button onClick={closeSelection} aria-label="Показать все здания"><X size={16} /></button></div> : points && <p className="result-count-note">На карте {number(points.counts.points)} записей с координатами{unlocated !== null ? `; ${number(unlocated)} записей реестра без подтверждённой точки` : ''}. {developerMode ? 'Точки относятся только к подтверждённым корпусам из этого снимка.' : 'Картографические стройки не подтверждают связь с ЖК.'}</p>}{loading ? <div className="loading-list" role="status">Загружаем здания…<span className="loading-bar" /></div> : shown.length ? <div className="registry-list">{shown.map((item) => <BuildingCard key={item.registry_id} item={item} selected={selected?.registry_id === item.registry_id} onSelect={() => select(item)} developerMode={developerMode} />)}{!selected && list && list.items.length < list.records_total && <button className="registry-more" onClick={() => void loadMore()} disabled={loadingMore}>{loadingMore ? 'Загружаем…' : 'Показать ещё'}</button>}</div> : <div className="empty-state"><Compass size={29} /><strong>Здания в реестре не найдены</strong><p>Измените запрос или фильтры. {developerMode ? 'На карте нет других корпусов для этого запроса.' : 'Картографические стройки не подтверждают связь с ЖК.'}</p><button className="primary-button" onClick={clear}>Сбросить фильтры</button></div>}</section></div>
      </aside>
      <main className={`map-column ${mobilePanel === 'results' ? 'mobile-hide' : ''}${developerMode ? ' developer-map-mode' : ''}${selectedPoint || selectedGroup ? ' has-map-selection' : ''}`}>
        <MapView data={mapData} selectedId={selectedMarkerId} selectionRequestId={selectionRequestId} focusOnSelection={focusOnSelection} visible={mapVisible} selectedModel={selectedMarkerId && selectedModel?.registry_id === modelRegistryId ? selectedModel : null} onSelectedModelVisible={setSelectedModelVisible}
          markerLabel={points ? `${number(points.counts.points)} записей с координатами${unlocated !== null ? ` · ${number(unlocated)} записей реестра без точки` : ''}${!developerMode && points.counts.excluded_mapped_by_registry_filters ? ` · ${number(points.counts.excluded_mapped_by_registry_filters)} картографических строек исключено фильтром статуса/типа` : ''}` : 'Точки с подтверждёнными координатами'}
          nearbyItems={nearbyItems} selectedPoiId={selectedPoiId} onSelectNearby={setSelectedPoiId}
          onMarkerScale={(height) => setExpandGroupsNear((current) => expandGroupsForCameraHeight(current, height))}
          onViewport={(bbox) => setViewportBbox((current) => { const next = boundedNearbyBbox(bbox); return JSON.stringify(current) === JSON.stringify(next) ? current : next })}
          onSelect={selectMarker} onStatus={setMapStatus} />
        <NearbyPanel selectedName={selectedPoint?.name ?? selected?.name ?? null} selectedHasPoint={Boolean(selectedPoint)}
          visible={nearbyVisibility} onToggle={toggleNearby} education={nearbyEducation}
          onEducation={(value) => { setNearbyEducation(value); setSelectedPoiId(null) }} data={nearbyData}
          viewportAvailable={Boolean(viewportBbox)} viewportCount={viewportData?.returned_count ?? null}
          viewportTruncated={viewportData?.truncated ?? false} viewportLoading={viewportLoading} viewportError={viewportError}
          selectedViewportPoiName={!nearbyPointId ? nearbyItems.find(({ item }) => item.id === selectedPoiId)?.item.name ?? null : null}
          loading={nearbyLoading} error={nearbyError} selectedPoiId={selectedPoiId} onSelectPoi={setSelectedPoiId} />
        {selectedGroup && !selectedPoint && <div className="map-selection map-point-card"><div className="map-point-card-head"><strong>{selectedGroup.group_label} · {number(selectedGroup.member_count)} записей</strong><button type="button" onClick={closeSelection} aria-label="Закрыть группу ЖК"><X size={15} /></button></div><p>{developerMode ? 'Группа ЖК по подтверждённому общему идентификатору. Каждый корпус остаётся отдельной записью.' : 'Группа по подтверждённому общему идентификатору. Каждая стройка остаётся отдельной записью; выберите точку на карте или ниже.'}</p><div className="map-point-card-items">{selectedGroup.members.slice(0, 12).map((point) => <div className="map-point-card-item" key={point.point_id}><span><strong>{point.name}</strong><small>{pointAddressCaption(point)}</small>{pointDeveloperCardCaption(point) && <small>{pointDeveloperCardCaption(point)}</small>}</span><button type="button" onClick={() => selectMarker(point.point_id)}>Показать точку</button></div>)}</div>{selectedGroup.member_count > 12 && <p>На карте показаны ещё {number(selectedGroup.member_count - 12)} записей.</p>}</div>}
        {selectedPoint && <div className="map-selection map-point-card"><div className="map-point-card-head"><strong>{coincident.length > 1 ? `${coincident.length} записи в одной точке` : selectedPoint.name}</strong><button type="button" onClick={closeSelection} aria-label="Закрыть сведения о точке"><X size={15} /></button></div>
          <div className="map-point-card-items">{coincident.map((point) => <div className="map-point-card-item" key={point.point_id}>
            <span><b>{point.point_type === 'developer_building' ? 'Корпус из каталога квартир' : point.point_type === 'registry' ? 'Запись реестра' : 'Строящийся объект на карте'}</b>{coincident.length > 1 && <strong>{point.name}</strong>}<small>{pointAddressCaption(point)}</small><small className="map-point-complex">{pointComplexCardCaption(point, selectedPointGroup)}</small>{pointDeveloperCardCaption(point) && <small>{pointDeveloperCardCaption(point)}</small>}{pointRegistryLinkCaption(point, selectedPointGroup) && <small>{pointRegistryLinkCaption(point, selectedPointGroup)}</small>}<small>{point.point_type === 'developer_building' ? `Сведения: ${snapshotDate(point.observed_at)}` : point.point_type === 'registry' ? `Сведения: ${snapshotDate(point.latest_known_date)}` : `Контур: ${snapshotDate(point.geometry_as_of)}`}</small>{selectedModelVisible && selectedModel?.registry_id === point.registry_id && <small className="selected-model-caption">Реконструкция: {selectedModel.photo_facade_reconstruction ? 'фасад по фото без подтверждённой ориентации' : 'схематический объём по контуру и этажности'}; высота оценочная.</small>}</span>
            {point.point_type !== 'mapped_construction' ? <button type="button" onClick={(event) => openRegistryPoint(point, point.point_type === 'developer_building', event.currentTarget)}>{point.point_type === 'developer_building' ? 'Этажи и квартиры' : 'Сведения'}</button>
              : <a href={`/models?decision=ready&candidate=${encodeURIComponent(point.candidate_id)}`} onClick={(event) => { if (onModels && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) { event.preventDefault(); onModels(undefined, point.candidate_id) } }}>Оценка модели</a>}
          </div>)}</div>
          {coincident.length > 1 && <p>Совпадение координат не подтверждает, что записи относятся к одному корпусу.</p>}
        </div>}
        <div className="map-bottom-note">{mapStatus && <span className="map-error">{mapStatus}</span>}<span>{[...new Set([...(map?.tilesets.map((item) => item.attribution).filter(Boolean) ?? []), ...(points?.counts.mapped_construction_points ? [points.mapped_attribution] : []), nearbyData?.attribution, viewportData?.attribution].filter((value): value is string => Boolean(value)))].join(' · ') || 'Схематичная визуализация зданий'}</span><span>Снимок: {snapshotDate(points?.checked_at ?? map?.snapshot_at ?? health.snapshot_at)}</span></div>
      </main>
    </div>
    {view !== 'data' && <div className="mobile-switch" role="tablist" aria-label="Вид"><button role="tab" aria-selected={mobilePanel === 'results'} className={mobilePanel === 'results' ? 'active' : ''} onClick={() => setMobilePanel('results')}><Building2 size={17} /> Здания</button><button role="tab" aria-selected={mobilePanel === 'map'} className={mobilePanel === 'map' ? 'active' : ''} onClick={() => setMobilePanel('map')}><MapPin size={17} /> Карта</button></div>}
    {developerMode && active && view === 'catalog' && explorerBuilding && <DeveloperExplorer key={`${health.registry_snapshot_id}:${explorerBuilding.registry_id}`} building={explorerBuilding} registrySnapshotId={health.registry_snapshot_id ?? ''} mapRunId={health.map_run_id ?? ''} unitFilters={selectedDeveloperUnitCriteria(applied)} filterSummary={activeUnitFilterSummary} onClose={closeExplorer} />}
  </div>
}
