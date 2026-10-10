import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRight, BadgeCheck, Building2, Check, ChevronDown, Compass, GitCompareArrows, Heart, MapPin, RefreshCw, Search, SlidersHorizontal, Sparkles, X } from 'lucide-react'
import MapView from './MapView'
import AdvancedFilters, { criterionLabel } from './AdvancedFilters'
import DataDashboard from './DataDashboard'
import RegistryApp from './RegistryApp'
import ModelsPage from './ModelsPage'
import useMobileViewport from './useMobileViewport'
import FactView, { FactValue } from './FactView'
import { api, setApiSnapshot, type Complex, type Criterion, type District, type Filters, type Health, type MapData, type Offer, type Query, type SearchResponse } from './api'

type View = 'catalog' | 'favorites' | 'compare' | 'data'
const SHOW_COMPARISON = false
const money = (amount: number) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(amount / 1_000_000)
const area = (amount: number) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(amount)
const date = (value: string) => {
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? 'дата не указана' : new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(parsed)
}
const quality = (offer: Offer) => offer.source.quality === 'illustrative' ? 'Учебные данные' : offer.source.quality === 'dated_asking' ? 'Цена из источника' : 'Источник не подтверждён'
const assertNoDemoOffers = (mode: string, offers: Offer[]) => {
  if (mode === 'real' && offers.some((offer) => offer.source.record_mode === 'demo' || offer.source.quality === 'illustrative')) throw new Error('Каталог содержит учебные предложения в реальном режиме. Показ данных остановлен.')
}
const roomLabel = (rooms: number | null) => rooms === null ? 'Комнатность не указана' : rooms === 0 ? 'Студия' : `${rooms}-комн.`
const DEFAULT_FACT_KEYS = ['offer.price_per_sqm_rub', 'unit.floor']
const storage = {
  read: (): string[] => { try { const value = JSON.parse(localStorage.getItem('novo:favorites:v1') ?? '[]'); return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [] } catch { return [] } },
  write: (ids: string[]) => { try { localStorage.setItem('novo:favorites:v1', JSON.stringify(ids)) } catch { /* private browsing */ } },
}

function OfferCard({ offer, complex, district, selected, favorite, compared, onSelect, onFavorite, onCompare, rank, criteria, activeFactKeys }: {
  offer: Offer; complex?: Complex; district?: District; selected: boolean; favorite: boolean; compared: boolean
  onSelect: () => void; onFavorite: () => void; onCompare: () => void; rank?: number; criteria: Criterion[]; activeFactKeys: string[]
}) {
  return <article className={`offer-card ${selected ? 'selected' : ''}`} data-testid={`offer-${offer.offer_id}`} onClick={onSelect}>
    <div className="offer-card-top"><div className="offer-art" aria-hidden="true"><span className="art-building art-back" /><span className="art-building art-front" /><span className="art-line" /></div><div className="offer-main">
      <div className="offer-overline"><span>{rank ? `Выбор №${rank}` : district?.name ?? 'Москва'}</span><span className="source-pill">{quality(offer)}</span></div>
      <h3>{complex?.name ?? 'Жилой комплекс'}</h3>
      <div className="offer-location"><MapPin size={13} />{district?.name ?? 'Москва'}{offer.floor !== null && offer.floor !== undefined ? ` · ${offer.floor} этаж` : ''}</div>
    </div></div>
    <div className="offer-divider" />
    <div className="offer-facts"><span><strong>{roomLabel(offer.rooms)}</strong><small>планировка</small></span><span><strong>{offer.area_sqm === null ? '—' : `${area(offer.area_sqm)} м²`}</strong><small>площадь</small></span><span className="price-fact"><strong>{offer.price ? `${money(offer.price.amount_rub)} млн ₽` : 'Цена не указана'}</strong><small>{offer.price?.basis === 'conditional_total' ? 'при условиях' : 'полная цена'}</small></span></div>
    {offer.recommendation?.explanation?.length ? <p className="card-reason"><Sparkles size={13} />{offer.recommendation.explanation.slice(0, 2).join(' · ')}</p> : null}
    <FactView facts={offer.facts} criteria={criteria} keys={[...new Set([...activeFactKeys, ...DEFAULT_FACT_KEYS])]} limit={Math.max(2, Math.min(activeFactKeys.length + 2, 5))} />
    <div className="offer-footer"><span>Цена: {offer.price ? date(offer.price.observed_at) : 'нет даты'} · {offer.availability === 'available' ? 'наличие подтверждено' : 'наличие не подтверждено'}</span><div className="offer-actions"><button className={favorite ? 'icon-action active' : 'icon-action'} aria-label={favorite ? 'Убрать из избранного' : 'В избранное'} aria-pressed={favorite} title="Избранное" onClick={(event) => { event.stopPropagation(); onFavorite() }}><Heart size={17} fill={favorite ? 'currentColor' : 'none'} /></button>{SHOW_COMPARISON && <button className={compared ? 'icon-action active' : 'icon-action'} aria-label={compared ? 'Убрать из сравнения' : 'Добавить к сравнению'} aria-pressed={compared} title="Сравнить" onClick={(event) => { event.stopPropagation(); onCompare() }}><GitCompareArrows size={17} /></button>}</div></div>
  </article>
}

function LegacyApp({ initialHealth, active = true, routeView = 'catalog', onCatalog, onData, onModels }: { initialHealth: Health; active?: boolean; routeView?: 'catalog' | 'data'; onCatalog?: () => void; onData?: () => void; onModels?: () => void }) {
  const mobile = useMobileViewport()
  const [health, setHealth] = useState<Health | null>(null)
  const [mapData, setMapData] = useState<MapData | null>(null)
  const [districts, setDistricts] = useState<District[]>([])
  const [criteria, setCriteria] = useState<Criterion[]>([])
  const [searchResult, setSearchResult] = useState<SearchResponse | null>(null)
  const [selectedOffers, setSelectedOffers] = useState<Offer[] | null>(null)
  const [selectedLoading, setSelectedLoading] = useState(false)
  const [recommendations, setRecommendations] = useState<Offer[]>([])
  const [query, setQuery] = useState('')
  const [filters, setFilters] = useState<Filters>({ limit: 100 })
  const [view, setView] = useState<View>(routeView)
  useEffect(() => setView(routeView), [routeView])
  const showCatalog = () => { setView('catalog'); setSelectedComplexId(null); onCatalog?.() }
  const showData = () => { setView('data'); onData?.() }
  const [selectedComplexId, setSelectedComplexId] = useState<string | null>(null)
  const [favoriteIds, setFavoriteIds] = useState<string[]>(storage.read)
  const [favoriteOffers, setFavoriteOffers] = useState<Offer[]>([])
  const [compareIds, setCompareIds] = useState<string[]>([])
  const [compareOffers, setCompareOffers] = useState<Offer[]>([])
  const [loading, setLoading] = useState(true)
  const [searching, setSearching] = useState(false)
  const [comparing, setComparing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [mapStatus, setMapStatus] = useState<string | null>(null)
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [mobilePanel, setMobilePanel] = useState<'results' | 'map'>('results')
  const [toast, setToast] = useState<string | null>(null)
  const [submitted, setSubmitted] = useState<Query>({ filters: { limit: 100 } })
  const requestId = useRef(0)
  const searchAbort = useRef<AbortController | null>(null)
  const selectionRequestId = useRef(0)
  const [cameraSelectionId, setCameraSelectionId] = useState(0)
  const sidebarRef = useRef<HTMLElement>(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const [m, d, registry] = await Promise.all([api.map(), api.districts(), api.criteria()])
      const h = initialHealth
      if (new Set([h.snapshot_id, m.snapshot_id, d.snapshot_id]).size > 1 || new Set([h.data_mode, m.data_mode, d.data_mode]).size > 1) throw new Error('Данные каталога обновляются. Повторите загрузку через минуту.')
      if (registry.version !== '1.1' || registry.snapshot_id !== h.snapshot_id || registry.data_mode !== h.data_mode) throw new Error('Реестр критериев относится к другому снимку данных. Повторите загрузку.')
      setApiSnapshot(h.snapshot_id)
      setHealth(h); setMapData(m); setDistricts(d.items)
      setCriteria(registry.criteria)
      void api.event('map_view').catch(() => undefined)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Не удалось открыть каталог') }
    finally { setLoading(false) }
  }, [initialHealth])
  useEffect(() => { void load() }, [load])

  const runSearch = useCallback(async (body: Query) => {
    searchAbort.current?.abort()
    const controller = new AbortController()
    searchAbort.current = controller
    const id = ++requestId.current
    ++selectionRequestId.current
    setSelectedComplexId(null); setSelectedOffers(null)
    setSearching(true); setError(null); setSubmitted(body)
    try {
      const [results, recs] = await Promise.all([api.search(body, controller.signal), api.recommendations(body, controller.signal)])
      if (id !== requestId.current) return
      if (results.snapshot_id !== recs.snapshot_id || (health && results.snapshot_id !== health.snapshot_id)) throw new Error('Каталог обновился. Повторите поиск.')
      assertNoDemoOffers(results.data_mode, results.items)
      assertNoDemoOffers(recs.data_mode, recs.items)
      const mapped = results.clarification ? null : await api.map(results.applied_filters)
      if (id !== requestId.current) return
      if (mapped && (mapped.snapshot_id !== results.snapshot_id || mapped.data_mode !== results.data_mode)) throw new Error('Данные карты обновились. Повторите поиск.')
      if (mapped && mapped.counts.offers !== results.count) throw new Error('Число квартир на карте и в поиске не совпадает. Повторите поиск.')
      setSearchResult(results); setRecommendations(recs.items.slice(0, 3))
      if (recs.items.length > 0) void api.event('recommendation_view').catch(() => undefined)
      if (mapped) setMapData(mapped)
      else setMapData((current) => current ? { ...current, complexes: [], tilesets: current.tilesets.filter((tile) => tile.kind !== 'facade'), counts: { ...current.counts, complexes: 0, offers: 0 } } : null)
      void api.event('search').catch(() => undefined)
    } catch (reason) { if (id === requestId.current && !controller.signal.aborted) { setError(reason instanceof Error ? reason.message : 'Поиск не выполнен'); setSearchResult(null); setRecommendations([]); setMapData((current) => current ? { ...current, complexes: [], tilesets: current.tilesets.filter((tile) => tile.kind !== 'facade'), counts: { ...current.counts, complexes: 0, offers: 0 } } : null) } }
    finally { if (id === requestId.current) setSearching(false) }
  }, [health])
  useEffect(() => () => searchAbort.current?.abort(), [])
  useEffect(() => { if (health) void runSearch({ filters: { limit: 100 } }) }, [health, runSearch])
  useEffect(() => { storage.write(favoriteIds) }, [favoriteIds])
  useEffect(() => {
    if (view !== 'favorites') return
    if (favoriteIds.length === 0) { setFavoriteOffers([]); return }
    let cancelled = false
    void Promise.allSettled(favoriteIds.map((id) => api.offer(id))).then((results) => {
      if (cancelled) return
      const offers = results.filter((result): result is PromiseFulfilledResult<Offer> => result.status === 'fulfilled').map((result) => result.value)
      try { assertNoDemoOffers(health?.data_mode ?? 'demo', offers); setFavoriteOffers(offers) } catch (reason) { setError(reason instanceof Error ? reason.message : 'Данные избранного недоступны'); setFavoriteOffers([]) }
      if (results.some((result) => result.status === 'rejected')) setToast('Часть сохранённых квартир сейчас недоступна')
    })
    return () => { cancelled = true }
  }, [view, favoriteIds, health])
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(null), 3500); return () => clearTimeout(timer) }, [toast])

  const complexById = useMemo(() => new Map(mapData?.complexes.map((item) => [item.complex_id, item]) ?? []), [mapData])
  const districtById = useMemo(() => new Map(districts.map((item) => [item.district_id, item])), [districts])
  const activeFactKeys = [...new Set((searchResult?.applied_filters.predicates ?? submitted.filters?.predicates ?? []).map((item) => item.key))]
  const criteriaByKey = useMemo(() => new Map(criteria.map((item) => [item.key, item])), [criteria])
  const compareFactKeys = [...new Set(compareOffers.flatMap((offer) => Object.keys(offer.facts ?? {})))].slice(0, 8)
  const activeFilterCount = Object.entries(filters).reduce((count, [key, value]) => {
    if (key === 'limit' || value === undefined || value === null) return count
    if (Array.isArray(value)) return count + (key === 'predicates' ? value.length : value.length ? 1 : 0)
    return count + 1
  }, 0)
  const allOffers = searchResult?.items ?? []
  const shownOffers = view === 'favorites' ? favoriteOffers.filter((item) => favoriteIds.includes(item.offer_id)) : selectedComplexId && view === 'catalog' ? (selectedOffers ?? []) : allOffers
  const selectedComplex = selectedComplexId ? complexById.get(selectedComplexId) : undefined
  const isDemo = health?.data_mode === 'demo' || mapData?.data_mode === 'demo' || searchResult?.data_mode === 'demo'
  const toggleFavorite = (id: string) => setFavoriteIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])
  const toggleCompare = (id: string) => setCompareIds((current) => {
    if (current.includes(id)) return current.filter((item) => item !== id)
    if (current.length >= 4) { setToast('Можно сравнить до 4 квартир'); return current }
    return [...current, id]
  })
  const selectComplex = (id: string, fromMap = false) => {
    const selectionId = ++selectionRequestId.current
    setCameraSelectionId((value) => value + 1)
    setView('catalog'); setSelectedComplexId(id); setSelectedOffers(null); setSelectedLoading(true)
    if (!fromMap) setMobilePanel('results')
    sidebarRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
    const active = searchResult?.applied_filters ?? submitted.filters ?? {}
    void api.search({ filters: { ...active, complex_ids: [id], limit: 100 } }).then((result) => {
      if (selectionId !== selectionRequestId.current) return
      if (health && result.snapshot_id !== health.snapshot_id) throw new Error('Каталог обновился. Повторите поиск.')
      assertNoDemoOffers(result.data_mode, result.items)
      setSelectedOffers(result.items)
      void api.event('offer_view').catch(() => undefined)
    }).catch((reason) => { if (selectionId === selectionRequestId.current) setError(reason instanceof Error ? reason.message : 'Не удалось открыть ЖК') }).finally(() => { if (selectionId === selectionRequestId.current) setSelectedLoading(false) })
  }
  const submit = (event?: React.FormEvent) => {
    event?.preventDefault(); setSelectedComplexId(null); setView('catalog'); setMobilePanel('results')
    void runSearch({ query: query.trim() || undefined, filters })
  }
  const clear = () => { setQuery(''); setFilters({ limit: 100 }); setSelectedComplexId(null); void runSearch({ filters: { limit: 100 } }) }
  const compare = async () => {
    if (compareIds.length < 2) { setToast('Выберите от 2 до 4 квартир для сравнения'); return }
    setView('compare'); setComparing(true); setError(null)
    try { const result = await api.compare(compareIds); assertNoDemoOffers(result.data_mode, result.items); setCompareOffers(result.items); void api.event('compare').catch(() => undefined) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Не удалось сравнить квартиры') }
    finally { setComparing(false) }
  }
  const resultHeading = view === 'favorites' ? 'Избранные квартиры' : selectedComplex ? selectedComplex.name : 'Подходящие квартиры'

  return <div className="app-shell">
    <header className="site-header"><a className="brand" href="/" onClick={(event) => { event.preventDefault(); showCatalog() }}><span className="brand-mark"><Building2 size={19} strokeWidth={2.4} /></span><span>ново<span className="brand-period">.</span></span></a><span className="brand-descriptor">Квартиры в новых домах Москвы</span><nav className="main-nav" aria-label="Основная навигация"><a href="/" className={view === 'catalog' ? 'nav-link active' : 'nav-link'} aria-current={view === 'catalog' ? 'page' : undefined} onClick={(event) => { event.preventDefault(); showCatalog() }}>Каталог</a><a className="nav-link" href="/models?decision=rejected" onClick={(event) => { if (onModels && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) { event.preventDefault(); onModels() } }}>Модели</a><button className={view === 'favorites' ? 'nav-link active' : 'nav-link'} onClick={() => setView('favorites')}>Избранное {favoriteIds.length > 0 && <em>{favoriteIds.length}</em>}</button><a href="/data" className={view === 'data' ? 'nav-link active' : 'nav-link'} aria-current={view === 'data' ? 'page' : undefined} onClick={(event) => { event.preventDefault(); showData() }}>Данные</a>{SHOW_COMPARISON && <button className={view === 'compare' ? 'nav-link active' : 'nav-link'} onClick={() => { if (compareIds.length >= 2) void compare(); else { setView('compare'); setCompareOffers([]) } }}>Сравнение {compareIds.length > 0 && <em>{compareIds.length}</em>}</button>}</nav><div className="header-location"><MapPin size={15} /> Москва</div></header>
    {isDemo && <div className="demo-banner" role="status"><BadgeCheck size={16} /><strong>Демо-каталог</strong><span>Все ЖК, квартиры, цены и 3D-объёмы здесь иллюстративны. Их нельзя использовать для покупки или оценки рынка.</span></div>}
    {view === 'data' && <DataDashboard snapshotId={health?.snapshot_id} criteria={criteria} onCatalog={showCatalog} />}
    <div className="app-grid" hidden={view === 'data'} inert={view === 'data'} aria-hidden={view === 'data'}><aside ref={sidebarRef} className={`sidebar ${mobilePanel === 'map' ? 'mobile-hide' : ''}`}>
      <div className="sidebar-intro"><div className="eyebrow"><span className="eyebrow-line" /> МОСКВА · НОВОСТРОЙКИ</div><h1>Найдите квартиру,<br /><i>которая подходит.</i></h1><p>Районы, цены и новые дома — прямо на карте.</p></div>
      <form className="search-form" onSubmit={submit}><label className="search-box"><Search size={19} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Например, двушка до 20 млн в Хамовниках" aria-label="Поиск квартир" /><button className="search-submit" type="submit" aria-label="Найти квартиры"><ArrowRight size={20} /></button></label><button className={`filter-toggle ${filtersOpen ? 'open' : ''}`} type="button" onClick={() => setFiltersOpen(!filtersOpen)}><SlidersHorizontal size={17} /> Фильтры <span>{activeFilterCount || 'Настроить'}</span><ChevronDown size={16} /></button>
        {filtersOpen && <div className="filters-panel"><div className="filter-field"><label htmlFor="district">Район</label><select id="district" value={filters.district_ids?.[0] ?? ''} onChange={(event) => setFilters((current) => ({ ...current, district_ids: event.target.value ? [event.target.value] : undefined }))}><option value="">Все районы</option>{districts.map((district) => <option key={district.district_id} value={district.district_id}>{district.name}</option>)}</select></div><div className="filter-field"><label htmlFor="budget">Бюджет до, млн ₽</label><input id="budget" type="number" min="1" step="0.5" placeholder="Без ограничений" value={filters.budget_max_rub === undefined ? '' : filters.budget_max_rub / 1_000_000} onChange={(event) => setFilters((current) => ({ ...current, budget_max_rub: event.target.value ? Math.round(Number(event.target.value) * 1_000_000) : undefined }))} /></div><div className="filter-field"><span className="field-label">Комнат</span><div className="room-choices">{[0, 1, 2, 3, 4].map((rooms) => <button key={rooms} className={filters.rooms?.includes(rooms) ? 'chosen' : ''} type="button" aria-pressed={filters.rooms?.includes(rooms) ?? false} onClick={() => setFilters((current) => ({ ...current, rooms: current.rooms?.includes(rooms) ? (current.rooms.length === 1 ? undefined : current.rooms.filter((item) => item !== rooms)) : [...(current.rooms ?? []), rooms] }))}>{rooms === 0 ? 'Ст' : rooms === 4 ? '4+' : rooms}</button>)}</div></div><div className="filter-field"><span className="field-label">Площадь, м²</span><div className="field-range"><input type="number" min="1" placeholder="От" aria-label="Площадь от" value={filters.area_min_sqm ?? ''} onChange={(event) => setFilters((current) => ({ ...current, area_min_sqm: event.target.value ? Number(event.target.value) : undefined }))} /><span>—</span><input type="number" min="1" placeholder="До" aria-label="Площадь до" value={filters.area_max_sqm ?? ''} onChange={(event) => setFilters((current) => ({ ...current, area_max_sqm: event.target.value ? Number(event.target.value) : undefined }))} /></div></div><div className="filter-field"><label htmlFor="price-basis">Тип цены</label><select id="price-basis" value={filters.price_basis ?? ''} onChange={(event) => setFilters((current) => ({ ...current, price_basis: event.target.value ? event.target.value as Filters['price_basis'] : undefined }))}><option value="">Все типы</option><option value="asking_total">Полная цена</option><option value="conditional_total">При условиях</option></select></div><AdvancedFilters criteria={criteria} predicates={filters.predicates ?? []} onChange={(predicates) => setFilters((current) => ({ ...current, predicates: predicates.length ? predicates : undefined }))} /><div className="filter-buttons"><button className="primary-button" type="submit">Показать квартиры <ArrowRight size={16} /></button><button className="text-button" type="button" onClick={clear}>Сбросить</button></div></div>}
      </form>
      {searchResult?.clarification && <div className="clarification" role="status"><strong>Уточните запрос</strong><p>{searchResult.clarification.message}</p><button onClick={() => { setFiltersOpen(true); setQuery("") }}>Выбрать район</button>{searchResult.clarification.options?.map((option) => option.district_id && <button key={option.district_id} onClick={() => { const next = { ...filters, district_ids: [option.district_id!] }; setFilters(next); setQuery(""); void runSearch({ filters: next }) }}>{option.name ?? option.label ?? option.district_id}</button>)}</div>}
      {error && <div className="error-card" role="alert"><strong>Не удалось загрузить данные</strong><p>{error}</p><button onClick={() => health ? void runSearch(submitted) : void load()}><RefreshCw size={15} /> Повторить</button></div>}
      <div className="sidebar-content">
        {view === 'compare' ? <div className="comparison"><div className="section-heading"><div><span className="section-kicker">ВАШ ВЫБОР</span><h2>Сравнение</h2></div><span className="count-badge">{compareIds.length}/4</span></div>{compareIds.length < 2 ? <div className="empty-state"><GitCompareArrows size={28} /><strong>Добавьте две квартиры</strong><p>Нажмите значок сравнения на карточках. Можно выбрать от 2 до 4 вариантов.</p><button className="primary-button" onClick={() => setView('catalog')}>Смотреть квартиры</button></div> : <><div className="compare-summary">{compareIds.length} варианта рядом — цена, площадь и источник данных.</div><button className="primary-button wide" onClick={() => void compare()} disabled={comparing}>{comparing ? 'Сравниваем…' : 'Обновить сравнение'}</button><div className="compare-scroll"><table className="compare-table"><thead><tr><th>Параметр</th>{compareOffers.map((offer) => <th key={offer.offer_id}>{complexById.get(offer.complex_id)?.name ?? offer.offer_id}</th>)}</tr></thead><tbody>{[['Цена, млн ₽', (offer: Offer) => offer.price ? money(offer.price.amount_rub) : '—'], ['Цена', (offer: Offer) => offer.price?.basis === 'conditional_total' ? 'При условиях' : 'Полная'], ['Площадь, м²', (offer: Offer) => offer.area_sqm === null ? '—' : area(offer.area_sqm)], ['Комнат', (offer: Offer) => roomLabel(offer.rooms)], ['Район', (offer: Offer) => districtById.get(offer.district_id ?? '')?.name ?? '—'], ['Дата цены', (offer: Offer) => offer.price ? date(offer.price.observed_at) : '—'], ['Источник', (offer: Offer) => quality(offer)]].map(([label, value]) => <tr key={label as string}><th>{label as string}</th>{compareOffers.map((offer) => <td key={offer.offer_id}>{(value as (item: Offer) => string)(offer)}</td>)}</tr>)}{compareFactKeys.map((key) => <tr key={key}><th>{criteriaByKey.has(key) ? criterionLabel(criteriaByKey.get(key)!) : key}</th>{compareOffers.map((offer) => <td key={offer.offer_id}><FactValue fact={offer.facts?.[key]} criterion={criteriaByKey.get(key)} /></td>)}</tr>)}</tbody></table></div></> }</div> : <>
          {view === 'catalog' && !selectedComplexId && recommendations.length > 0 && <section className="recommendations"><div className="section-heading"><div><span className="section-kicker"><Sparkles size={13} /> ПОДОБРАНО ДЛЯ ВАС</span><h2>Стоит посмотреть</h2></div><span className="count-badge">топ 3</span></div><div className="rec-list">{recommendations.map((offer, index) => <button key={offer.offer_id} className="rec-row" onClick={() => selectComplex(offer.complex_id)}><span className="rec-num">0{index + 1}</span><span className="rec-text"><strong>{complexById.get(offer.complex_id)?.name ?? 'Жилой комплекс'}</strong><small>{offer.recommendation?.explanation?.slice(0, 1).join('') ?? `${roomLabel(offer.rooms)} · ${offer.area_sqm ?? '—'} м²`}</small></span><span className="rec-price">{offer.price ? `${money(offer.price.amount_rub)} млн` : '—'}<ArrowRight size={15} /></span></button>)}</div></section>}
          <section className="results"><div className="section-heading"><div><span className="section-kicker">{selectedComplex ? 'НА КАРТЕ' : view === 'favorites' ? 'СОХРАНЕНО' : 'КАТАЛОГ КВАРТИР'}</span><h2>{resultHeading}</h2></div><span className="count-badge">{view === "catalog" && !selectedComplex && searchResult && searchResult.count > shownOffers.length ? `${shownOffers.length} из ${searchResult.count}` : shownOffers.length}</span></div>{view === "catalog" && !selectedComplex && searchResult && searchResult.count > shownOffers.length && <p className="result-count-note">Показано {shownOffers.length} из {searchResult.count} совпадений. Уточните фильтры или выберите ЖК на карте, чтобы увидеть остальные.</p>}{selectedComplex && <div className="complex-note"><span>{districtById.get(selectedComplex.district_id ?? '')?.name ?? 'Москва'}</span><span>·</span><span>{selectedComplex.model_quality === 'licensed_facade' ? 'Лицензированная 3D-модель' : selectedComplex.model_quality === 'synthetic_facade' ? 'Схематичный 3D-фасад' : 'Схематичная 3D-модель'}</span><button onClick={() => { ++selectionRequestId.current; setSelectedComplexId(null); setSelectedOffers(null) }} aria-label="Показать все ЖК"><X size={16} /></button></div>}{searching || loading || selectedLoading ? <div className="loading-list" role="status">Загружаем варианты…<span className="loading-bar" /></div> : shownOffers.length ? <div className="offer-list">{shownOffers.map((offer) => <div key={offer.offer_id} ><OfferCard offer={offer} criteria={criteria} activeFactKeys={activeFactKeys} complex={complexById.get(offer.complex_id)} district={districtById.get(offer.district_id ?? '')} selected={selectedComplexId === offer.complex_id} favorite={favoriteIds.includes(offer.offer_id)} compared={compareIds.includes(offer.offer_id)} onSelect={() => selectComplex(offer.complex_id)} onFavorite={() => toggleFavorite(offer.offer_id)} onCompare={() => toggleCompare(offer.offer_id)} /></div>)}</div> : <div className="empty-state"><Compass size={29} /><strong>{view === 'favorites' ? 'Пока ничего не сохранено' : selectedComplex ? 'По вашим фильтрам здесь нет квартир' : 'Квартиры не найдены'}</strong><p>{view === 'favorites' ? 'Сохраняйте понравившиеся варианты значком сердца.' : 'Попробуйте изменить бюджет, район или площадь.'}</p><button className="primary-button" onClick={view === 'favorites' ? () => setView('catalog') : clear}>{view === 'favorites' ? 'Открыть каталог' : 'Сбросить фильтры'}</button></div>}</section>
        </>}
      </div>
    </aside><main className={`map-column ${mobilePanel === 'results' ? 'mobile-hide' : ''}`}><MapView data={mapData} selectedId={selectedComplexId} selectionRequestId={cameraSelectionId} visible={active && view !== 'data' && (!mobile || mobilePanel === 'map')} onSelect={(id) => selectComplex(id, true)} onStatus={setMapStatus} />{selectedComplex && <div className="map-selection"><div><strong>{selectedComplex.name}</strong><span>{selectedLoading ? "Загружаем квартиры…" : `${selectedOffers?.length ?? selectedComplex.offer_count ?? 0} квартир · ${selectedComplex.model_quality === "synthetic_facade" ? "схематичный 3D-фасад" : "3D-модель"}`}</span></div><button onClick={() => { setMobilePanel("results"); sidebarRef.current?.scrollTo({ top: 0, behavior: "smooth" }) }}>Квартиры <ArrowRight size={15} /></button></div>}<div className="map-bottom-note">{mapStatus && <span className="map-error">{mapStatus}</span>}<span>{[...new Set(mapData?.tilesets.map((item) => item.attribution).filter(Boolean) ?? [])].join(' · ') || 'Схематичная визуализация зданий'}</span><span>Данные: {health ? date(health.snapshot_at) : 'загрузка'}</span></div></main></div>
    {view !== 'data' && <div className="mobile-switch" role="tablist" aria-label="Вид"><button role="tab" aria-selected={mobilePanel === 'results'} className={mobilePanel === 'results' ? 'active' : ''} onClick={() => setMobilePanel('results')}><Building2 size={17} /> Квартиры</button><button role="tab" aria-selected={mobilePanel === 'map'} className={mobilePanel === 'map' ? 'active' : ''} onClick={() => setMobilePanel('map')}><MapPin size={17} /> Карта</button></div>}
    {SHOW_COMPARISON && compareIds.length >= 2 && view !== 'compare' && <div className="compare-float"><span><Check size={16} /> Выбрано {compareIds.length} из 4</span><button onClick={() => void compare()}>Сравнить <ArrowRight size={16} /></button></div>}
    {toast && <div className="toast" role="status">{toast}</div>}
  </div>
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [modelsRoute, setModelsRoute] = useState(() => window.location.pathname === '/models')
  const [dataRoute, setDataRoute] = useState(() => window.location.pathname === '/data')
  const [catalogMounted, setCatalogMounted] = useState(() => window.location.pathname !== '/models')
  const healthRequestId = useRef(0)
  const refresh = useCallback(() => {
    const requestId = ++healthRequestId.current
    setError(null)
    void api.health().then((value) => {
      if (requestId !== healthRequestId.current) return
      setApiSnapshot(value.snapshot_id)
      setHealth(value)
    }).catch((reason) => {
      if (requestId === healthRequestId.current) setError(reason instanceof Error ? reason.message : 'Не удалось открыть каталог')
    })
  }, [])
  useEffect(refresh, [refresh])
  useEffect(() => {
    const refreshVisible = () => { if (document.visibilityState === 'visible') refresh() }
    const timer = window.setInterval(refreshVisible, 60_000)
    window.addEventListener('focus', refreshVisible)
    return () => { window.clearInterval(timer); window.removeEventListener('focus', refreshVisible) }
  }, [refresh])
  useEffect(() => {
    const onPop = () => { const isModels = window.location.pathname === '/models'; setModelsRoute(isModels); setDataRoute(window.location.pathname === '/data'); if (!isModels) setCatalogMounted(true) }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  const openModels = (registryId?: string, candidateId?: string) => {
    const params = new URLSearchParams({ decision: registryId ? '' : candidateId ? 'ready' : 'rejected' })
    if (registryId) params.set('registry_id', registryId)
    if (candidateId) params.set('candidate', candidateId)
    window.history.pushState(null, '', `/models?${params.toString()}`)
    setModelsRoute(true)
    setDataRoute(false)
  }
  const openCatalog = () => { if (window.location.pathname !== '/') window.history.pushState(null, '', '/'); setCatalogMounted(true); setModelsRoute(false); setDataRoute(false) }
  const openData = () => { if (window.location.pathname !== '/data') window.history.pushState(null, '', '/data'); setCatalogMounted(true); setModelsRoute(false); setDataRoute(true) }
  if (error && !health) return <div className="boot-state" role="alert"><strong>Не удалось открыть каталог</strong><p>{error}</p><button onClick={refresh}>Повторить</button></div>
  if (!health) return <div className="boot-state" role="status">Загружаем каталог…</div>
  return <>
    {catalogMounted && <div hidden={modelsRoute} inert={modelsRoute} aria-hidden={modelsRoute}>{health.catalog_view === 'building_registry' || health.catalog_view === 'developer_buildings' || health.registry_enabled
      ? <RegistryApp key={`${health.registry_snapshot_id ?? health.catalog_view}:${health.map_run_id ?? ''}:${health.unit_run_id ?? ''}:${health.enrichment_run_id ?? ''}`} health={health} active={!modelsRoute} routeView={dataRoute ? 'data' : 'catalog'} onCatalog={openCatalog} onData={openData} onModels={openModels} />
      : <LegacyApp initialHealth={health} active={!modelsRoute} routeView={dataRoute ? 'data' : 'catalog'} onCatalog={openCatalog} onData={openData} onModels={openModels} />}</div>}
    {modelsRoute && (health.catalog_view === 'developer_buildings'
      ? <div className="boot-state" role="status"><strong>Оценки моделей для этого снимка не опубликованы</strong><p>Откройте каталог домов и проверенные точки на карте.</p><button onClick={openCatalog}>К каталогу</button></div>
      : <ModelsPage onCatalog={openCatalog} onData={openData} />)}
  </>
}
