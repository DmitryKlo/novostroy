import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, Building2, ChevronDown, Layers3, X } from 'lucide-react'
import { api, type DeveloperFloorDetail, type DeveloperFloorList, type DeveloperPlan, type DeveloperUnitCriteria, type DeveloperUnitDetail, type DeveloperUnitList, type RegistryBuilding } from './api'
import DeveloperMediaImage from './DeveloperMediaImage'
import { explorerBack, initialExplorerState, openExplorerFloor, openExplorerUnit, type ExplorerState } from './developerExplorerNav'

type Props = { building: RegistryBuilding; registrySnapshotId: string; mapRunId: string; enrichmentRunId: string | null; unitFilters: DeveloperUnitCriteria; filterSummary: string | null; onClose: () => void }
const PAGE_SIZE = 24
const DeveloperFloorVolume = lazy(() => import('./DeveloperFloorVolume'))
const number = (value: number) => new Intl.NumberFormat('ru-RU').format(value)
const date = (value: string | null | undefined) => {
  if (!value) return 'дата не указана'
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? 'дата не указана'
    : new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(parsed)
}
const sourceDeadline = (value: string) => /^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(value) && !Number.isNaN(new Date(value).valueOf()) ? date(value) : value
const priceContext: Record<string, string> = {
  payment_100_percent: 'при полной оплате', promotion_or_conditions: 'с условиями или акцией',
  source_base_amount: 'базовая сумма источника', source_displayed_total: 'показанная источником сумма',
  conditions_unverified: 'условия не проверены',
}
const localAsset = (plan: DeveloperPlan): string | null => {
  if (!plan.asset_url.startsWith('/') || plan.asset_url.startsWith('//') || plan.asset_url.includes('..')) return null
  return /^image\/(?:png|jpeg|webp|avif)$/.test(plan.mime_type) ? plan.asset_url : null
}

export default function DeveloperExplorer({ building, registrySnapshotId, mapRunId, enrichmentRunId, unitFilters, filterSummary, onClose }: Props) {
  const [nav, setNav] = useState<ExplorerState>(initialExplorerState)
  const [floors, setFloors] = useState<DeveloperFloorList | null>(null)
  const [floorsError, setFloorsError] = useState<string | null>(null)
  const [units, setUnits] = useState<DeveloperUnitList | null>(null)
  const [unitsError, setUnitsError] = useState<string | null>(null)
  const [unit, setUnit] = useState<DeveloperUnitDetail | null>(null)
  const [unitError, setUnitError] = useState<string | null>(null)
  const [floorDetail, setFloorDetail] = useState<DeveloperFloorDetail | null>(null)
  const [offset, setOffset] = useState(0)
  const [showAllUnits, setShowAllUnits] = useState(false)
  const activeUnitFilters: DeveloperUnitCriteria = showAllUnits ? {} : unitFilters
  const activeFilterKey = JSON.stringify(activeUnitFilters)
  const filtersActive = Boolean(filterSummary && !showAllUnits)
  const panelRef = useRef<HTMLElement>(null)
  const backRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLButtonElement>(null)
  const pendingFocus = useRef<{ kind: 'floor' | 'unit'; id: string } | null>(null)
  const regionEscapeRef = useRef<(() => boolean) | null>(null)
  const registerRegionEscape = useCallback((handler: (() => boolean) | null) => { regionEscapeRef.current = handler }, [])

  const validate = (value: { registry_snapshot_id: string; map_run_id: string; unit_run_id: string; enrichment_run_id?: string | null; registry_id: string }, unitRunId?: string) => {
    if (value.registry_snapshot_id !== registrySnapshotId || value.map_run_id !== mapRunId
      || value.registry_id !== building.registry_id || !value.unit_run_id || (value.enrichment_run_id ?? null) !== enrichmentRunId
      || (unitRunId && value.unit_run_id !== unitRunId)) throw new Error('Каталог квартир обновился. Закройте сведения и откройте дом заново.')
  }
  const validateFilters = (value: { applied_filters?: DeveloperUnitCriteria }) => {
    if (!filtersActive) return
    if (!value.applied_filters || Object.entries(activeUnitFilters).some(([key, expected]) => value.applied_filters?.[key as keyof DeveloperUnitCriteria] !== expected)) {
      throw new Error('Этажи и квартиры не соответствуют выбранным фильтрам. Обновите сведения о доме.')
    }
  }
  const focusReturn = (target: HTMLElement | null) => requestAnimationFrame(() => {
    if (target?.isConnected) target.focus()
    else backRef.current?.focus()
  })
  const goBack = () => {
    const next = explorerBack(nav)
    if (next.close) { onClose(); return }
    if (nav.stage.kind === 'unit' && !nav.menuOpen) {
      setUnits(null)
      if (nav.stage.parent === 'floor') pendingFocus.current = { kind: 'unit', id: nav.stage.unitId }
    } else if (nav.stage.kind === 'floor' && !nav.menuOpen) pendingFocus.current = { kind: 'floor', id: nav.stage.floorId ?? '_unassigned' }
    setNav(next.state)
    if (nav.menuOpen) focusReturn(menuRef.current)
    else focusReturn(backRef.current)
  }
  const chooseFloor = (floorId: string | null, sectionId: string | null) => {
    pendingFocus.current = null
    setOffset(0); setUnits(null); setUnit(null); setFloorDetail(null)
    setNav((current) => openExplorerFloor(current, floorId, sectionId))
    focusReturn(backRef.current)
  }
  const chooseUnit = (unitId: string, floorId: string | null, sectionId: string | null) => {
    pendingFocus.current = null
    setUnit(null); setNav((current) => openExplorerUnit(current, unitId, floorId, sectionId))
    focusReturn(backRef.current)
  }

  useEffect(() => { backRef.current?.focus() }, [])
  useEffect(() => {
    const pending = pendingFocus.current
    if (!pending) return
    const ready = pending.kind === 'floor' ? nav.stage.kind === 'building' : nav.stage.kind === 'floor' && units !== null
    if (!ready) return
    const element = [...(panelRef.current?.querySelectorAll<HTMLElement>(pending.kind === 'floor' ? '[data-floor-id]' : '[data-unit-id]') ?? [])]
      .find((candidate) => candidate.dataset[pending.kind === 'floor' ? 'floorId' : 'unitId'] === pending.id)
    if (!element) return
    pendingFocus.current = null
    requestAnimationFrame(() => element.focus())
  }, [nav.stage, units])
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); if (!nav.menuOpen && regionEscapeRef.current?.()) return; goBack(); return }
      if (event.key !== 'Tab' || !panelRef.current) return
      const controls = [...panelRef.current.querySelectorAll<HTMLElement>('button:not([disabled]),a[href],select,input,[role="button"][tabindex="0"]')]
        .filter((element) => element.offsetParent !== null)
      if (!controls.length) return
      const first = controls[0]; const last = controls[controls.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  })

  useEffect(() => {
    const controller = new AbortController()
    setFloors(null); setFloorsError(null)
    void api.developerFloors(building.registry_id, activeUnitFilters, controller.signal).then((value) => {
      if (controller.signal.aborted) return
      validate(value); validateFilters(value); setFloors(value)
    }).catch((reason) => { if (!controller.signal.aborted) setFloorsError(reason instanceof Error ? reason.message : 'Не удалось загрузить этажи') })
    return () => controller.abort()
  }, [building.registry_id, registrySnapshotId, mapRunId, activeFilterKey])

  const stage = nav.stage
  useEffect(() => {
    if (!floors || stage.kind === 'unit') return
    const controller = new AbortController()
    const floorFilters = stage.kind === 'floor'
      ? stage.floorId ? { floor_id: stage.floorId, limit: PAGE_SIZE, offset }
        : { floor_known: false, limit: PAGE_SIZE, offset }
      : { limit: PAGE_SIZE, offset }
    const filters = { ...activeUnitFilters, ...floorFilters }
    setUnits(null); setUnitsError(null)
    void api.developerUnits(building.registry_id, filters, controller.signal).then((value) => {
      if (controller.signal.aborted) return
      validate(value, floors.unit_run_id)
      validateFilters(value)
      if (value.offset !== offset || value.limit > PAGE_SIZE || value.total < 0) throw new Error('Список квартир относится к другой странице.')
      setUnits(value)
    }).catch((reason) => { if (!controller.signal.aborted) setUnitsError(reason instanceof Error ? reason.message : 'Не удалось загрузить квартиры') })
    return () => controller.abort()
  }, [building.registry_id, floors?.unit_run_id, stage.kind, stage.kind === 'floor' ? stage.floorId : '', offset, activeFilterKey])

  useEffect(() => {
    if (!floors || stage.kind !== 'unit') return
    const controller = new AbortController()
    setUnit(null); setUnitError(null)
    void api.developerUnit(building.registry_id, stage.unitId, activeUnitFilters, controller.signal).then((value) => {
      if (controller.signal.aborted) return
      validate(value, floors.unit_run_id)
      validateFilters(value)
      if (value.unit_id !== stage.unitId) throw new Error('Квартира относится к другой записи.')
      setUnit(value)
    }).catch((reason) => { if (!controller.signal.aborted) setUnitError(reason instanceof Error ? reason.message : 'Не удалось загрузить квартиру') })
    return () => controller.abort()
  }, [building.registry_id, floors?.unit_run_id, stage.kind, stage.kind === 'unit' ? stage.unitId : '', activeFilterKey])

  useEffect(() => {
    if (!floors || stage.kind !== 'floor' || !stage.floorId) return
    setFloorDetail(null)
    const controller = new AbortController()
    void api.developerFloor(building.registry_id, stage.floorId, activeUnitFilters, controller.signal).then((value) => {
      if (controller.signal.aborted) return
      validate(value, floors.unit_run_id)
      validateFilters(value)
      if (value.floor_id !== stage.floorId) throw new Error('План относится к другому этажу.')
      setFloorDetail(value)
    }).catch(() => { if (!controller.signal.aborted) setFloorDetail(null) })
    return () => controller.abort()
  }, [building.registry_id, floors?.unit_run_id, stage.kind, stage.kind === 'floor' ? stage.floorId : '', activeFilterKey])

  const title = stage.kind === 'building' ? building.name
    : stage.kind === 'floor' ? stage.floorId === null ? 'Этаж не указан'
      : `${floors?.sections.flatMap((section) => section.floors).find((floor) => floor.floor_id === stage.floorId)?.floor_number ?? '—'} этаж`
      : unit?.unit_label ? `Квартира ${unit.unit_label}` : 'Квартира'
  const backLabel = nav.menuOpen ? 'Закрыть меню' : stage.kind === 'unit'
    ? stage.parent === 'floor' ? 'К этажу' : 'К дому' : stage.kind === 'floor' ? 'К дому' : 'К карте'
  const image = floorDetail?.plan && localAsset(floorDetail.plan)
  const floorMedia = !image ? floorDetail?.media?.find((media) => media.scope === 'floor_plan' && media.role === 'floor_plan') : null
  const preparedUnitPlans = unit?.plans.filter((plan) => ['apartment_plan', 'furnished_apartment_plan'].includes(plan.role) && localAsset(plan)) ?? []
  const unitMedia = preparedUnitPlans.length ? [] : unit?.media?.filter((media) => media.scope === 'unit_plan'
    && (media.role === 'apartment_plan' || media.role === 'furnished_apartment_plan')) ?? []

  return <div className="developer-explorer-backdrop">
    <section className="developer-explorer" ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="developer-explorer-title">
      <header className="developer-explorer-header">
        <button ref={backRef} type="button" className="developer-explorer-back" onClick={goBack}><ArrowLeft size={16} />{backLabel}</button>
        <button ref={menuRef} type="button" className="developer-explorer-menu-button" aria-expanded={nav.menuOpen} aria-controls="developer-explorer-menu" onClick={() => setNav((current) => ({ ...current, menuOpen: !current.menuOpen }))}>Разделы <ChevronDown size={15} /></button>
        <button type="button" className="developer-explorer-close" aria-label="Закрыть сведения о доме" onClick={onClose}><X size={18} /></button>
      </header>
      {nav.menuOpen && <nav id="developer-explorer-menu" className="developer-explorer-menu" aria-label="Разделы дома">
        <button type="button" onClick={() => { setNav(initialExplorerState()); setOffset(0); focusReturn(backRef.current) }}>Дом</button>
        {floors?.sections.flatMap((section) => section.floors.map((floor) => ({ floor, section }))).map(({ floor, section }) => <button type="button" key={floor.floor_id} onClick={() => chooseFloor(floor.floor_id, section.section_id)}>{section.section_label ? `${section.section_label} · ` : ''}{floor.floor_number} этаж</button>)}
      </nav>}
      <div className="developer-explorer-scroll">
        <div className="developer-explorer-intro"><span className="section-kicker"><Building2 size={14} /> ДОМ И КВАРТИРЫ</span><h2 id="developer-explorer-title">{title}</h2><p>{building.complex_label ? `ЖК: ${building.complex_label} · ` : ''}{building.address ?? 'Адрес не указан'}{building.address_complete === false ? ' · частичный адрес' : ''}</p></div>
        {filterSummary && <div className="developer-explorer-filter-note"><span>Активные фильтры: {filterSummary}. {building.matching_units_count != null ? `Подходит квартир в доме: ${number(building.matching_units_count)}. ` : ''}{showAllUnits ? 'Ниже показаны все квартиры дома из снимка.' : 'Список ниже сохраняет эти ограничения.'}</span><button type="button" onClick={() => { setShowAllUnits((current) => !current); setOffset(0); setNav(initialExplorerState()); setUnits(null); setUnit(null); setFloorDetail(null); focusReturn(backRef.current) }}>{showAllUnits ? 'Вернуть фильтры' : 'Показать все квартиры дома'}</button></div>}
        {floorsError && <div className="developer-explorer-state" role="alert">{floorsError}</div>}
        {!floors && !floorsError && <div className="developer-explorer-state" role="status">Загружаем этажи и квартиры…</div>}
        {floors && stage.kind === 'building' && <>
          <p className="developer-explorer-note">Квартиры наблюдались в каталоге на {date(floors.observed_at)}; наличие и цена сейчас не подтверждены.</p>
          <div className="developer-explorer-facts"><span>Всего этажей по источнику: <b>{floors.total_floors ?? 'не указано'}</b></span><span>Максимальный этаж среди увиденных квартир: <b>{floors.max_observed_unit_floor ?? 'не указан'}</b></span></div>
          <h3>{filtersActive ? 'Этажи с подходящими квартирами' : 'Секции и этажи'}</h3>
          {filtersActive && <p className="developer-explorer-note">Показаны только этажи с квартирами, подходящими под выбранные фильтры. Число на каждом этаже учитывает все ограничения одновременно.</p>}
          {floors.sections.map((section, index) => <section className="developer-explorer-section" key={section.section_id ?? `unknown-${index}`}><h4>{section.section_label ? `Секция ${section.section_label}` : 'Секция не указана'}{section.total_floors != null && section.total_floors_basis === 'source_section_floor_count' ? ` · ${section.total_floors} этажей по источнику` : ''}</h4><div className="developer-explorer-floors">{section.floors.map((floor) => <button type="button" data-floor-id={floor.floor_id} key={floor.floor_id} onClick={() => chooseFloor(floor.floor_id, section.section_id)}><Layers3 size={17} /><strong>{floor.floor_number} этаж</strong><small>{filtersActive ? 'Подходят' : 'Квартир'}: {number(floor.unit_count)}</small></button>)}</div></section>)}
          {floors.unassigned_unit_count > 0 && <button className="developer-explorer-unknown" data-floor-id="_unassigned" type="button" onClick={() => chooseFloor(null, null)}>Этаж не указан · {filtersActive ? 'подходят' : 'квартир'}: {number(floors.unassigned_unit_count)}</button>}
          {!floors.sections.length && !floors.unassigned_unit_count && <p className="developer-explorer-note">{filtersActive ? 'В этом доме нет этажей с квартирами, подходящими под выбранные фильтры.' : 'Для этого дома этажи квартир в источнике не указаны.'}</p>}
          <h3>{filtersActive ? 'Подходящие квартиры из снимка' : 'Все квартиры из снимка'}</h3>
        </>}
        {floors && stage.kind === 'floor' && <>
          {image && floorDetail?.plan_regions?.length ? <Suspense fallback={<div className="developer-explorer-state" role="status">Загружаем схему этажа…</div>}><DeveloperFloorVolume imageUrl={image} regions={floorDetail.plan_regions} registerEscape={registerRegionEscape} /></Suspense>
            : image && <figure className="developer-explorer-plan"><img src={image} alt={`План ${title} по данным каталога`} loading="lazy" /><figcaption>План этажа из источника. Соответствие областей отдельным квартирам не подтверждено.</figcaption></figure>}
          {!image && floorMedia && <DeveloperMediaImage key={floorMedia.media_id} media={floorMedia} label="План этажа из источника; расположение квартир из списка не подтверждено" className="developer-explorer-plan" />}
          {!image && !floorMedia && <p className="developer-explorer-note">Подтверждённой схемы расположения квартир на этом этаже пока нет. Ниже — квартиры из каталога.</p>}
          <h3>Квартиры на этаже</h3>
        </>}
        {floors && stage.kind !== 'unit' && <>
          {unitsError && <div className="developer-explorer-state" role="alert">{unitsError}</div>}
          {!units && !unitsError && <div className="developer-explorer-state" role="status">Загружаем квартиры…</div>}
          {units && <><p className="developer-explorer-count">{filterSummary && !showAllUnits ? 'Подходят' : 'Квартир'}: {number(units.total)} · по данным снимка</p>{units.total === 0 && <p className="developer-explorer-note">На этом этаже квартиры по выбранным фильтрам не найдены.</p>}<div className="developer-explorer-units">{units.items.map((item) => <button type="button" data-unit-id={item.unit_id} key={item.unit_id} onClick={() => chooseUnit(item.unit_id, item.floor_id, item.section_id)}><strong>{item.unit_label ? `Квартира ${item.unit_label}` : 'Квартира без номера'}</strong><span>{item.rooms_label ?? 'Комнатность не указана'}{item.area_m2 != null ? ` · ${number(item.area_m2)} м²` : ''}</span><small>{item.floor_number != null ? `${item.floor_number} этаж` : 'Этаж не указан'}{item.price_rub != null ? ` · цена в снимке ${number(item.price_rub)} ₽` : ''}</small>{item.has_prepared_plan && <em className="developer-explorer-plan-badge">Планировка квартиры есть</em>}</button>)}</div>{units.total > units.limit && <div className="developer-explorer-pages"><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Назад</button><span>{number(offset + 1)}–{number(Math.min(offset + units.items.length, units.total))} из {number(units.total)}</span><button type="button" disabled={offset + units.items.length >= units.total} onClick={() => setOffset(offset + PAGE_SIZE)}>Далее</button></div>}</>}
        </>}
        {floors && stage.kind === 'unit' && <>
          {unitError && <div className="developer-explorer-state" role="alert">{unitError}</div>}
          {!unit && !unitError && <div className="developer-explorer-state" role="status">Загружаем квартиру…</div>}
          {unit && <>
            <div className="developer-explorer-unit-facts">
              <span>Номер квартиры <b>{unit.unit_label ?? 'не указан'}</b></span>
              <span>Секция <b>{unit.section_label ?? 'не указана'}</b></span>
              <span>Этаж <b>{unit.floor_number ?? 'не указан'}</b></span>
              <span>Комнаты <b>{unit.rooms_label ?? 'не указаны'}</b></span>
              <span>Площадь <b>{unit.area_m2 != null ? `${number(unit.area_m2)} м²` : 'не указана'}</b></span>
              <span>Дата наблюдения <b>{date(unit.observed_at)}</b></span>
            </div>
            {unit.price_rub != null && <div className="developer-explorer-price"><strong>{number(unit.price_rub)} ₽</strong><span>Цена в снимке на {date(unit.observed_at)} · {priceContext[unit.price_context] ?? 'условия не проверены'}. Это не текущая цена.</span></div>}
            {unit.price_rub == null && <p className="developer-explorer-note">Цена этой квартиры в снимке не указана.</p>}
            <p className="developer-explorer-note">Наличие квартиры сейчас не подтверждено.{unit.record_updated_at ? ` Запись источника обновлена ${date(unit.record_updated_at)}.` : ''}</p>
            {unit.observed_attributes && <div className="developer-explorer-unit-facts">
              {unit.observed_attributes.finishing_label && <span>Отделка по источнику на {date(unit.observed_attributes.observed_at)} <b>{unit.observed_attributes.finishing_label}</b></span>}
              {unit.observed_attributes.delivery_deadline && <span>Срок передачи, записанный источником на {date(unit.observed_attributes.observed_at)} <b>{sourceDeadline(unit.observed_attributes.delivery_deadline)}</b></span>}
              {unit.observed_attributes.source_sale_status === 'free' && <span>Статус продажи в снимке <b>свободна на дату снимка</b></span>}
              {unit.observed_attributes.previous_price_rub != null && <span>Предыдущая цена по источнику <b>{number(unit.observed_attributes.previous_price_rub)} ₽</b></span>}
              {unit.observed_attributes.discount_percent != null && <span>Скидка в источнике <b>{number(unit.observed_attributes.discount_percent)}%</b></span>}
            </div>}
            <h3>Планировка квартиры</h3>
            {preparedUnitPlans.length ? preparedUnitPlans.map((plan) => <figure className="developer-explorer-plan" key={plan.asset_sha256}><img src={localAsset(plan)!} alt={`План квартиры ${unit.unit_label ?? ''} из каталога`} loading="lazy" /><figcaption>Планировка именно этой квартиры из каталога на дату снимка {date(unit.observed_at)}.</figcaption></figure>)
              : unitMedia.length ? unitMedia.map((media) => <DeveloperMediaImage key={media.media_id} media={media} label={media.role === 'furnished_apartment_plan' ? 'Планировка квартиры с мебелью из источника' : 'Планировка квартиры из источника'} className="developer-explorer-plan" />)
                : <p className="developer-explorer-note">Подготовленной планировки именно этой квартиры пока нет.</p>}
          </>}
        </>}
      </div>
    </section>
  </div>
}
