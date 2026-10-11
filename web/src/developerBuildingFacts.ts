type BuildingFacts = {
  developer_display_name?: string | null
  developer_display_basis?: 'aggregator_asserted_unverified' | 'first_party_source_brand' | 'official_project_brand' | null
  developer_legal_name?: string | null
  total_floors?: number | null
  total_floors_basis?: 'source_apartment_record_building_floors' | 'source_building' | null
  section_floor_counts?: { section_label: string; total_floors: number; total_floors_basis: 'source_section_floor_count' }[]
}

export function developerDisplayCaption(facts: BuildingFacts): string {
  if (facts.developer_display_name) {
    if (facts.developer_display_basis === 'aggregator_asserted_unverified') return `Застройщик по данным каталога: ${facts.developer_display_name}`
    if (facts.developer_display_basis === 'first_party_source_brand') return `Бренд застройщика: ${facts.developer_display_name}`
    if (facts.developer_display_basis === 'official_project_brand') return `Бренд проекта: ${facts.developer_display_name}`
  }
  if (facts.developer_legal_name) return `Юридическое лицо застройщика: ${facts.developer_legal_name}`
  return 'Застройщик не указан в данных источника'
}

export function developerFloorsCaption(facts: BuildingFacts): string {
  if (Number.isInteger(facts.total_floors) && facts.total_floors !== null && facts.total_floors !== undefined
    && facts.total_floors > 0 && (facts.total_floors_basis === 'source_apartment_record_building_floors' || facts.total_floors_basis === 'source_building')
  ) return `Этажей в корпусе по источнику: ${facts.total_floors}`
  const sections = facts.section_floor_counts?.filter((section) => section.section_label && Number.isInteger(section.total_floors)
    && section.total_floors > 0 && section.total_floors_basis === 'source_section_floor_count') ?? []
  if (sections.length) return `Этажность секций по источнику: ${sections.map((section) => `${section.section_label} — ${section.total_floors}`).join(', ')}`
  return 'Этажность корпуса: —'
}
