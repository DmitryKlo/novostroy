export type DataMode = 'demo' | 'real'
export type ModelQuality = 'synthetic_massing' | 'synthetic_facade' | 'licensed_facade' | 'cartographic_ribbon' | 'cartographic_surface' | 'none'
export type Predicate = { key: string; op: 'min' | 'max' | 'equals' | 'in' | 'contains'; value: number | string | boolean | string[] | number[] }
export type Fact = { value: unknown; unit?: string | null; status: string; scope?: string | null; observed_at?: string | null; coverage_status?: string | null; approximate?: boolean | null; method?: string | null }
export type Criterion = { key: string; label_ru: string; group: 'property' | 'social' | 'environment' | string; type: string; value_type: 'boolean' | 'decimal' | 'enum' | 'integer' | 'object' | 'object_array' | 'ratio' | 'string' | 'string_array'; unit: string | null; scope?: string; operators: Predicate['op'][]; enum_values: string[]; availability: 'blocked' | 'conditional' | 'source_dependent' | string; filterable: boolean; coverage_requirement: string }
export type CriteriaResponse = { snapshot_id: string; data_mode: DataMode; version: '1.1'; criteria: Criterion[] }
export type Source = {
  name: string
  quality: 'illustrative' | 'dated_asking' | 'unknown'
  record_mode: string
  observed_at: string
  source_updated_at?: string | null
  url?: string | null
}
export type Offer = {
  offer_id: string
  unit_id: string | null
  complex_id: string
  district_id: string | null
  rooms: number | null
  area_sqm: number | null
  floor?: number | null
  price: { amount_rub: number; basis: 'asking_total' | 'conditional_total'; observed_at: string; conditions?: string | null } | null
  source: Source
  availability: 'available' | 'unavailable' | 'unknown' | null
  recommendation?: { score: number | null; explanation: string[]; coverage: { status: string } }
  facts?: Record<string, Fact>
}
export type District = { district_id: string; name: string; boundary_version: string }
export type Complex = {
  complex_id: string
  name: string
  district_id: string | null
  location: [number, number]
  developer?: string | null
  model_quality?: ModelQuality
  min_price_rub?: number | null
  offer_count?: number
  tileset_url?: string | null
  source?: Source
  facts?: Record<string, Fact>
}
export type Tileset = { id: string; url: string; kind: 'ground_buildings' | 'city_volumes' | 'facade' | 'roads' | 'parks' | 'water'; model_quality: ModelQuality; label: string; attribution?: string | null }
export type MapMarker = { id: string; point_type: 'registry' | 'mapped_construction' | 'developer_building'; name: string; label?: string; location: [number, number]; fallback_group_id?: string }
  | { id: string; point_type: 'complex_group'; name: string; location: [number, number]; group_member_locations: [number, number][] }
export type MapData = { snapshot_id: string; data_mode: DataMode; center: [number, number]; tilesets: Tileset[]; complexes: Complex[]; markers?: MapMarker[]; counts: Record<string, number>; applied_filters?: Filters }
export type Filters = { budget_min_rub?: number; budget_max_rub?: number; district_ids?: string[]; complex_ids?: string[]; rooms?: number[]; area_min_sqm?: number; area_max_sqm?: number; price_basis?: 'asking_total' | 'conditional_total'; predicates?: Predicate[]; limit?: number }
export type Query = { query?: string; filters?: Filters; preferences?: { infrastructure_categories?: string[] } }
export type SearchResponse = { snapshot_id: string; data_mode: DataMode; items: Offer[]; count: number; applied_filters: Filters; clarification?: { field: string; code: string; message: string; options?: { district_id?: string; name?: string; label?: string }[] } | null; coverage: { status: string } }
export type ListResponse<T> = { snapshot_id: string; data_mode: DataMode; items: T[]; count: number }
export type Health = { api_version: string; snapshot_id: string; snapshot_at: string; data_mode: DataMode; status: string; counts: Record<string, number>; catalog_view?: 'building_registry' | 'developer_buildings' | 'demo' | 'real'; registry_enabled?: boolean; registry_snapshot_id?: string | null; map_run_id?: string | null; unit_run_id?: string | null; enrichment_run_id?: string | null; overlay_run_id?: string | null }
export type DeveloperMedia = { media_id: string; scope: 'unit_plan' | 'floor_plan' | 'project_gallery' | 'project_gallery_single_corpus'; role: 'apartment_plan' | 'furnished_apartment_plan' | 'floor_plan' | 'project_rendering' | 'facade_photo'; label: string | null; observed_at: string | null; asset_url: string; availability: 'prepared' | 'on_demand' }
export type DeveloperAirQuality = { status: 'modeled_forecast' | 'unavailable'; label?: string | null; pm2_5_ug_m3?: number | null; pm10_ug_m3?: number | null; no2_ug_m3?: number | null; valid_at?: string | null; retrieved_at?: string | null; spatial_scope?: string | null; cell_resolution_km_approx?: number | null; stale?: boolean; measurement?: false; measured_district_average?: false; data_credit?: string | null; reason?: string | null; internal_index?: { status: 'modeled_forecast' | 'stale' | 'incomplete' | 'unavailable'; value: number | null; scale_id: 'internal_3pollutant_0_100_v1'; higher_is_worse: true; component_scores: { pm2_5: number | null; pm10: number | null; no2: number | null } | null; dominant_pollutants: ('pm2_5' | 'pm10' | 'no2')[]; official_index?: false; measurement?: false } | null }
export type RegistryBuilding = { registry_id: string; point_id?: string; point_type?: 'developer_building'; enrichment_run_id?: string | null; name: string; record_kind: string | null; city: string; address: string | null; address_complete?: boolean; complex_label?: string | null; developer_name?: string | null; developer_display_name?: string | null; developer_display_basis?: 'aggregator_asserted_unverified' | 'first_party_source_brand' | 'official_project_brand' | null; developer_legal_name?: string | null; total_floors?: number | null; total_floors_basis?: 'source_apartment_record_building_floors' | 'source_building' | null; max_observed_unit_floor?: number | null; section_floor_counts?: { section_label: string; total_floors: number; total_floors_basis: 'source_section_floor_count' }[]; unit_count?: number | null; matching_units_count?: number | null; observed_at?: string | null; location_basis?: string | null; okrug_id?: string | null; okrug_name?: string | null; district_id?: string | null; district_name?: string | null; noise_index?: number | null; air_quality?: DeveloperAirQuality | null; media?: DeveloperMedia[]; location: [number, number] | null; location_quality: 'verified' | null; latest_known_date: string | null; status: string | null; handover: string | null; floors?: number | string | null; building_type?: string | null; offer_count: 0; price: null }
export type RegistryFilters = { query?: string; status?: string; kind?: string; district?: string; okrug_id?: string; max_noise_index?: number; max_air_index?: number; handover_year?: number; handover_quarter?: number; floors_min?: number; floors_max?: number; walk_metro_max_min?: number; walk_school_max_min?: number; walk_kindergarten_max_min?: number; walk_medical_max_min?: number; price_min_rub?: number; price_max_rub?: number; price_per_m2_min_rub?: number; price_per_m2_max_rub?: number; rooms?: number; area_min_m2?: number; area_max_m2?: number; unit_floor_min?: number; unit_floor_max?: number; limit?: number; offset?: number }
export type DeveloperUnitCriteria = Pick<RegistryFilters, 'price_min_rub' | 'price_max_rub' | 'price_per_m2_min_rub' | 'price_per_m2_max_rub' | 'rooms' | 'area_min_m2' | 'area_max_m2' | 'unit_floor_min' | 'unit_floor_max' | 'district' | 'okrug_id' | 'max_noise_index' | 'max_air_index'>
export type RegistryList = { dataset_id: string; snapshot_id: string; map_run_id?: string; unit_run_id?: string; enrichment_run_id?: string | null; overlay_run_id?: string | null; snapshot_at: string; dataset_total?: number; source_total: number; archived_total: number; records_total: number; facets?: { statuses?: { value: string; count: number }[]; kinds?: { value: string; count: number }[]; developers?: { value: string; count: number }[]; map_eligible_count?: number; unlocated_count?: number; okrugs?: { id: string; name: string; count: number }[]; noise_known_count?: number; districts?: { value?: string; id?: string; name?: string; count: number }[]; district_coverage_count?: number; district_source_release?: string; walking_origin_count?: number; handover_years?: { value: string; count: number }[]; numeric_floors_count?: number }; items: RegistryBuilding[] }
export type RegistryMap = { dataset_id: string; snapshot_id: string; map_run_id?: string; unit_run_id?: string; enrichment_run_id?: string | null; overlay_run_id?: string | null; snapshot_at: string; center: [number, number]; items: RegistryBuilding[]; counts: { records: number; geocoded: number; source_records: number; archived_records: number }; tilesets: Tileset[] }
export type DeveloperUnitIdentity = { registry_snapshot_id: string; map_run_id: string; unit_run_id: string; enrichment_run_id?: string | null; overlay_run_id?: string | null; registry_id: string }
export type DeveloperPlan = { role: string; asset_url: string; asset_sha256: string; mime_type: string; geometry_status: 'source_plan_image' }
export type DeveloperPlanRegion = { region_id: string; polygon: [number, number][]; coordinate_frame: 'image_normalized_top_left'; source_label: string | null; source_rooms: string | null; source_area_m2: number | null; unit_id: null; match_status: 'unconfirmed'; basis: 'raster_traced_floor_plan'; reconstruction: true; unit_action: false }
export type DeveloperFloor = { floor_id: string; floor_number: number; unit_count: number; source_unit_count?: number; geometry_status: 'none' | 'source_plan_image'; plan: DeveloperPlan | null }
export type DeveloperFloorList = DeveloperUnitIdentity & { observed_at: string; total_floors: number | null; total_floors_basis?: 'source_apartment_record_building_floors' | 'source_building' | null; developer_display_name?: string | null; developer_display_basis?: 'aggregator_asserted_unverified' | 'first_party_source_brand' | 'official_project_brand' | null; developer_legal_name?: string | null; max_observed_unit_floor: number | null; sections: { section_id: string | null; section_label: string | null; total_floors?: number | null; total_floors_basis?: 'source_section_floor_count' | null; floors: DeveloperFloor[] }[]; unassigned_unit_count: number; source_unassigned_unit_count?: number; matching_units_total?: number; applied_filters?: DeveloperUnitCriteria }
export type DeveloperFloorDetail = DeveloperUnitIdentity & DeveloperFloor & { section_id: string | null; section_label: string | null; hotspots: { unit_id: string; polygon: [number, number][] }[]; plan_regions?: DeveloperPlanRegion[]; media?: DeveloperMedia[]; applied_filters?: DeveloperUnitCriteria }
export type DeveloperUnit = { unit_id: string; unit_label: string | null; section_id: string | null; section_label: string | null; floor_id: string | null; floor_number: number | null; rooms_label: string | null; area_m2: number | null; price_rub: number | null; price_context: 'payment_100_percent' | 'promotion_or_conditions' | 'source_base_amount' | 'source_displayed_total' | 'conditions_unverified'; observed_at: string; record_updated_at: string | null; availability: 'unknown'; has_prepared_plan: boolean }
export type DeveloperUnitList = DeveloperUnitIdentity & { total: number; limit: number; offset: number; applied_filters?: DeveloperUnitCriteria; items: DeveloperUnit[] }
export type DeveloperUnitDetail = DeveloperUnitIdentity & DeveloperUnit & { plans: DeveloperPlan[]; media?: DeveloperMedia[]; observed_attributes?: { observed_at: string; record_updated_at: string | null; finishing_label: string | null; delivery_deadline: string | null; source_sale_status: string | null; previous_price_rub: number | null; discount_percent: number | null; availability_confirmed_current: false; payment_terms_confirmed_unconditional: false } | null; applied_filters?: DeveloperUnitCriteria }
export type DeveloperModel = DeveloperUnitIdentity & { model_run_id: string | null; status: 'ready' | 'unavailable'; depiction: 'schematic_massing' | null; label: string | null; height_m: number | null; height_basis: string | null; geometry_basis: string | null; geometry_attribution?: string | null; photo_facade_reconstruction?: boolean; texture_basis?: 'generated_photo_informed_unoriented' | 'generated_photo_informed_repeat_unoriented' | 'generated_project_style_repeat_unoriented' | null; facade_style_basis?: 'project_style_inference' | null; model_transform: number[] | null; asset_url: string | null; asset_sha256: string | null }
export type DeveloperParkWalk = DeveloperUnitIdentity & { status: 'routed' | 'unavailable'; park: { id: string; name: string | null } | null; distance_m: number | null; estimated_minutes: number | null; geometry: [number, number][] | null; approximate: true; endpoint_basis: 'pedestrian_graph_near_park_boundary' | null; boundary_gap_m: number | null; access_verified: false; origin_access: { status: 'estimated'; approximate: true; distance_m: number; estimated_minutes: number; geometry: [number, number][] } | null; osm_snapshot_at: string; attribution: string }
export type MapPointLabel = {
  address: string | null; address_basis: string | null;
  address_complete?: boolean;
  complex_label: string | null; complex_label_basis?: 'registry_source_name' | 'osm_addr_place' | 'osm_name' | 'osm_parent_relation' | null;
  known_catalog_registry_id?: string | null;
}
export type MapPoint = MapPointLabel & ({ point_id: string; point_type: 'registry'; registry_id: string; candidate_id: null; name: string; location: [number, number]; latest_known_date: string | null; geometry_as_of: null }
  | { point_id: string; point_type: 'mapped_construction'; registry_id: null; candidate_id: string; name: string; location: [number, number]; latest_known_date: null; geometry_as_of: string | null }
  | { point_id: string; point_type: 'developer_building'; registry_id: string; candidate_id?: null; enrichment_run_id?: string | null; name: string; location: [number, number]; latest_known_date?: string | null; geometry_as_of?: null; developer_name: string | null; developer_display_name?: string | null; developer_display_basis?: 'aggregator_asserted_unverified' | 'first_party_source_brand' | 'official_project_brand' | null; developer_legal_name?: string | null; total_floors?: number | null; total_floors_basis?: 'source_apartment_record_building_floors' | 'source_building' | null; max_observed_unit_floor?: number | null; section_floor_counts?: { section_label: string; total_floors: number; total_floors_basis: 'source_section_floor_count' }[]; unit_count: number | null; observed_at: string | null; location_basis: string | null; okrug_id?: string | null; okrug_name?: string | null; district_id?: string | null; district_name?: string | null; noise_index?: number | null; air_quality?: DeveloperAirQuality | null; media?: DeveloperMedia[] })
export type MapPointsResult = { dataset_id: 'map_points' | 'developer_map_points'; registry_snapshot_id: string; model_run_id?: string; map_run_id?: string; unit_run_id?: string; enrichment_run_id?: string | null; overlay_run_id?: string | null; snapshot_at: string; checked_at?: string; registry_rights_status?: 'user_supplied_internal_use'; mapped_attribution?: string; dataset_total: number; registry_source_total?: number; registry_archived_total?: number; counts: { points: number; registry_points: number; registry_points_total: number; mapped_construction_points: number; coordinate_positions: number; excluded_mapped_by_registry_filters: number }; applied_filters: Omit<RegistryFilters, 'limit' | 'offset'>; items: MapPoint[]; group_evidence: import('./mapPointGroups').MapPointGroupEvidenceBundle }
export type NearbyCategory = 'metro' | 'education' | 'medical'
export type NearbyEducation = 'all' | 'school' | 'kindergarten'
export type NearbyPoi = {
  id: string; name: string; category: 'metro_station' | 'school' | 'kindergarten' | 'clinic';
  coordinate: { lon: number; lat: number }; coordinate_basis: string; straight_line_m: number | null;
  route: { status: string; distance_m: number | null; estimated_minutes: number | null; geometry: [number, number][] | null; approximate: true; endpoint_note?: string; endpoint_coordinate?: { lon: number; lat: number } | null; estimate_basis?: string;
    origin_access?: { status: 'estimated'; approximate: true; geometry: [number, number][]; distance_m: number; estimated_minutes: number; segment_osm_source_id?: string | null; access_coordinate?: { lon: number; lat: number } | null } | null;
    network_geometry?: [number, number][] | null; network_distance_m?: number | null; network_estimated_minutes?: number | null } | null;
  free_oms: { status: 'verified_program_participant' | 'unknown'; scope: string | null; official_snapshot_at?: string | null };
  metro_access_link?: string | null;
  osm_updated_at?: string | null;
}
export type NearbyLayer = { items: NearbyPoi[]; ranking_basis?: string; selection_policy?: string; required_clinic_included?: boolean; candidate_count?: number; coverage: string; attribution: string }
export type NearbySelection = { status: 'ok'; point_id: string; education: NearbyEducation; origin: { coordinate: { lon: number; lat: number }; access_kind: string }; dataset_id: string; osm_snapshot_at: string; layers: Record<NearbyCategory, NearbyLayer>; free_clinic_guarantee: { status: 'met' | 'unavailable'; scope: string }; coverage: Record<string, unknown>; attribution: string; registry_snapshot_id: string; model_run_id?: string; map_run_id?: string }
export type NearbyViewport = { status: 'ok'; dataset_id: string; osm_snapshot_at: string; education: NearbyEducation; bbox: [number, number, number, number]; layers: Partial<Record<NearbyCategory, NearbyLayer>>; returned_count: number; truncated: boolean; coverage: Record<string, unknown>; attribution: string; registry_snapshot_id: string; model_run_id?: string; map_run_id?: string }
export type ModelCandidateDecision = 'ready' | 'review' | 'rejected' | 'unavailable'
export type ModelCandidateCounts = { scanned: number; ready: number; review: number; rejected: number; unavailable: number }
export type ModelCandidateMeta = { dataset_id: 'model_candidates'; registry_snapshot_id: string; model_run_id: string; checked_at: string }
export type ModelCandidateSummary = ModelCandidateMeta & { run_status: 'ready'; counts: ModelCandidateCounts; ready_depictions?: Partial<Record<'construction_outline' | 'project_footprint' | 'schematic_massing' | 'licensed_exterior', number>>; facets: { reason_codes: { value: string; count: number }[]; source_groups: { value: string; count: number }[] } }
export type ProjectModelTileset = { url: string; kind: 'project_model' | 'mapped_construction'; model_quality: string; attribution: string }
export type ModelHeightEvidence = { mapped_height_m: number | null; mapped_levels: number | null; estimated_render_height_m: number | null; render_height_m: number | null; render_height_basis: 'mapped_height_tag' | 'mapped_floors_times_3m' | null }
export type ModelCandidateAttempt = { code: string; label_ru: string; result_ru: string; count: number | null }
export type ModelCandidate = {
  candidate_id: string; registry_id: string | null; name: string; source_group: 'registry' | 'mapped_construction' | 'external_event'; entity_type: string;
  decision: ModelCandidateDecision; asset_readiness: 'ready_to_render' | 'review' | 'unavailable'; project_link_status: 'verified' | 'review' | 'unlinked'; ready_to_render: boolean;
  depiction: string | null; model_quality: string | null; geometry_quality: string | null; geometry_as_of: string | null;
  reason_codes: string[]; explanation_ru: string; next_step_ru: string;
  geometry_confidence?: number | null; height_evidence: ModelHeightEvidence; render_height_m: number | null;
}
export type ModelCandidateDetailItem = ModelCandidate & { model_tileset: ProjectModelTileset | null; attempts: ModelCandidateAttempt[] }
export type ModelCandidateFilters = { query?: string; decision?: ModelCandidateDecision; reason_code?: string; source_group?: string; registry_id?: string; limit?: number; offset?: number }
export type ModelCandidateList = ModelCandidateMeta & { dataset_total: number; records_total: number; limit: number; offset: number; items: ModelCandidate[] }
export type ModelCandidateDetail = ModelCandidateMeta & { item: ModelCandidateDetailItem }
export type ModelPreviewGeometry = { type: 'Polygon'; coordinates: [number, number][][] } | { type: 'MultiPolygon'; coordinates: [number, number][][][] }
export type ModelCandidateGeometry = ModelCandidateMeta & { candidate_id: string; center: [number, number] | null; bbox: [number, number, number, number] | null; footprint: [number, number][] | null; preview_geometry: ModelPreviewGeometry | null; render_height_m: number | null; height_evidence: ModelHeightEvidence; model_tileset: ProjectModelTileset; parts?: { footprint: [number, number][]; height_m?: number | null }[] | null }
export type CompareResponse = { snapshot_id: string; data_mode: DataMode; items: Offer[]; fields: unknown[] }
export type CoverageField = { key: string; unit: string | null; populated_count: number; unknown_count: number; stale_count: number; quality_counts: { observed: number; derived: number; estimated: number; illustrative: number; render_fallback: number } }
export type DistributionBin = { lower: number; upper: number; upper_inclusive: boolean; count: number }
export type Distribution = { key: 'offer.price_total_rub' | 'unit.area_sqm'; unit: 'RUB' | 'm²'; basis: 'asking_total' | 'total_area'; population_count: number; sample_count: number; missing_count: number; excluded_count: number; bins: DistributionBin[] }
export type CoverageDataset = { dataset_id: 'demo_offers' | 'real_current_offers' | 'city_geometry' | 'building_registry' | 'developer_building_registry'; data_mode: DataMode | 'geospatial' | 'registry'; status: string; snapshot_at: string | null; records_total: number; complexes_total: number | null; fields: CoverageField[]; distributions: Distribution[] }
export type DataCoverageResponse = { snapshot_id: string; data_mode: DataMode; generated_at: string; criteria_total: number; datasets: CoverageDataset[] }

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  } catch (error) {
    if (options?.signal?.aborted) throw error
    throw new Error('Нет соединения с каталогом. Проверьте сеть и попробуйте ещё раз.')
  }
  if (!response.ok) {
    let message = `Каталог ответил с ошибкой ${response.status}`
    try {
      const body = await response.json()
      if (typeof body?.detail?.message === 'string') message = body.detail.message
      else if (typeof body?.detail === 'string') message = body.detail
    } catch { /* preserve HTTP status */ }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

const post = <T>(path: string, body: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body) })
const filterParams = (filters: Filters = {}) => {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters).sort(([left], [right]) => left.localeCompare(right))) {
    if (key === 'limit' || value === undefined || value === null) continue
    if (key === 'predicates') { if (Array.isArray(value) && value.length) params.set(key, JSON.stringify(value)); continue }
    if (Array.isArray(value)) for (const item of [...value].sort()) params.append(key, String(item))
    else params.set(key, String(value))
  }
  return params.toString()
}
const filteredUrl = (path: string, filters?: Filters) => {
  const params = filterParams(filters)
  return params ? `${path}?${params}` : path
}
const eventSessionId = Array.from(crypto.getRandomValues(new Uint8Array(24)), (byte) => byte.toString(16).padStart(2, '0')).join('')
const MAP_CACHE_LIMIT = 12
const mapResults = new Map<string, MapData>()
const mapPending = new Map<string, Promise<MapData>>()
let currentSnapshot: string | null = null
export function setApiSnapshot(snapshotId: string): void {
  if (currentSnapshot === snapshotId) return
  currentSnapshot = snapshotId
  mapResults.clear()
  mapPending.clear()
}
function cachedMap(filters?: Filters): Promise<MapData> {
  const key = filteredUrl('/v1/map', filters)
  const result = mapResults.get(key)
  if (result && (!currentSnapshot || result.snapshot_id === currentSnapshot)) {
    mapResults.delete(key); mapResults.set(key, result)
    return Promise.resolve(result)
  }
  const pending = mapPending.get(key)
  if (pending) return pending
  const task = request<MapData>(key).then((data) => {
    if (!currentSnapshot || data.snapshot_id === currentSnapshot) {
      mapResults.delete(key); mapResults.set(key, data)
      if (mapResults.size > MAP_CACHE_LIMIT) mapResults.delete(mapResults.keys().next().value!)
    }
    return data
  }).finally(() => { if (mapPending.get(key) === task) mapPending.delete(key) })
  mapPending.set(key, task)
  return task
}
const registryUrl = (path: string, filters: RegistryFilters = {}) => {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) if (value !== undefined && value !== '') params.set(key, String(value))
  return params.size ? `${path}?${params}` : path
}
const modelUrl = (path: string, filters: ModelCandidateFilters = {}) => {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters).sort(([left], [right]) => left.localeCompare(right))) if (value !== undefined && value !== '') params.set(key, String(value))
  return params.size ? `${path}?${params}` : path
}
const MODEL_CACHE_LIMIT = 12
let modelCacheRunId: string | null = null
const modelLists = new Map<string, ModelCandidateList>()
const modelDetails = new Map<string, ModelCandidateDetail>()
const modelGeometries = new Map<string, ModelCandidateGeometry>()
export class ModelSnapshotChanged extends Error {}
export function setModelRun(modelRunId: string): void {
  if (modelCacheRunId === modelRunId) return
  modelCacheRunId = modelRunId
  modelLists.clear(); modelDetails.clear(); modelGeometries.clear()
}
function rememberModel<T>(cache: Map<string, T>, key: string, value: T): T {
  cache.delete(key); cache.set(key, value)
  if (cache.size > MODEL_CACHE_LIMIT) cache.delete(cache.keys().next().value!)
  return value
}
function modelSnapshot<T extends ModelCandidateMeta>(value: T, expectedRun: string): T {
  if (value.dataset_id !== 'model_candidates' || value.model_run_id !== expectedRun) throw new ModelSnapshotChanged('Проверка моделей обновилась. Повторите загрузку.')
  return value
}
async function modelCandidates(filters: ModelCandidateFilters, expectedRun: string, signal?: AbortSignal): Promise<ModelCandidateList> {
  const url = modelUrl('/v1/models/candidates', filters)
  const cached = modelLists.get(url)
  if (cached && cached.model_run_id === expectedRun) return rememberModel(modelLists, url, cached)
  const value = modelSnapshot(await request<ModelCandidateList>(url, { signal }), expectedRun)
  return rememberModel(modelLists, url, value)
}
async function modelCandidate(id: string, expectedRun: string, signal?: AbortSignal): Promise<ModelCandidateDetail> {
  const key = id
  const cached = modelDetails.get(key)
  if (cached && cached.model_run_id === expectedRun) return rememberModel(modelDetails, key, cached)
  const value = modelSnapshot(await request<ModelCandidateDetail>(`/v1/models/candidates/${encodeURIComponent(id)}`, { signal }), expectedRun)
  return rememberModel(modelDetails, key, value)
}
async function modelGeometry(id: string, expectedRun: string, signal?: AbortSignal): Promise<ModelCandidateGeometry> {
  const key = id
  const cached = modelGeometries.get(key)
  if (cached && cached.model_run_id === expectedRun) return rememberModel(modelGeometries, key, cached)
  const value = modelSnapshot(await request<ModelCandidateGeometry>(`/v1/models/candidates/${encodeURIComponent(id)}/geometry`, { signal }), expectedRun)
  return rememberModel(modelGeometries, key, value)
}
export class RegistrySnapshotChanged extends Error {}
export type RegistryCatalogView = 'building_registry' | 'developer_buildings'
const registryDataset = (view: RegistryCatalogView) => view === 'developer_buildings' ? 'developer_building_registry' : 'building_registry'

export async function loadRegistryView(filters: RegistryFilters, expectedSnapshot?: string | null, signal?: AbortSignal, catalogView: RegistryCatalogView = 'building_registry', expectedEnrichmentRun?: string | null): Promise<{ list: RegistryList; map: RegistryMap }> {
  const { limit: _limit, offset: _offset, ...base } = filters
  const [list, map] = await Promise.all([
    request<RegistryList>(registryUrl('/v1/registry/buildings', { ...base, limit: 48, offset: 0 }), { signal }),
    request<RegistryMap>(registryUrl('/v1/registry/map', base), { signal }),
  ])
  if (list.dataset_id !== registryDataset(catalogView) || map.dataset_id !== registryDataset(catalogView) || list.snapshot_id !== map.snapshot_id || (expectedSnapshot && list.snapshot_id !== expectedSnapshot)
    || (catalogView === 'developer_buildings' && (!list.map_run_id || list.map_run_id !== map.map_run_id
      || list.unit_run_id !== map.unit_run_id || (list.enrichment_run_id ?? null) !== (map.enrichment_run_id ?? null)
      || (expectedEnrichmentRun !== undefined && (list.enrichment_run_id ?? null) !== expectedEnrichmentRun)))) {
    throw new RegistrySnapshotChanged('Реестр обновился. Обновите страницу, чтобы получить новый снимок.')
  }
  return { list, map }
}

export async function loadRegistryPointView(filters: RegistryFilters, expectedSnapshot?: string | null, signal?: AbortSignal, catalogView: RegistryCatalogView = 'building_registry', expectedMapRun?: string | null, expectedUnitRun?: string | null, expectedEnrichmentRun?: string | null): Promise<{ list: RegistryList; map: RegistryMap; points: MapPointsResult }> {
  const { limit: _limit, offset: _offset, ...base } = filters
  const [view, points] = await Promise.all([
    loadRegistryView(filters, expectedSnapshot, signal, catalogView, expectedEnrichmentRun),
    request<MapPointsResult>(registryUrl('/v1/map/points', base), { signal }),
  ])
  const newMap = catalogView === 'developer_buildings'
  if (points.dataset_id !== (newMap ? 'developer_map_points' : 'map_points')
    || !(newMap ? points.map_run_id : points.model_run_id)
    || (newMap && (points.map_run_id !== view.list.map_run_id || (expectedMapRun && points.map_run_id !== expectedMapRun)
      || points.unit_run_id !== view.list.unit_run_id || (expectedUnitRun && points.unit_run_id !== expectedUnitRun)
      || (points.enrichment_run_id ?? null) !== (view.list.enrichment_run_id ?? null)))
    || (newMap && (points.items.some((point) => point.point_type !== 'developer_building')
      || points.counts?.mapped_construction_points !== 0
      || view.list.items.some((item) => (item.enrichment_run_id ?? null) !== (view.list.enrichment_run_id ?? null))
      || view.map.items.some((item) => (item.enrichment_run_id ?? null) !== (view.list.enrichment_run_id ?? null))
      || points.items.some((point) => point.point_type === 'developer_building'
        && (point.enrichment_run_id ?? null) !== (view.list.enrichment_run_id ?? null))))
    || points.registry_snapshot_id !== view.list.snapshot_id
    || points.registry_snapshot_id !== view.map.snapshot_id || (expectedSnapshot && points.registry_snapshot_id !== expectedSnapshot)) {
    throw new RegistrySnapshotChanged('Точки и реестр относятся к разным снимкам. Обновите страницу.')
  }
  return { ...view, points }
}

export const api = {
  health: () => request<Health>('/v1/health', { cache: 'no-store' }),
  districts: () => request<ListResponse<District>>('/v1/districts'),
  criteria: () => request<CriteriaResponse>('/v1/criteria'),
  dataCoverage: () => request<DataCoverageResponse>('/v1/data-coverage'),
  map: cachedMap,
  registryBuildings: (filters?: RegistryFilters, signal?: AbortSignal) => request<RegistryList>(registryUrl('/v1/registry/buildings', filters), { signal }),
  registryBuilding: (id: string, signal?: AbortSignal) => request<RegistryBuilding>(`/v1/registry/buildings/${encodeURIComponent(id)}`, { signal }),
  developerFloors: (registryId: string, filters: DeveloperUnitCriteria = {}, signal?: AbortSignal) => request<DeveloperFloorList>(registryUrl(`/v1/developer-buildings/${encodeURIComponent(registryId)}/floors`, filters), { signal }),
  developerFloor: (registryId: string, floorId: string, filters: DeveloperUnitCriteria = {}, signal?: AbortSignal) => request<DeveloperFloorDetail>(registryUrl(`/v1/developer-buildings/${encodeURIComponent(registryId)}/floors/${encodeURIComponent(floorId)}`, filters), { signal }),
  developerUnits: (registryId: string, filters: DeveloperUnitCriteria & { floor_id?: string; section_id?: string; floor_known?: boolean; limit?: number; offset?: number } = {}, signal?: AbortSignal) => request<DeveloperUnitList>(registryUrl(`/v1/developer-buildings/${encodeURIComponent(registryId)}/units`, filters), { signal }),
  developerUnit: (registryId: string, unitId: string, filters: DeveloperUnitCriteria = {}, signal?: AbortSignal) => request<DeveloperUnitDetail>(registryUrl(`/v1/developer-buildings/${encodeURIComponent(registryId)}/units/${encodeURIComponent(unitId)}`, filters), { signal }),
  developerModel: (registryId: string, signal?: AbortSignal) => request<DeveloperModel>(`/v1/developer-buildings/${encodeURIComponent(registryId)}/model`, { signal }),
  developerParkWalk: (registryId: string, signal?: AbortSignal) => request<DeveloperParkWalk>(`/v1/developer-buildings/${encodeURIComponent(registryId)}/park-walk`, { signal }),
  registryMap: (filters?: Omit<RegistryFilters, 'limit' | 'offset'>, signal?: AbortSignal) => request<RegistryMap>(registryUrl('/v1/registry/map', filters), { signal }),
  registryView: loadRegistryView,
  registryPointView: loadRegistryPointView,
  nearbyPoint: (pointId: string, education: NearbyEducation, signal?: AbortSignal) => request<NearbySelection>(`/v1/map/nearby?point_id=${encodeURIComponent(pointId)}&education=${education}`, { signal }),
  nearbyViewport: (bbox: [number, number, number, number], layers: NearbyCategory[], education: NearbyEducation, signal?: AbortSignal) => request<NearbyViewport>(`/v1/map/nearby/viewport?bbox=${bbox.join(',')}&layers=${layers.join(',')}&education=${education}&limit=150`, { signal }),
  modelSummary: (signal?: AbortSignal) => request<ModelCandidateSummary>('/v1/models/candidates/summary', { signal }),
  modelCandidates,
  modelCandidate,
  modelGeometry,
  complex: (id: string) => request<Complex>(`/v1/complexes/${encodeURIComponent(id)}`),
  offer: (id: string) => request<Offer>(`/v1/offers/${encodeURIComponent(id)}`),
  search: (body: Query, signal?: AbortSignal) => request<SearchResponse>('/v1/search', { method: 'POST', body: JSON.stringify(body), signal }),
  recommendations: (body: Query, signal?: AbortSignal) => request<SearchResponse>('/v1/recommendations', { method: 'POST', body: JSON.stringify(body), signal }),
  compare: (offer_ids: string[]) => post<CompareResponse>('/v1/compare', { offer_ids }),
  event: (event_type: 'search' | 'offer_view' | 'compare' | 'recommendation_view' | 'map_view') => post<{ accepted: boolean }>('/v1/events', { event_type, session_id: eventSessionId }),
}
