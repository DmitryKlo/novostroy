import { useEffect, useState } from 'react'
import type { NearbyCategory, NearbyEducation, NearbySelection } from './api'
import { displayNearbyItems, NEARBY_CATEGORIES, NEARBY_LABELS, nearbyWalkingCaption, originAccessCaption, shownNearbyItems, type NearbyVisibility } from './nearby'
import useMobileViewport from './useMobileViewport'

type Props = {
  selectedName: string | null
  selectedHasPoint: boolean
  visible: NearbyVisibility
  onToggle: (layer: NearbyCategory) => void
  education: NearbyEducation
  onEducation: (value: NearbyEducation) => void
  data: NearbySelection | null
  loading: boolean
  error: string | null
  viewportAvailable: boolean
  viewportCount: number | null
  viewportTruncated: boolean
  viewportLoading: boolean
  viewportError: string | null
  selectedViewportPoiName: string | null
  selectedPoiId: string | null
  onSelectPoi: (id: string) => void
}

const count = (value: number | null) => value == null ? null : new Intl.NumberFormat('ru-RU').format(value)

export default function NearbyPanel({ selectedName, selectedHasPoint, visible, onToggle, education, onEducation, data, loading, error,
  viewportAvailable, viewportCount, viewportTruncated, viewportLoading, viewportError, selectedViewportPoiName, selectedPoiId, onSelectPoi }: Props) {
  const anyVisible = NEARBY_CATEGORIES.some((layer) => visible[layer])
  const shown = shownNearbyItems(data, visible)
  const mobile = useMobileViewport()
  const [expanded, setExpanded] = useState(false)
  useEffect(() => setExpanded(false), [selectedName])
  const compact = mobile && !expanded
  return <section className={compact ? 'nearby-panel nearby-panel-compact' : 'nearby-panel'} aria-label="Объекты рядом с выбранной точкой">
    <div className="nearby-heading"><strong>Рядом на карте</strong><small>{selectedHasPoint ? selectedName : anyVisible ? 'Текущий участок карты' : 'Слои скрыты'}</small><button className="nearby-expand" type="button" aria-expanded={!compact} aria-controls="nearby-panel-body" onClick={() => setExpanded((value) => !value)}>{compact ? `Показать${selectedHasPoint && data ? ` · ${shown.length}` : ''}` : 'Свернуть'}</button></div>
    <div className="nearby-toggles" role="group" aria-label="Слои рядом">
      {NEARBY_CATEGORIES.map((layer) => <button key={layer} type="button" className={visible[layer] ? `nearby-toggle active nearby-${layer}` : 'nearby-toggle'}
        aria-pressed={visible[layer]} onClick={() => onToggle(layer)}><span className={`nearby-swatch nearby-swatch-${layer}`} aria-hidden="true" />{NEARBY_LABELS[layer]}</button>)}
    </div>
    {!compact && <div id="nearby-panel-body">
    <label className="nearby-education">Образование <select value={education} onChange={(event) => onEducation(event.target.value as NearbyEducation)}>
      <option value="all">Школы и детсады</option><option value="school">Только школы</option><option value="kindergarten">Только детсады</option>
    </select></label>
    {!selectedHasPoint && selectedName && <p className="nearby-note">Для этой записи нет подтверждённой точки. Маршрут не рассчитан.</p>}
    {!selectedName && !anyVisible && <p className="nearby-note">Включите слой или выберите точку новостройки для пешеходных маршрутов.</p>}
    {!selectedHasPoint && anyVisible && !viewportAvailable && <p className="nearby-note">Приблизьте карту до района: общий обзор не загружает объекты инфраструктуры.</p>}
    {!selectedHasPoint && anyVisible && viewportAvailable && viewportLoading && <p className="nearby-note" role="status">Загружаем объекты в видимой области…</p>}
    {!selectedHasPoint && anyVisible && viewportAvailable && viewportError && <p className="nearby-note nearby-error" role="alert">{viewportError}</p>}
    {!selectedHasPoint && anyVisible && viewportCount !== null && !viewportLoading && !viewportError && <p className="nearby-note">
      На видимом участке: {count(viewportCount)} объектов{viewportTruncated ? ' (показан ограниченный набор)' : ''}.
      {selectedViewportPoiName ? ` Выбрано: ${selectedViewportPoiName}.` : ''} Выберите точку новостройки для маршрутов и времени пешком.
    </p>}
    {selectedHasPoint && !anyVisible && <p className="nearby-note">Слои скрыты. Включите нужную категорию.</p>}
    {selectedHasPoint && anyVisible && loading && <p className="nearby-note" role="status">Ищем ближайшие объекты и маршруты…</p>}
    {selectedHasPoint && anyVisible && error && <p className="nearby-note nearby-error" role="alert">{error}</p>}
    {selectedHasPoint && anyVisible && data && !loading && !error && <>
      <div className="nearby-results">
        {NEARBY_CATEGORIES.filter((layer) => visible[layer]).map((layer) => <div className="nearby-group" key={layer}>
          <h3>{layer === 'medical' && data.layers.medical.selection_policy === 'nearest_local_plus_verified_oms' ? 'Местная клиника и адрес ОМС' : NEARBY_LABELS[layer]} <span>{displayNearbyItems(layer, data.layers[layer]?.items ?? []).length}</span></h3>
          {displayNearbyItems(layer, data.layers[layer]?.items ?? []).length === 0 && <p className="nearby-empty">В подключённых данных подходящие объекты не найдены.</p>}
          {displayNearbyItems(layer, data.layers[layer]?.items ?? []).map((item) => <button key={item.id} type="button" className={selectedPoiId === item.id ? 'nearby-item selected' : 'nearby-item'}
            onClick={() => onSelectPoi(item.id)} aria-pressed={selectedPoiId === item.id}>
            <span className="nearby-item-title"><b>{item.name}</b>{item.category === 'school' ? <em>школа</em> : item.category === 'kindergarten' ? <em>детсад</em> : null}</span>
            <span>{nearbyWalkingCaption(layer, item)}</span>
            {originAccessCaption(item) && <small className="nearby-origin-access">{originAccessCaption(item)}</small>}
            {item.free_oms.status === 'verified_program_participant' && <small>Участие в ОМС подтверждено; доступность услуги уточните.</small>}
          </button>)}
        </div>)}
      </div>
      {visible.medical && data.layers.medical.selection_policy === 'nearest_local_plus_verified_oms' && <p className="nearby-note">Подтверждённый адрес ОМС добавлен к местным результатам. Он может быть далеко: проверенных адресов пока мало. Это не доказательство, что ближе нет помощи по ОМС; доступность конкретной услуги уточните.</p>}
      {visible.medical && data.free_clinic_guarantee.status === 'unavailable' && <p className="nearby-note">Не удалось подтвердить клинику ОМС в этой выборке. Это не означает, что бесплатных клиник рядом нет.</p>}
      {shown.length > 0 && <p className="nearby-note">До двух станций метро, клиник, школ и детсадов каждого вида. Местные объекты выбраны по рассчитанной длине пути из ближайших кандидатов; для объектов без маршрута близость остаётся неизвестной. Время указано целыми минутами от точки на карте; вход в здание не подтверждён.</p>}
      <p className="nearby-credit">{data.attribution}</p>
    </>}
    </div>}
  </section>
}
