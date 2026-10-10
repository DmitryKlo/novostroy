import type { DeveloperUnitFilterDraft } from './developerUnitFilters'

type Props = {
  draft: DeveloperUnitFilterDraft
  onChange: (key: keyof DeveloperUnitFilterDraft, value: string) => void
  onClear: () => void
  error: string | null
  observedAt: string | null
}

function RangeField({ label, unit, minValue, maxValue, minKey, maxKey, onChange, step = 'any' }: {
  label: string; unit: string; minValue: string; maxValue: string; minKey: keyof DeveloperUnitFilterDraft;
  maxKey: keyof DeveloperUnitFilterDraft; onChange: Props['onChange']; step?: string
}) {
  return <fieldset className="developer-filter-range"><legend>{label} <span>{unit}</span></legend><div>
    <label><span>От</span><input type="number" inputMode="decimal" min="0" step={step} value={minValue} onChange={(event) => onChange(minKey, event.target.value)} aria-label={`${label} от`} placeholder="Любая" /></label>
    <label><span>До</span><input type="number" inputMode="decimal" min="0" step={step} value={maxValue} onChange={(event) => onChange(maxKey, event.target.value)} aria-label={`${label} до`} placeholder="Любая" /></label>
  </div></fieldset>
}

export default function DeveloperFilterPanel({ draft, onChange, onClear, error, observedAt }: Props) {
  return <div className="filters-panel developer-filter-panel" id="developer-filter-panel">
    <p className="developer-filter-scope">Цена на дату сбора данных{observedAt ? ` (${observedAt})` : ''}; наличие и нынешняя цена не подтверждены. Все ограничения должна выполнить одна квартира.</p>
    <div className="developer-filter-grid">
      <RangeField label="Цена" unit="млн ₽, снимок" minValue={draft.priceMinMln} maxValue={draft.priceMaxMln} minKey="priceMinMln" maxKey="priceMaxMln" onChange={onChange} />
      <RangeField label="Цена за м²" unit="тыс ₽, снимок" minValue={draft.pricePerM2MinThousand} maxValue={draft.pricePerM2MaxThousand} minKey="pricePerM2MinThousand" maxKey="pricePerM2MaxThousand" onChange={onChange} />
      <RangeField label="Площадь" unit="м²" minValue={draft.areaMinM2} maxValue={draft.areaMaxM2} minKey="areaMinM2" maxKey="areaMaxM2" onChange={onChange} />
      <RangeField label="Этаж квартиры" unit="по каталогу" minValue={draft.floorMin} maxValue={draft.floorMax} minKey="floorMin" maxKey="floorMax" onChange={onChange} step="1" />
      <label className="developer-filter-rooms">Комнат <select value={draft.rooms} onChange={(event) => onChange('rooms', event.target.value)} aria-label="Комнат"><option value="">Любое число</option>{[0, 1, 2, 3, 4, 5, 6].map((value) => <option key={value} value={value}>{value === 0 ? '0 · студия по источнику' : value}</option>)}</select></label>
    </div>
    <details className="developer-filter-unavailable"><summary>Что пока нельзя отфильтровать</summary><p>Отделка, срок сдачи, статус, общая этажность и точный план этажа не подтверждены для всех домов. Неизвестные значения не считаются совпадением.</p></details>
    {error && <p className="registry-filter-error" role="alert">{error}</p>}
    <div className="filter-buttons"><button className="primary-button" type="submit">Показать дома</button><button className="text-button" type="button" onClick={onClear}>Сбросить</button></div>
  </div>
}
