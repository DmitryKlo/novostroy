import type { DeveloperUnitFilterDraft } from './developerUnitFilters'

type Props = {
  draft: DeveloperUnitFilterDraft
  onChange: (key: keyof DeveloperUnitFilterDraft, value: string) => void
  onClear: () => void
  error: string | null
  observedAt: string | null
  okrugs: { id: string; name: string; count: number }[]
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

export default function DeveloperFilterPanel({ draft, onChange, onClear, error, observedAt, okrugs }: Props) {
  return <div className="filters-panel developer-filter-panel" id="developer-filter-panel">
    <p className="developer-filter-scope">Цена на дату сбора данных{observedAt ? ` (${observedAt})` : ''}; наличие и нынешняя цена не подтверждены. Все ограничения проверяются для одной квартиры.</p>
    <div className="developer-filter-grid">
      <RangeField label="Цена" unit="млн ₽, снимок" minValue={draft.priceMinMln} maxValue={draft.priceMaxMln} minKey="priceMinMln" maxKey="priceMaxMln" onChange={onChange} />
      <RangeField label="Цена за м²" unit="тыс ₽, снимок" minValue={draft.pricePerM2MinThousand} maxValue={draft.pricePerM2MaxThousand} minKey="pricePerM2MinThousand" maxKey="pricePerM2MaxThousand" onChange={onChange} />
      <RangeField label="Площадь" unit="м²" minValue={draft.areaMinM2} maxValue={draft.areaMaxM2} minKey="areaMinM2" maxKey="areaMaxM2" onChange={onChange} />
      <RangeField label="Этаж квартиры" unit="по каталогу" minValue={draft.floorMin} maxValue={draft.floorMax} minKey="floorMin" maxKey="floorMax" onChange={onChange} step="1" />
      <label className="developer-filter-rooms">Комнат <select value={draft.rooms} onChange={(event) => onChange('rooms', event.target.value)} aria-label="Комнат"><option value="">Любое число</option>{[0, 1, 2, 3, 4, 5, 6].map((value) => <option key={value} value={value}>{value === 0 ? '0 · студия по источнику' : value}</option>)}</select></label>
      <label className="developer-filter-rooms">Административный округ <select value={draft.okrugId} onChange={(event) => onChange('okrugId', event.target.value)} aria-label="Административный округ"><option value="">Все округа</option>{okrugs.map((okrug) => <option key={okrug.id} value={okrug.id}>{okrug.name} · {okrug.count}</option>)}{draft.okrugId && !okrugs.some((okrug) => okrug.id === draft.okrugId) && <option value={draft.okrugId}>Выбранный округ</option>}</select></label>
      <div className="developer-noise-filter"><label><input type="checkbox" checked={draft.noiseMax !== ''} onChange={(event) => onChange('noiseMax', event.target.checked ? '100' : '')} /> Ограничить индекс шума</label><div className="developer-noise-range"><input type="range" min="0" max="100" step="1" disabled={draft.noiseMax === ''} value={draft.noiseMax || '100'} onChange={(event) => onChange('noiseMax', event.target.value)} aria-label="Индекс шума до" /><output>{draft.noiseMax === '' ? 'Без ограничения' : `До ${draft.noiseMax} из 100`}</output></div><small>Внутренняя оценка платформы</small></div>
      <div className="developer-noise-filter"><label><input type="checkbox" checked={draft.airMax !== ''} onChange={(event) => onChange('airMax', event.target.checked ? '100' : '')} /> Ограничить индекс загрязнения воздуха</label><div className="developer-noise-range"><input type="range" min="0" max="100" step="1" disabled={draft.airMax === ''} value={draft.airMax || '100'} onChange={(event) => onChange('airMax', event.target.value)} aria-label="Индекс загрязнения воздуха до" /><output>{draft.airMax === '' ? 'Без ограничения' : `До ${draft.airMax} из 100`}</output></div><small>Внутренняя модельная оценка · выше — хуже. При ограничении дома без актуального индекса исключаются.</small></div>
    </div>
    <details className="developer-filter-unavailable"><summary>Что пока нельзя отфильтровать</summary><p>Отделка, срок сдачи, статус, общая этажность и точный план этажа не подтверждены для всех домов. Неизвестные значения не считаются совпадением.</p></details>
    {error && <p className="registry-filter-error" role="alert">{error}</p>}
    <div className="filter-buttons"><button className="primary-button" type="submit">Показать дома</button><button className="text-button" type="button" onClick={onClear}>Сбросить</button></div>
  </div>
}
