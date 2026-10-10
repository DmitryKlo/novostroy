import type { Criterion, Fact } from './api'
import { criterionLabel } from './AdvancedFilters'

const PRESENT = new Set(['observed', 'derived', 'illustrative', 'estimated'])
const STATUS: Record<string, string> = {
  observed: 'По данным источника', derived: 'Расчёт по данным', illustrative: 'Учебный пример',
  estimated: 'Оценка, не проверена', unknown: 'Нет проверенных данных',
  stale: 'Сведения устарели', conflict: 'Данные расходятся', not_applicable: 'Не относится',
}
const UNIT: Record<string, string> = {
  'RUB': '₽', 'RUB/m2': '₽/м²', 'm²': 'м²', 'm': 'м', 's': 'мин',
  'year': 'г.', 'room': 'комн.', 'floor': 'эт.', 'bathroom': 'санузла',
  'elevator': 'лифта', 'apartment/floor': 'кв./эт.', 'mapped_features_within_500m': 'на карте в 500 м',
}

function formatValue(fact: Fact): string {
  if (!PRESENT.has(fact.status) || fact.value === null || fact.value === undefined) return STATUS[fact.status] ?? 'Не проверено'
  if (typeof fact.value === 'boolean') return fact.value ? 'Да' : 'Нет'
  if (typeof fact.value === 'number') return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(fact.unit === 's' ? fact.value / 60 : fact.value)
  if (typeof fact.value === 'string') return fact.value
  if (Array.isArray(fact.value)) return fact.value.filter((item): item is string | number => typeof item === 'string' || typeof item === 'number').slice(0, 4).join(', ') || 'Значение указано'
  if (typeof fact.value === 'object') {
    const ratio = fact.value as { numerator_rub?: unknown; denominator_sqm?: unknown }
    const denominator = Number(ratio.denominator_sqm)
    if (typeof ratio.numerator_rub === 'number' && Number.isFinite(denominator) && denominator > 0) {
      return `≈ ${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(ratio.numerator_rub / denominator)}`
    }
  }
  return 'Значение указано'
}

function dateOf(value: string | null | undefined): string | null {
  if (!value) return null
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? null : new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' }).format(parsed)
}

export function FactValue({ fact, criterion }: { fact?: Fact; criterion?: Criterion }) {
  const actual: Fact = fact ?? { value: null, status: 'unknown' }
  const present = PRESENT.has(actual.status) && actual.value !== null && actual.value !== undefined
  const unit = actual.unit ?? criterion?.unit
  const shownUnit = unit && UNIT[unit] ? UNIT[unit] : null
  const date = dateOf(actual.observed_at)
  const coverage = actual.coverage_status === 'partial' || actual.coverage_status === 'unknown' ? 'неполное покрытие' : null
  const approximation = actual.approximate ? 'приблизительно' : null
  return <span className={`fact-value ${['estimated', 'unknown', 'stale', 'conflict', 'not_applicable'].includes(actual.status) ? 'unverified' : ''}`}>
    <strong>{formatValue({ ...actual, unit })}{present && shownUnit && typeof actual.value !== 'boolean' ? ` ${shownUnit}` : ''}</strong>
    <small>{STATUS[actual.status] ?? 'Не проверено'}{date ? ` · ${date}` : ''}{coverage ? ` · ${coverage}` : ''}{approximation ? ` · ${approximation}` : ''}</small>
  </span>
}

export default function FactView({ facts, criteria, keys, limit = 3 }: { facts?: Record<string, Fact>; criteria: Criterion[]; keys?: string[]; limit?: number }) {
  const byKey = new Map(criteria.map((criterion) => [criterion.key, criterion]))
  const chosen = keys?.length ? keys : Object.entries(facts ?? {}).filter(([, fact]) => PRESENT.has(fact.status) && fact.value !== null).map(([key]) => key).slice(0, limit)
  if (!chosen.length) return null
  return <div className="fact-list">{chosen.slice(0, limit).map((key) => <div className="fact-row" key={key}><span className="fact-name">{byKey.has(key) ? criterionLabel(byKey.get(key)!) : key}</span><FactValue fact={facts?.[key]} criterion={byKey.get(key)} /></div>)}</div>
}
