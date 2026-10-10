import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRight, BarChart3, Database, RefreshCw } from 'lucide-react'
import { api, type CoverageDataset, type CoverageField, type Criterion, type DataCoverageResponse, type Distribution } from './api'

type DatasetId = CoverageDataset['dataset_id']
const DATASETS: { id: DatasetId; label: string; description: string }[] = [
  { id: 'building_registry', label: 'Реестр зданий', description: 'Новостройки и корпуса без сведений о квартирах и ценах' },
  { id: 'developer_building_registry', label: 'Дома из каталога квартир', description: 'Корпуса с наблюдавшимися квартирами; текущее наличие и цены не подтверждены' },
  { id: 'demo_offers', label: 'Демо-предложения', description: 'Иллюстративные квартиры и цены' },
  { id: 'real_current_offers', label: 'Реальные предложения', description: 'Только предложения из разрешённого актуального фида' },
  { id: 'city_geometry', label: 'Геометрия города', description: 'Контуры зданий для карты, не сведения о квартирах' },
]
const FIELD_LABELS: Record<string, string> = {
  'offer.price_total_rub': 'Полная цена', 'unit.area_sqm': 'Площадь квартиры', 'unit.rooms': 'Комнатность',
  'unit.floor': 'Этаж', 'city.footprint_geometry': 'Контур здания',
  'city.source_height_m': 'Высота по данным источника', 'city.source_num_floors': 'Этажность по данным источника',
  'city.render_height_from_floors_m': 'Расчётная высота для карты по этажности',
  'city.render_height_imputed_m': 'Оценочная высота для карты по похожим контурам',
  'city.render_height_m': 'Высота отображения на карте',
  'building.name': 'Название', 'building.address': 'Адрес', 'building.status_as_reported': 'Статус по выгрузке',
  'building.record_kind': 'Тип здания', 'building.location': 'Проверенная геопривязка',
  'building.handover_as_reported': 'Срок сдачи по выгрузке', 'building.floors_as_reported': 'Этажность по выгрузке',
  'building.complex_label': 'ЖК', 'building.project_name': 'ЖК', 'building.developer_name': 'Застройщик',
  'building.unit_count': 'Число записей квартир в снимке',
}
const PRIMARY_OFFER_KEYS = new Set(['offer.price_total_rub', 'offer.price_per_sqm_rub', 'unit.area_sqm', 'unit.rooms', 'unit.floor'])
const GROUP_LABELS: Record<string, string> = { property: 'Квартира и дом', social: 'Рядом с домом', environment: 'Окружение', other: 'Другие поля' }
const number = (value: number) => new Intl.NumberFormat('ru-RU').format(value)
const percent = (value: number, total: number) => total > 0 ? `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value / total * 100)}%` : '—'
const formattedDate = (value: string | null) => {
  if (!value) return 'Дата не указана'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? 'Дата не указана' : new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(date)
}
let sessionCache: DataCoverageResponse | null = null
let sessionCacheKey: string | null = null

function qualitySummary(field: CoverageField): string {
  const counts = field.quality_counts
  const parts = [
    counts.observed && `${number(counts.observed)} по данным`, counts.derived && `${number(counts.derived)} расчётных`,
    counts.illustrative && `${number(counts.illustrative)} иллюстративных`, counts.estimated && `${number(counts.estimated)} оценочных`,
    counts.render_fallback && `${number(counts.render_fallback)} только для показа`,
  ].filter(Boolean)
  return parts.length ? parts.join(' · ') : 'Подтверждённых значений нет'
}

function FieldRow({ field, total, label }: { field: CoverageField; total: number; label: string }) {
  const width = total > 0 ? Math.min(100, field.populated_count / total * 100) : 0
  return <div className="coverage-row" data-testid={`coverage-${field.key}`}>
    <div className="coverage-row-heading"><strong>{label}</strong><span>{number(field.populated_count)} из {number(total)} · {percent(field.populated_count, total)}</span></div>
    <div className="coverage-track" aria-label={`Значение указано в ${field.populated_count} из ${total} записей`}><span style={{ width: `${width}%` }} /></div>
    <small>{qualitySummary(field)} · нет данных: {number(field.unknown_count)}{field.stale_count ? ` · устарело: ${number(field.stale_count)}` : ''}</small>
  </div>
}

function rangeLabel(distribution: Distribution, bin: { lower: number; upper: number }) {
  if (distribution.unit === 'RUB') return `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(bin.lower / 1_000_000)}–${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(bin.upper / 1_000_000)} млн ₽`
  return `${number(bin.lower)}–${number(bin.upper)} м²`
}

function DistributionChart({ distribution, demo }: { distribution: Distribution; demo: boolean }) {
  const max = Math.max(1, ...distribution.bins.map((bin) => bin.count))
  const label = distribution.key === 'offer.price_total_rub' ? 'Полная цена' : distribution.key === 'unit.area_sqm' ? 'Площадь квартиры' : FIELD_LABELS[distribution.key] ?? distribution.key
  return <section className="distribution-card" data-testid={`distribution-${distribution.key}`}>
    <div className="distribution-title"><h3>{label}</h3><span>{number(distribution.sample_count)} из {number(distribution.population_count)}</span></div>
    <div className="distribution-bars">{distribution.bins.map((bin, index) => <div className="distribution-bin" key={`${bin.lower}-${bin.upper}-${index}`}><div className="distribution-bar-track"><span style={{ height: `${Math.max(bin.count > 0 ? 6 : 0, bin.count / max * 100)}%` }} /></div><strong>{number(bin.count)}</strong><small title={bin.upper_inclusive ? 'Верхняя граница включена' : 'Верхняя граница не включена'}>{rangeLabel(distribution, bin)}</small></div>)}</div>
    <p>{demo ? 'Распределение иллюстративных значений; оно не описывает рынок.' : 'Распределение только выбранного набора данных.'}{distribution.basis === 'asking_total' ? ' Только полные цены запроса; условные исключены.' : ''} {distribution.missing_count ? `Нет значения: ${number(distribution.missing_count)}.` : ''} {distribution.excluded_count ? `Исключено: ${number(distribution.excluded_count)}.` : ''}</p>
  </section>
}

export default function DataDashboard({ snapshotId, registrySnapshotId, criteria, onCatalog, registryMode = false, developerMode = false }: { snapshotId?: string; registrySnapshotId?: string | null; criteria: Criterion[]; onCatalog: () => void; registryMode?: boolean; developerMode?: boolean }) {
  const cacheKey = `${snapshotId ?? ''}|${registrySnapshotId ?? ''}|${developerMode ? 'developer' : registryMode ? 'registry' : 'offers'}`
  const [selected, setSelected] = useState<DatasetId>(developerMode ? 'developer_building_registry' : registryMode ? 'building_registry' : 'demo_offers')
  const [report, setReport] = useState<DataCoverageResponse | null>(sessionCacheKey === cacheKey ? sessionCache : null)
  const [loading, setLoading] = useState(!report)
  const [error, setError] = useState<string | null>(null)
  const inFlight = useRef(false)
  const autoStarted = useRef(false)
  const load = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    setLoading(true); setError(null)
    try {
      const next = await api.dataCoverage()
      if (snapshotId && next.snapshot_id !== snapshotId) throw new Error('Каталог обновился. Повторите загрузку данных.')
      if (registryMode && !next.datasets.some((item) => item.dataset_id === (developerMode ? 'developer_building_registry' : 'building_registry')))
        throw new Error('Снимок данных реестра обновился. Повторите загрузку.')
      if (developerMode && next.datasets.some((item) => item.dataset_id === 'building_registry'))
        throw new Error('Новый и прежний реестры не должны смешиваться.')
      sessionCache = next
      sessionCacheKey = cacheKey
      setReport(next)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Не удалось загрузить данные') }
    finally { inFlight.current = false; setLoading(false) }
  }, [snapshotId, cacheKey, registryMode, developerMode])
  useEffect(() => { if (!report && !autoStarted.current) { autoStarted.current = true; void load() } }, [load, report])

  const dataset = report?.datasets.find((item) => item.dataset_id === selected)
  const byKey = useMemo(() => new Map(criteria.map((item) => [item.key, item.label_ru])), [criteria])
  const groupByKey = useMemo(() => new Map(criteria.map((item) => [item.key, item.group])), [criteria])
  const title = DATASETS.find((item) => item.id === selected)!
  const visibleDatasets = registryMode ? DATASETS.filter((item) => item.id === (developerMode ? 'developer_building_registry' : 'building_registry') || item.id === 'city_geometry') : DATASETS.filter((item) => item.id !== 'building_registry' && item.id !== 'developer_building_registry')
  const distributionKeys = new Set(['offer.price_total_rub', 'unit.area_sqm'])
  const distributions = (dataset?.distributions ?? []).filter((item) => distributionKeys.has(item.key) && item.sample_count > 0 && item.bins.length > 0)
  const isCity = selected === 'city_geometry'
  const isRegistry = selected === 'building_registry' || selected === 'developer_building_registry'
  const isDemo = selected === 'demo_offers'
  const visibleFields = (dataset?.fields ?? []).filter((field) => isCity || isRegistry || PRIMARY_OFFER_KEYS.has(field.key))
  const otherFields = isCity || isRegistry ? [] : (dataset?.fields ?? []).filter((field) => !PRIMARY_OFFER_KEYS.has(field.key))
  const otherGroups = [...new Set(otherFields.map((field) => groupByKey.get(field.key) ?? 'other'))]

  return <main className="data-dashboard">
    <div className="data-dashboard-inner"><div className="data-intro"><div><span className="section-kicker"><BarChart3 size={14} /> ДАННЫЕ КАТАЛОГА</span><h1>Что есть в данных</h1><p>{developerMode ? 'Количество корпусов и заполненность полей в снимке квартир. Геопривязка учитывается только после проверки; актуальное наличие квартир и цены не подтверждены.' : registryMode ? 'Количество записей и заполненность полей. Геопривязка учитывается только после проверки; статус и сроки относятся к дате выгрузки.' : 'Количество записей, заполненность полей и распределения по выбранному набору. Оценочные значения не считаются измеренными.'}</p></div><button className="data-back" onClick={onCatalog}>К карте <ArrowRight size={16} /></button></div>
      <div className="dataset-switch" role="tablist" aria-label="Набор данных">{visibleDatasets.map((item) => <button key={item.id} role="tab" aria-selected={selected === item.id} className={selected === item.id ? 'active' : ''} onClick={() => setSelected(item.id)}>{item.label}</button>)}</div>
      {loading && <div className="data-state" role="status"><Database size={26} /><strong>Загружаем цифры…</strong></div>}
      {error && !loading && <div className="data-state error" role="alert"><strong>Не удалось загрузить данные</strong><p>{error}</p><button className="primary-button" onClick={() => void load()}><RefreshCw size={15} /> Повторить</button></div>}
      {dataset && !loading && <><div className="data-source-line"><strong>{title.label}</strong><span>{title.description}</span><span>Снимок: {formattedDate(dataset.snapshot_at)}</span></div>
        <div className="data-stat-grid"><div className="data-stat"><span>{isCity ? 'Контуров зданий' : isRegistry ? 'Записей зданий' : 'Предложений квартир'}</span><strong>{number(dataset.records_total)}</strong><small>{isCity ? 'Геометрия карты, не полный реестр зданий' : isRegistry ? (developerMode ? 'Из исторического снимка квартир; актуальное наличие и цены не подтверждены' : 'Квартиры и цены в выгрузке отсутствуют') : isDemo ? 'Все предложения иллюстративные' : 'Только актуальные записи с разрешённым показом'}</small></div>{!isCity && (!isRegistry || developerMode) && dataset.complexes_total !== undefined && dataset.complexes_total !== null && <div className="data-stat"><span>Жилых комплексов</span><strong>{number(dataset.complexes_total)}</strong><small>{developerMode ? 'Группы ЖК по идентификаторам источника' : 'Среди предложений этого набора'}</small></div>}<div className="data-stat"><span>Полей с отчётом</span><strong>{number(dataset.fields.length)}</strong><small>{isRegistry ? 'Полнота полей исходной выгрузки' : `В реестре ${number(report!.criteria_total)} определений; это не число заполненных полей`}</small></div></div>
        {dataset.records_total === 0 ? <div className="data-state empty"><Database size={27} /><strong>{selected === 'real_current_offers' ? 'Нет подтверждённых актуальных предложений' : 'В этом наборе нет записей'}</strong><p>Покрытие и распределения не рассчитываются при нулевом числе записей.</p></div> : <><section className="coverage-panel"><div className="data-section-heading"><div><span className="section-kicker">ЗАПОЛНЕННОСТЬ</span><h2>Что известно о записях</h2></div><span>{number(dataset.fields.length)} полей</span></div><p>{isRegistry ? (developerMode ? 'ЖК, застройщик и адрес взяты из снимка квартир; точка корпуса учитывается только после проверки.' : 'Поля статуса, сроков и этажности переданы из исторической выгрузки; точка на карте учитывается только после проверки.') : '«Значение указано» включает иллюстративные и оценочные данные; их статус показан отдельно.'}</p><div className="coverage-list">{visibleFields.map((field) => <FieldRow key={field.key} field={field} total={dataset.records_total} label={byKey.get(field.key) ?? FIELD_LABELS[field.key] ?? field.key} />)}</div>{otherFields.length > 0 && <details className="coverage-more"><summary>Остальные критерии — {number(otherFields.length)}</summary>{otherGroups.map((group) => <div className="coverage-group" key={group}><h3>{GROUP_LABELS[group] ?? group}</h3><div className="coverage-list">{otherFields.filter((field) => (groupByKey.get(field.key) ?? 'other') === group).map((field) => <FieldRow key={field.key} field={field} total={dataset.records_total} label={byKey.get(field.key) ?? FIELD_LABELS[field.key] ?? field.key} />)}</div></div>)}</details>}</section>
          {!isCity && !isRegistry && <section className="distribution-panel"><div className="data-section-heading"><div><span className="section-kicker">РАСПРЕДЕЛЕНИЕ</span><h2>Цены и площадь</h2></div></div>{distributions.length ? <div className="distribution-grid">{distributions.map((item) => <DistributionChart key={item.key} distribution={item} demo={isDemo} />)}</div> : <div className="data-state empty"><strong>Распределение не рассчитано</strong><p>Достаточных значений в выбранном наборе нет.</p></div>}</section>}
        </>}
      </>}
    </div>
  </main>
}
