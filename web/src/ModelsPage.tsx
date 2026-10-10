import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Building2, ChevronLeft, ChevronRight, Layers3, RefreshCw, Search } from 'lucide-react'
import { api, ModelSnapshotChanged, setModelRun, type ModelCandidate, type ModelCandidateAttempt, type ModelCandidateDetail, type ModelCandidateGeometry, type ModelCandidateList, type ModelCandidateSummary } from './api'
import { MODEL_PAGE_SIZE, decisionLabels, modelProgress, modelReviewSearch, parseModelReviewQuery, reasonLabels, safeModelNextStep, type ModelDecision, type ModelReviewFilters } from './modelReview'
import SelectedModelPreview from './SelectedModelPreview'

type RouteState = { filters: ModelReviewFilters; selectedId: string | null }
const routeFromLocation = (): RouteState => {
  const params = new URLSearchParams(window.location.search)
  return { filters: parseModelReviewQuery(window.location.search), selectedId: params.get('candidate')?.slice(0, 100) || null }
}
const dateLabel = (value: string | null | undefined) => {
  if (!value) return 'Дата не подтверждена'
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(parsed)
}
const count = (value: number) => new Intl.NumberFormat('ru-RU').format(value)
const sourceGroupLabel = (value: string) => ({ registry: 'Запись проекта', mapped_construction: 'Контур стройки на карте', external_event: 'Сообщение о строительстве' })[value as 'registry' | 'mapped_construction' | 'external_event'] ?? 'Кандидат модели'
const projectLinkLabel = (value: string) => ({ verified: 'Связь с ЖК подтверждена', review: 'Связь с ЖК проверяется', unlinked: 'Не привязан к ЖК' })[value as 'verified' | 'review' | 'unlinked'] ?? 'Связь с ЖК неизвестна'
const geometryLabel = (item: ModelCandidate) => item.depiction === 'schematic_massing' ? 'Оценочный объём'
  : item.depiction === 'licensed_exterior' ? 'Лицензированный экстерьер'
    : item.depiction === 'construction_outline' || item.depiction === 'project_footprint' ? 'Плоский контур' : 'Геометрия не опубликована'
const geometryQualityLabel = (value: string | null) => ({ mapped_footprint: 'Контур из картографических данных', mapped_estimate: 'Оценка по картографическим данным', measured: 'Измеренная геометрия', licensed: 'Разрешённая модель' })[value ?? ''] ?? 'Не определено'
const safeRequestError = (reason: unknown, fallback: string) => reason instanceof ModelSnapshotChanged
  ? 'Оценка моделей обновилась. Обновите данные, чтобы видеть один снимок.'
  : reason instanceof Error && /503|404/.test(reason.message) ? 'Проверка моделей ещё не опубликована на этом сервере. Повторите после обновления API.' : fallback

function writeRoute(route: RouteState, replace = false): void {
  const params = new URLSearchParams(modelReviewSearch(route.filters))
  if (route.selectedId) params.set('candidate', route.selectedId)
  const url = `/models?${params.toString()}`
  if (replace) window.history.replaceState(null, '', url)
  else window.history.pushState(null, '', url)
}

function DecisionBadge({ decision }: { decision: ModelDecision }) {
  return <span className={`model-decision model-decision-${decision}`}>{decisionLabels[decision]}</span>
}

function CandidateCard({ item, selected, onSelect }: { item: ModelCandidate; selected: boolean; onSelect: () => void }) {
  return <article className={`model-candidate ${selected ? 'selected' : ''}`} data-testid={`model-${item.candidate_id}`}>
    <button type="button" className="model-candidate-button" onClick={onSelect} aria-pressed={selected}>
      <span className="model-candidate-top"><span className="model-source-group">{sourceGroupLabel(item.source_group)}</span><DecisionBadge decision={item.decision} /></span>
      <strong>{item.name}</strong>
      <span className="model-candidate-link">{projectLinkLabel(item.project_link_status)}</span>
      <span className="model-candidate-reason">{item.explanation_ru || (item.decision === 'ready' ? geometryLabel(item) : 'Причина ещё не передана; запись требует повторной проверки.')}</span>
      {item.decision !== 'ready' && <span className="model-candidate-next"><b>Что нужно:</b> {safeModelNextStep(item.reason_codes, item.next_step_ru)}</span>}
      <span className="model-candidate-foot"><span>{geometryLabel(item)}</span><span>{dateLabel(item.geometry_as_of)}</span></span>
    </button>
  </article>
}

function CandidateDetails({ item, attempts, geometry, loadingGeometry, geometryError }: { item: ModelCandidate; attempts?: ModelCandidateAttempt[]; geometry: ModelCandidateGeometry | null; loadingGeometry: boolean; geometryError: string | null }) {
  const confidence = item.geometry_confidence
  return <div className="model-detail-body">
    <div className="model-detail-heading"><span className="section-kicker">КАНДИДАТ МОДЕЛИ</span><DecisionBadge decision={item.decision} /></div>
    <h2>{item.name}</h2>
    <p className="model-detail-subtitle">{sourceGroupLabel(item.source_group)} · {projectLinkLabel(item.project_link_status)}</p>
    {item.project_link_status === 'unlinked' && <p className="model-scope-note">Этот контур описывает наблюдаемую стройку и не подтверждает проектный корпус или фасад ЖК.</p>}
    <div className="model-detail-facts">
      <span><small>Готовность геометрии</small><strong>{item.ready_to_render ? geometryLabel(item) : 'Не готова к показу'}</strong></span>
      <span><small>Качество геометрии</small><strong>{geometryQualityLabel(item.geometry_quality)}</strong></span>
      <span><small>Дата геометрии</small><strong>{dateLabel(item.geometry_as_of)}</strong></span>
      {typeof confidence === 'number' && <span><small>Уверенность геометрии</small><strong>{Math.round(confidence * 100)}%</strong></span>}
    </div>
    <div className="model-explanation"><strong>Результат проверки</strong><p>{item.explanation_ru || (item.decision === 'ready' ? 'Геометрия доступна для ограниченного показа.' : 'Пояснение не передано. Эту модель пока нельзя считать подтверждённой.')}</p>{item.reason_codes.length > 0 && <div className="model-reason-tags">{item.reason_codes.map((code) => <span key={code}>{reasonLabels[code] ?? 'Нужна дополнительная проверка'}</span>)}</div>}</div>
    {attempts && attempts.length > 0 && <div className="model-attempts"><strong>Что проверено</strong><ul>{attempts.map((attempt, index) => <li key={`${attempt.code}-${index}`}><b>{attempt.label_ru}</b><span>{attempt.result_ru}{attempt.count !== null ? ` · ${count(attempt.count)}` : ''}</span></li>)}</ul></div>}
    {item.decision !== 'ready' && <div className="model-next-step"><strong>Что нужно исправить</strong><p>{safeModelNextStep(item.reason_codes, item.next_step_ru)}</p></div>}
    {item.decision === 'ready' && (loadingGeometry ? <div className="model-preview-loading" role="status">Готовим предпросмотр выбранного контура…</div>
      : geometryError ? <div className="model-next-step" role="status"><strong>Предпросмотр недоступен</strong><p>{geometryError}</p></div>
        : geometry ? <SelectedModelPreview item={item} geometry={geometry} />
          : <div className="model-preview-loading" role="status">Контур для предпросмотра не передан.</div>)}
    <p className="model-detail-disclaimer">Оценка относится к геометрии, а не к наличию квартир. Проектная или картографическая форма не доказывает фактический фасад и размеры отдельного ЖК.</p>
  </div>
}

export default function ModelsPage({ onCatalog, onData }: { onCatalog: () => void; onData: () => void }) {
  const [route, setRoute] = useState<RouteState>(routeFromLocation)
  const selectedIdRef = useRef(route.selectedId)
  const [queryDraft, setQueryDraft] = useState(route.filters.query)
  const [summary, setSummary] = useState<ModelCandidateSummary | null>(null)
  const [list, setList] = useState<ModelCandidateList | null>(null)
  const [listKey, setListKey] = useState<string | null>(null)
  const [detail, setDetail] = useState<ModelCandidateDetail | null>(null)
  const [geometry, setGeometry] = useState<ModelCandidateGeometry | null>(null)
  const [loadingSummary, setLoadingSummary] = useState(true)
  const [loadingList, setLoadingList] = useState(false)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [loadingGeometry, setLoadingGeometry] = useState(false)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [geometryError, setGeometryError] = useState<string | null>(null)
  const [reloadVersion, setReloadVersion] = useState(0)
  const filterKeyRef = useRef(modelReviewSearch(route.filters))

  useEffect(() => {
    if (window.location.pathname === '/models' && !new URLSearchParams(window.location.search).has('decision')) writeRoute(route, true)
    const onPop = () => { const next = routeFromLocation(); selectedIdRef.current = next.selectedId; filterKeyRef.current = modelReviewSearch(next.filters); setListError(null); setDetailError(null); setGeometryError(null); setRoute(next); setQueryDraft(next.filters.query) }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  const navigate = (next: RouteState) => { selectedIdRef.current = next.selectedId; filterKeyRef.current = modelReviewSearch(next.filters); setListError(null); setDetailError(null); setGeometryError(null); writeRoute(next); setRoute(next); setQueryDraft(next.filters.query) }

  useEffect(() => {
    const controller = new AbortController()
    setSummary(null); setList(null); setListKey(null); setSummaryError(null); setLoadingSummary(true)
    void api.modelSummary(controller.signal).then((value) => {
      if (controller.signal.aborted) return
      if (value.dataset_id !== 'model_candidates' || !value.model_run_id || value.run_status !== 'ready') throw new Error('Неполный снимок моделей')
      setModelRun(value.model_run_id)
      setSummary(value)
    }).catch((reason) => { if (!controller.signal.aborted) setSummaryError(safeRequestError(reason, 'Не удалось получить итог проверки моделей. Попробуйте ещё раз.')) })
      .finally(() => { if (!controller.signal.aborted) setLoadingSummary(false) })
    return () => controller.abort()
  }, [reloadVersion])

  const filterKey = modelReviewSearch(route.filters)
  useEffect(() => {
    if (!summary) return
    const controller = new AbortController()
    setList(null); setListKey(null); setListError(null); setLoadingList(true)
    const filters = { query: route.filters.query || undefined, decision: route.filters.decision || undefined,
      reason_code: route.filters.reason_code || undefined, source_group: route.filters.source_group || undefined,
      registry_id: route.filters.registry_id || undefined,
      limit: MODEL_PAGE_SIZE, offset: route.filters.offset }
    void api.modelCandidates(filters, summary.model_run_id, controller.signal).then((value) => { if (!controller.signal.aborted && filterKeyRef.current === filterKey) { setList(value); setListKey(filterKey) } })
      .catch((reason) => { if (!controller.signal.aborted && filterKeyRef.current === filterKey) setListError(safeRequestError(reason, 'Не удалось загрузить кандидатов. Повторите попытку.')) })
      .finally(() => { if (!controller.signal.aborted && filterKeyRef.current === filterKey) setLoadingList(false) })
    return () => controller.abort()
  }, [summary, filterKey])

  useEffect(() => {
    if (!summary || !route.selectedId) { setDetail(null); setGeometry(null); setDetailError(null); return }
    const controller = new AbortController()
    setDetail(null); setGeometry(null); setDetailError(null); setGeometryError(null); setLoadingDetail(true); setLoadingGeometry(false)
    void api.modelCandidate(route.selectedId, summary.model_run_id, controller.signal).then(async (value) => {
      if (controller.signal.aborted || selectedIdRef.current !== route.selectedId) return
      if (value.item.candidate_id !== route.selectedId) throw new ModelSnapshotChanged('Кандидат изменился.')
      setDetail(value)
      if (value.item.decision !== 'ready' || !value.item.ready_to_render) return
      setLoadingGeometry(true)
      try {
        const next = await api.modelGeometry(route.selectedId!, summary.model_run_id, controller.signal)
        if (!controller.signal.aborted && selectedIdRef.current === route.selectedId && next.candidate_id === route.selectedId) setGeometry(next)
      } catch (reason) {
        if (!controller.signal.aborted && selectedIdRef.current === route.selectedId) setGeometryError(safeRequestError(reason, 'Проверенный контур не удалось открыть.'))
      } finally { if (!controller.signal.aborted && selectedIdRef.current === route.selectedId) setLoadingGeometry(false) }
    }).catch((reason) => { if (!controller.signal.aborted && selectedIdRef.current === route.selectedId) setDetailError(safeRequestError(reason, 'Не удалось открыть выбранную оценку.')) })
      .finally(() => { if (!controller.signal.aborted && selectedIdRef.current === route.selectedId) setLoadingDetail(false) })
    return () => controller.abort()
  }, [summary, route.selectedId])

  const progress = summary ? modelProgress(summary.counts.scanned, summary.counts) : null
  const visibleList = listKey === filterKey && list?.model_run_id === summary?.model_run_id ? list : null
  const visibleDetail = detail?.model_run_id === summary?.model_run_id && detail?.item.candidate_id === route.selectedId ? detail : null
  const visibleGeometry = geometry?.model_run_id === summary?.model_run_id && geometry?.candidate_id === route.selectedId ? geometry : null
  const selectedFromList = useMemo(() => visibleList?.items.find((item) => item.candidate_id === route.selectedId) ?? null, [visibleList, route.selectedId])
  const selectedItem = visibleDetail?.item ?? selectedFromList
  const updateFilters = (patch: Partial<ModelReviewFilters>) => navigate({ filters: { ...route.filters, ...patch, offset: 0 }, selectedId: null })
  const onSearch = (event: React.FormEvent) => { event.preventDefault(); updateFilters({ query: queryDraft.trim() }) }
  const clearFilters = () => navigate({ filters: { decision: 'rejected', reason_code: '', source_group: '', registry_id: route.filters.registry_id, query: '', offset: 0 }, selectedId: null })
  const select = (item: ModelCandidate) => navigate({ ...route, selectedId: item.candidate_id })
  const changePage = (offset: number) => navigate({ filters: { ...route.filters, offset }, selectedId: null })

  return <div className="app-shell models-page">
    <header className="site-header"><a className="brand" href="/" onClick={(event) => { event.preventDefault(); onCatalog() }}><span className="brand-mark"><Building2 size={19} strokeWidth={2.4} /></span><span>ново<span className="brand-period">.</span></span></a><span className="brand-descriptor">Проверка моделей Москвы</span><nav className="main-nav" aria-label="Основная навигация"><a className="nav-link" href="/" onClick={(event) => { event.preventDefault(); onCatalog() }}>Каталог</a><a className="nav-link active" href="/models?decision=rejected" aria-current="page">Модели</a><a className="nav-link" href="/data" onClick={(event) => { event.preventDefault(); onData() }}>Данные</a></nav></header>
    <main className="models-scroll"><div className="models-inner">
      <div className="models-intro"><div><span className="section-kicker"><Layers3 size={14} /> ПРОВЕРКА ГЕОМЕТРИИ</span><h1>Какие модели можно показать?</h1><p>Оценка касается найденной геометрии. Отклонённая модель не удаляет здание из каталога; контур стройки без подтверждённой связи не становится корпусом ЖК.</p></div><a href="/" onClick={(event) => { event.preventDefault(); onCatalog() }}><ArrowLeft size={16} /> К каталогу</a></div>
      {loadingSummary ? <div className="models-loading" role="status">Загружаем итог оценки…</div> : summaryError ? <div className="models-error" role="alert"><strong>Проверка моделей пока недоступна</strong><p>{summaryError}</p><button onClick={() => setReloadVersion((value) => value + 1)}><RefreshCw size={15} /> Повторить</button></div> : summary && <>
        <div className="models-summary" aria-label="Итоги проверки">
          <div className="model-stat"><small>Просмотрено кандидатов</small><strong>{count(summary.counts.scanned)}</strong><span>записи подключённых наборов</span></div>
          {(['ready', 'review', 'rejected', 'unavailable'] as const).map((decision) => <button key={decision} className={`model-stat ${route.filters.decision === decision ? 'active' : ''}`} onClick={() => updateFilters({ decision })} aria-pressed={route.filters.decision === decision}><small>{decisionLabels[decision]}</small><strong>{count(summary.counts[decision])}</strong><span>{decision === 'ready' ? 'контур или оценочный объём' : decision === 'rejected' ? 'с объяснением причины' : decision === 'review' ? 'нужны уточнения' : 'индивидуальная модель не найдена'}</span></button>)}
        </div>
        <p className="models-snapshot">Оценено {count(progress?.assessed ?? 0)} из {count(summary.counts.scanned)} · ожидают оценки {count(progress?.pending ?? 0)} · снимок {dateLabel(summary.checked_at)}. {summary.ready_depictions ? `Готовы к показу: ${count(summary.ready_depictions.construction_outline ?? 0)} плоских контуров, ${count(summary.ready_depictions.schematic_massing ?? 0)} оценочных объёмов.` : '«Можно показать» включает плоские контуры; число объёмов отдельно не подтверждено.'}</p>
        {route.filters.registry_id && <p className="models-project-scope">Показаны оценки, связанные с выбранным ЖК. Самостоятельные контуры строек без подтверждённой связи остаются в общем списке. <button type="button" onClick={() => navigate({ filters: { ...route.filters, registry_id: '', offset: 0 }, selectedId: null })}>Показать всех кандидатов</button></p>}
        <div className="models-workspace">
          <section className={`models-list-panel ${route.selectedId ? 'has-selection' : ''}`} aria-label="Кандидаты моделей">
            <form className="models-filters" onSubmit={onSearch}><label className="models-search"><Search size={17} /><input value={queryDraft} onChange={(event) => setQueryDraft(event.target.value)} placeholder="Название кандидата" aria-label="Поиск кандидатов" /><button type="submit" aria-label="Искать"><ArrowRight size={17} /></button></label><div className="models-filter-row"><label>Решение<select value={route.filters.decision} onChange={(event) => updateFilters({ decision: event.target.value as ModelDecision | '' })}><option value="">Все решения</option>{(['ready', 'review', 'rejected', 'unavailable'] as const).map((decision) => <option key={decision} value={decision}>{decisionLabels[decision]}</option>)}</select></label><label>Причина<select value={route.filters.reason_code} onChange={(event) => updateFilters({ reason_code: event.target.value })}><option value="">Все причины</option>{summary.facets.reason_codes.map((facet) => <option key={facet.value} value={facet.value}>{reasonLabels[facet.value] ?? 'Дополнительная проверка'} · {count(facet.count)}</option>)}</select></label><label>Группа<select value={route.filters.source_group} onChange={(event) => updateFilters({ source_group: event.target.value })}><option value="">Все группы</option>{summary.facets.source_groups.map((facet) => <option key={facet.value} value={facet.value}>{sourceGroupLabel(facet.value)} · {count(facet.count)}</option>)}</select></label></div><button type="button" className="models-clear" onClick={clearFilters}>Сбросить фильтры</button></form>
            {listError && <div className="models-error" role="alert"><strong>Не удалось загрузить список</strong><p>{listError}</p><button onClick={() => setReloadVersion((value) => value + 1)}><RefreshCw size={15} /> Повторить</button></div>}
            <div className="models-list-head"><strong>{visibleList ? `${count(visibleList.records_total)} найдено` : 'Кандидаты'}</strong><span>По {MODEL_PAGE_SIZE} на странице</span></div>
            {loadingList || (!visibleList && !listError) ? <div className="models-loading" role="status">Загружаем кандидатов…</div> : visibleList?.items.length ? <div className="models-cards">{visibleList.items.map((item) => <CandidateCard key={item.candidate_id} item={item} selected={route.selectedId === item.candidate_id} onSelect={() => select(item)} />)}</div>
              : visibleList && !listError ? <div className="models-empty" role="status"><strong>По этим условиям кандидатов нет</strong><p>В снимке {count(summary.counts.scanned)} проверенных записей. Измените решение, причину или запрос; отсутствие результата не означает, что модель отклонена.</p><button onClick={clearFilters}>Показать отклонённые</button></div> : null}
            {visibleList && <div className="models-pagination"><button disabled={route.filters.offset === 0 || loadingList} onClick={() => changePage(Math.max(0, route.filters.offset - MODEL_PAGE_SIZE))}><ChevronLeft size={16} /> Назад</button><span>{visibleList.records_total === 0 ? '0 из 0' : `${count(Math.min(visibleList.records_total, route.filters.offset + 1))}–${count(Math.min(visibleList.records_total, route.filters.offset + visibleList.items.length))} из ${count(visibleList.records_total)}`}</span><button disabled={route.filters.offset + visibleList.items.length >= visibleList.records_total || loadingList} onClick={() => changePage(route.filters.offset + MODEL_PAGE_SIZE)}>Далее <ChevronRight size={16} /></button></div>}
          </section>
          <aside className={`models-detail-panel ${route.selectedId ? 'has-selection' : ''}`} aria-label="Оценка выбранной модели">
            {route.selectedId && <button className="models-detail-back" onClick={() => navigate({ ...route, selectedId: null })}><ArrowLeft size={16} /> К списку</button>}
            {!route.selectedId ? <div className="models-detail-placeholder"><Layers3 size={28} /><strong>Выберите кандидата</strong><p>Откройте оценку, чтобы увидеть причину отказа, следующий шаг и доступный предпросмотр. Список сам не загружает 3D.</p></div>
              : loadingDetail && !selectedItem ? <div className="models-loading" role="status">Открываем оценку…</div>
                : detailError ? <div className="models-error" role="alert"><strong>Оценка не открылась</strong><p>{detailError}</p><button onClick={() => setReloadVersion((value) => value + 1)}><RefreshCw size={15} /> Повторить</button></div>
                  : selectedItem ? <CandidateDetails item={selectedItem} attempts={visibleDetail?.item.attempts} geometry={visibleGeometry} loadingGeometry={loadingGeometry || loadingDetail || !visibleDetail} geometryError={geometryError} />
                    : <div className="models-loading" role="status">Открываем оценку…</div>}
          </aside>
        </div>
      </>}
    </div></main>
  </div>
}
