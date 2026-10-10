import { useMemo, useState } from 'react'
import { ChevronDown, Search } from 'lucide-react'
import type { Criterion, Predicate } from './api'

type Props = {
  criteria: Criterion[]
  predicates: Predicate[]
  onChange: (predicates: Predicate[]) => void
}

const GROUP_NAMES: Record<string, string> = { property: 'Квартира и дом', social: 'Рядом с домом', environment: 'Окружение' }
const ENUM_NAMES: Record<string, string> = { underground: 'Подземная', surface: 'Наземная', none: 'Нет', cash: 'Наличные', mortgage: 'Ипотека', installment: 'Рассрочка', combined: 'Совмещённый', separate: 'Раздельный', gas: 'Газовая', electric: 'Электрическая' }
const BASE_KEYS = new Set(['offer.price_total_rub', 'unit.area_sqm', 'unit.rooms'])

export function criterionLabel(criterion: Criterion): string {
  return criterion.label_ru && criterion.label_ru !== criterion.key ? criterion.label_ru : criterion.key
}

function unitSpec(criterion: Criterion): { label: string; scale: number; step: string } {
  if (criterion.unit === 's') return { label: 'мин', scale: 60, step: '0.5' }
  if (criterion.unit === 'RUB') return { label: 'млн ₽', scale: 1_000_000, step: '0.1' }
  if (criterion.unit === 'RUB/m2') return { label: 'тыс ₽/м²', scale: 1000, step: '1' }
  if (criterion.unit === 'm²') return { label: 'м²', scale: 1, step: '0.1' }
  if (criterion.unit === 'm') return { label: 'м', scale: 1, step: '0.1' }
  if (criterion.unit === 'boolean') return { label: '', scale: 1, step: '1' }
  if (criterion.unit === 'apartment/floor') return { label: 'кв./эт.', scale: 1, step: '1' }
  if (criterion.unit === 'mapped_features_within_500m') return { label: 'объектов на карте в 500 м', scale: 1, step: '1' }
  if (criterion.unit === 'bathroom') return { label: 'санузлов', scale: 1, step: '1' }
  if (criterion.unit === 'elevator') return { label: 'лифтов', scale: 1, step: '1' }
  if (criterion.unit === 'floor') return { label: 'этаж', scale: 1, step: '1' }
  if (criterion.unit === 'year') return { label: 'год', scale: 1, step: '1' }
  if (criterion.unit === 'room') return { label: 'комнат', scale: 1, step: '1' }
  return { label: criterion.unit ?? '', scale: 1, step: criterion.value_type === 'integer' ? '1' : '0.1' }
}

function CriterionControl({ criterion, predicates, onChange }: Omit<Props, 'criteria'> & { criterion: Criterion }) {
  const active = predicates.filter((item) => item.key === criterion.key)
  const spec = unitSpec(criterion)
  const get = (op: Predicate['op']) => active.find((item) => item.op === op)?.value
  const put = (op: Predicate['op'], value: Predicate['value'] | undefined) => {
    const rest = predicates.filter((item) => !(item.key === criterion.key && item.op === op))
    if (value === undefined || (Array.isArray(value) && value.length === 0)) { onChange(rest); return }
    if (rest.length >= 24) return
    onChange([...rest, { key: criterion.key, op, value }])
  }
  const blocked = criterion.availability === 'blocked'
  const note = blocked ? 'Источник для этого условия пока не подключён; совпадений может не быть.' : criterion.coverage_requirement === 'complete' ? 'Для совпадения требуется полное покрытие данных.' : null

  return <div className="criterion-control" data-testid={`criterion-${criterion.key}`}>
    <div className="criterion-title"><label>{criterionLabel(criterion)}</label>{active.length > 0 && <span className="criterion-active">задано</span>}</div>
    {criterion.value_type === 'boolean' ? <select aria-label={criterionLabel(criterion)} value={get('equals') === true ? 'yes' : get('equals') === false ? 'no' : ''} onChange={(event) => put('equals', event.target.value === '' ? undefined : event.target.value === 'yes')}><option value="">Любое значение</option><option value="yes">Да</option><option value="no">Нет</option></select>
      : ['integer', 'decimal', 'ratio'].includes(criterion.value_type) && (criterion.operators.includes('min') || criterion.operators.includes('max')) ? <div className="criterion-range">
        {(['min', 'max'] as const).filter((op) => criterion.operators.includes(op)).map((op) => <label key={op}><span>{op === 'min' ? 'От' : 'До'}{spec.label ? `, ${spec.label}` : ''}</span><input aria-label={`${criterionLabel(criterion)} ${op === 'min' ? 'от' : 'до'}`} type="number" min="0" step={spec.step} value={typeof get(op) === 'number' || typeof get(op) === 'string' ? Number(get(op)) / spec.scale : ''} onChange={(event) => put(op, event.target.value === '' ? undefined : Number(event.target.value) * spec.scale)} /></label>)}</div>
      : criterion.enum_values.length > 0 ? criterion.operators.includes('in') ? <div className="criterion-options">{criterion.enum_values.map((option) => { const current = get('in'); const selected = Array.isArray(current) ? current.map(String) : []; const checked = selected.includes(option); return <label key={option}><input type="checkbox" checked={checked} onChange={() => put('in', checked ? selected.filter((item) => item !== option) : [...selected, option])} />{ENUM_NAMES[option] ?? option}</label> })}</div> : <select aria-label={criterionLabel(criterion)} value={String(get('equals') ?? '')} onChange={(event) => put('equals', event.target.value || undefined)}><option value="">Любое значение</option>{criterion.enum_values.map((option) => <option key={option} value={option}>{ENUM_NAMES[option] ?? option}</option>)}</select>
        : <input className="criterion-text" aria-label={criterionLabel(criterion)} type="text" value={String(get(criterion.operators.includes('contains') ? 'contains' : 'equals') ?? '')} placeholder="Укажите значение" onChange={(event) => put(criterion.operators.includes('contains') ? 'contains' : 'equals', event.target.value || undefined)} />}
    {note && <small className="criterion-note">{note}</small>}
  </div>
}

export default function AdvancedFilters({ criteria, predicates, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const visible = useMemo(() => criteria.filter((criterion) => !BASE_KEYS.has(criterion.key) && criterion.filterable && criterion.operators.length > 0 && criterionLabel(criterion).toLocaleLowerCase('ru').includes(search.toLocaleLowerCase('ru'))), [criteria, search])
  const groups = [...new Set(visible.map((criterion) => criterion.group))]
  if (criteria.length === 0) return null
  return <section className="advanced-filters"><button type="button" className="advanced-toggle" aria-expanded={open} onClick={() => setOpen((value) => !value)}>Дополнительные критерии <span>{predicates.length > 0 ? `${predicates.length} выбрано` : `${visible.length} условий`}</span><ChevronDown size={15} className={open ? 'up' : ''} /></button>{open && <div className="advanced-body"><p>Условия строгие: квартира без подходящего значения не попадёт в результаты. Реестр описывает возможные поля, а не наличие данных по каждой квартире.</p><label className="criteria-search"><Search size={14} /><input aria-label="Найти критерий" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Найти критерий" /></label><div className="criteria-scroll">{groups.map((group) => <div className="criteria-group" key={group}><h4>{GROUP_NAMES[group] ?? group}</h4>{visible.filter((criterion) => criterion.group === group).map((criterion) => <CriterionControl key={criterion.key} criterion={criterion} predicates={predicates} onChange={onChange} />)}</div>)}{visible.length === 0 && <span className="criteria-none">Критерии не найдены</span>}</div>{predicates.length >= 24 && <p className="criteria-limit">Можно задать до 24 условий.</p>}</div>}</section>
}
