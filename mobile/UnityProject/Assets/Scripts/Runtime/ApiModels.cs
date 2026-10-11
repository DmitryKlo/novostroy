using System.Collections.Generic;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Novostroy.Runtime
{
    public sealed class HealthResponse
    {
        [JsonProperty("data_mode")] public string DataMode { get; set; }
        [JsonProperty("snapshot_id")] public string SnapshotId { get; set; }
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("catalog_view")] public string CatalogView { get; set; }
        [JsonProperty("registry_enabled")] public bool RegistryEnabled { get; set; }
        [JsonProperty("registry_snapshot_id")] public string RegistrySnapshotId { get; set; }
        [JsonProperty("enrichment_run_id")] public string EnrichmentRunId { get; set; }
        [JsonProperty("overlay_run_id")] public string OverlayRunId { get; set; }
    }

    public sealed class MapResponse
    {
        [JsonProperty("data_mode")] public string DataMode { get; set; }
        [JsonProperty("snapshot_id")] public string SnapshotId { get; set; }
        [JsonProperty("center")] public double[] Center { get; set; }
        [JsonProperty("tilesets")] public List<Tileset> Tilesets { get; set; }
        [JsonProperty("complexes")] public List<MapComplex> Complexes { get; set; }
        [JsonProperty("clarification")] public Clarification Clarification { get; set; }
    }

    public sealed class Tileset
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("url")] public string Url { get; set; }
        [JsonProperty("kind")] public string Kind { get; set; }
        [JsonProperty("model_quality")] public string ModelQuality { get; set; }
        [JsonProperty("label")] public string Label { get; set; }
        [JsonProperty("attribution")] public string Attribution { get; set; }
    }

    public sealed class MapComplex
    {
        [JsonProperty("complex_id")] public string ComplexId { get; set; }
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("district_id")] public string DistrictId { get; set; }
        [JsonProperty("location")] public double[] Location { get; set; }
        [JsonProperty("model_quality")] public string ModelQuality { get; set; }
        [JsonProperty("offer_count")] public int OfferCount { get; set; }
    }

    // Additive point-map wire. Native registry browsing is still explicitly
    // unsupported; these typed targets must not become demo complex IDs.
    public sealed class SourceInternalAirIndex
    {
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("value")] public int? Value { get; set; }
        [JsonProperty("scale_id")] public string ScaleId { get; set; }
        [JsonProperty("higher_is_worse")] public bool HigherIsWorse { get; set; }
        [JsonProperty("official_index")] public bool OfficialIndex { get; set; }
        [JsonProperty("measurement")] public bool Measurement { get; set; }
        [JsonProperty("valid_at")] public string ValidAt { get; set; }
        [JsonProperty("stale_after")] public string StaleAfter { get; set; }
    }

    public sealed class SourceAirQuality
    {
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("label")] public string Label { get; set; }
        [JsonProperty("internal_index")] public SourceInternalAirIndex InternalIndex { get; set; }
    }

    public sealed class SourceSectionFloorCount
    {
        [JsonProperty("section_label")] public string SectionLabel { get; set; }
        [JsonProperty("total_floors")] public int TotalFloors { get; set; }
        [JsonProperty("total_floors_basis")] public string TotalFloorsBasis { get; set; }
    }

    public sealed class SourceMapPoint
    {
        [JsonProperty("point_id")] public string PointId { get; set; }
        [JsonProperty("point_type")] public string PointType { get; set; }
        [JsonProperty("registry_id")] public string RegistryId { get; set; }
        [JsonProperty("candidate_id")] public string CandidateId { get; set; }
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("location")] public double[] Location { get; set; }
        [JsonProperty("latest_known_date")] public string LatestKnownDate { get; set; }
        [JsonProperty("geometry_as_of")] public string GeometryAsOf { get; set; }
        [JsonProperty("address")] public string Address { get; set; }
        [JsonProperty("address_basis")] public string AddressBasis { get; set; }
        [JsonProperty("complex_label")] public string ComplexLabel { get; set; }
        [JsonProperty("complex_label_basis")] public string ComplexLabelBasis { get; set; }
        [JsonProperty("known_catalog_registry_id")] public string KnownCatalogRegistryId { get; set; }
        [JsonProperty("developer_name")] public string DeveloperName { get; set; }
        [JsonProperty("developer_display_name")] public string DeveloperDisplayName { get; set; }
        [JsonProperty("developer_display_basis")] public string DeveloperDisplayBasis { get; set; }
        [JsonProperty("total_floors")] public int? TotalFloors { get; set; }
        [JsonProperty("total_floors_basis")] public string TotalFloorsBasis { get; set; }
        [JsonProperty("max_observed_unit_floor")] public int? MaxObservedUnitFloor { get; set; }
        [JsonProperty("section_floor_counts")] public List<SourceSectionFloorCount> SectionFloorCounts { get; set; }
        [JsonProperty("air_quality")] public SourceAirQuality AirQuality { get; set; }
        [JsonProperty("unit_count")] public int? UnitCount { get; set; }
        [JsonProperty("observed_at")] public string ObservedAt { get; set; }
        [JsonProperty("location_basis")] public string LocationBasis { get; set; }
        [JsonProperty("address_complete")] public bool? AddressComplete { get; set; }
    }

    public sealed class SourceMapPointCounts
    {
        [JsonProperty("points")] public int Points { get; set; }
        [JsonProperty("registry_points")] public int RegistryPoints { get; set; }
        [JsonProperty("registry_points_total")] public int RegistryPointsTotal { get; set; }
        [JsonProperty("mapped_construction_points")] public int MappedConstructionPoints { get; set; }
        [JsonProperty("coordinate_positions")] public int CoordinatePositions { get; set; }
        [JsonProperty("excluded_mapped_by_registry_filters")] public int ExcludedMappedByRegistryFilters { get; set; }
    }

    // Optional display grouping preserves each point's typed identity. A group
    // exists only when the server has validated source-owned membership proof.
    public sealed class SourceMapPointGroup
    {
        [JsonProperty("group_id")] public string GroupId { get; set; }
        [JsonProperty("group_scope")] public string GroupScope { get; set; }
        [JsonProperty("identity_basis")] public string IdentityBasis { get; set; }
        [JsonProperty("group_label")] public string GroupLabel { get; set; }
        [JsonProperty("point_ids")] public List<string> PointIds { get; set; }
    }

    public sealed class SourceMapPointGroupEvidence
    {
        [JsonProperty("schema_version")] public int SchemaVersion { get; set; }
        [JsonProperty("visibility")] public string Visibility { get; set; }
        [JsonProperty("registry_snapshot_id")] public string RegistrySnapshotId { get; set; }
        [JsonProperty("model_run_id")] public string ModelRunId { get; set; }
        [JsonProperty("map_run_id")] public string MapRunId { get; set; }
        [JsonProperty("groups")] public List<SourceMapPointGroup> Groups { get; set; }
    }

    public sealed class SourceMapPointsResponse
    {
        [JsonProperty("dataset_id")] public string DatasetId { get; set; }
        [JsonProperty("registry_snapshot_id")] public string RegistrySnapshotId { get; set; }
        [JsonProperty("model_run_id")] public string ModelRunId { get; set; }
        [JsonProperty("map_run_id")] public string MapRunId { get; set; }
        [JsonProperty("unit_run_id")] public string UnitRunId { get; set; }
        [JsonProperty("enrichment_run_id")] public string EnrichmentRunId { get; set; }
        [JsonProperty("overlay_run_id")] public string OverlayRunId { get; set; }
        [JsonProperty("snapshot_at")] public string SnapshotAt { get; set; }
        [JsonProperty("checked_at")] public string CheckedAt { get; set; }
        [JsonProperty("registry_rights_status")] public string RegistryRightsStatus { get; set; }
        [JsonProperty("mapped_attribution")] public string MappedAttribution { get; set; }
        [JsonProperty("dataset_total")] public int DatasetTotal { get; set; }
        [JsonProperty("registry_source_total")] public int RegistrySourceTotal { get; set; }
        [JsonProperty("registry_archived_total")] public int RegistryArchivedTotal { get; set; }
        [JsonProperty("counts")] public SourceMapPointCounts Counts { get; set; }
        [JsonProperty("applied_filters")] public Dictionary<string, JToken> AppliedFilters { get; set; }
        [JsonProperty("items")] public List<SourceMapPoint> Items { get; set; }
        [JsonProperty("group_evidence")] public SourceMapPointGroupEvidence GroupEvidence { get; set; }
    }

    // A selected viewport references one pinned batch tileset. Native clients
    // must not infer a building identity from a visual city volume.
    public sealed class SourceDeveloperOverlayViewport
    {
        [JsonProperty("dataset_id")] public string DatasetId { get; set; }
        [JsonProperty("registry_snapshot_id")] public string RegistrySnapshotId { get; set; }
        [JsonProperty("map_run_id")] public string MapRunId { get; set; }
        [JsonProperty("unit_run_id")] public string UnitRunId { get; set; }
        [JsonProperty("enrichment_run_id")] public string EnrichmentRunId { get; set; }
        [JsonProperty("overlay_run_id")] public string OverlayRunId { get; set; }
        [JsonProperty("tileset_url")] public string TilesetUrl { get; set; }
        [JsonProperty("city_patch_url")] public string CityPatchUrl { get; set; }
        [JsonProperty("bbox")] public double[] Bbox { get; set; }
        [JsonProperty("features_total")] public int FeaturesTotal { get; set; }
        [JsonProperty("matched_total")] public int MatchedTotal { get; set; }
        [JsonProperty("limit")] public int Limit { get; set; }
        [JsonProperty("offset")] public int Offset { get; set; }
        [JsonProperty("truncated")] public bool Truncated { get; set; }
        [JsonProperty("eligible_registry_ids")] public List<string> EligibleRegistryIds { get; set; }
        [JsonProperty("applied_filters")] public Dictionary<string, JToken> AppliedFilters { get; set; }
    }

    // Additive local-amenity wire. A route is an approximate estimate from a
    // mapped point, never a verified building entrance or live travel time.
    // Native registry browsing remains unsupported; these are DTOs only.
    public sealed class NearbyCoordinate
    {
        [JsonProperty("lon")] public double Lon { get; set; }
        [JsonProperty("lat")] public double Lat { get; set; }
    }

    public sealed class NearbyOrigin
    {
        [JsonProperty("coordinate")] public NearbyCoordinate Coordinate { get; set; }
        [JsonProperty("access_kind")] public string AccessKind { get; set; }
    }

    public sealed class NearbyRoute
    {
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("distance_m")] public int? DistanceM { get; set; }
        [JsonProperty("estimated_minutes")] public double? EstimatedMinutes { get; set; }
        [JsonProperty("geometry")] public List<double[]> Geometry { get; set; }
        [JsonProperty("approximate")] public bool Approximate { get; set; }
        [JsonProperty("origin_access_kind")] public string OriginAccessKind { get; set; }
        [JsonProperty("target_access_kind")] public string TargetAccessKind { get; set; }
        [JsonProperty("endpoint_coordinate")] public NearbyCoordinate EndpointCoordinate { get; set; }
        [JsonProperty("endpoint_basis")] public string EndpointBasis { get; set; }
        [JsonProperty("endpoint_note")] public string EndpointNote { get; set; }
        [JsonProperty("snap_distance_m")] public List<int> SnapDistanceM { get; set; }
        [JsonProperty("geometry_simplification_tolerance_m")] public double? GeometrySimplificationToleranceM { get; set; }
        [JsonProperty("routing_basis")] public string RoutingBasis { get; set; }
        [JsonProperty("estimate_basis")] public string EstimateBasis { get; set; }
    }

    public sealed class NearbyOmsEvidence
    {
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("scope")] public string Scope { get; set; }
        [JsonProperty("official_snapshot_at")] public string OfficialSnapshotAt { get; set; }
    }

    public sealed class NearbyPoi
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("category")] public string Category { get; set; }
        [JsonProperty("coordinate")] public NearbyCoordinate Coordinate { get; set; }
        [JsonProperty("coordinate_basis")] public string CoordinateBasis { get; set; }
        [JsonProperty("straight_line_m")] public int? StraightLineM { get; set; }
        [JsonProperty("metro_access_link")] public string MetroAccessLink { get; set; }
        [JsonProperty("route")] public NearbyRoute Route { get; set; }
        [JsonProperty("free_oms")] public NearbyOmsEvidence FreeOms { get; set; }
        [JsonProperty("osm_updated_at")] public string OsmUpdatedAt { get; set; }
    }

    public sealed class NearbyPointLayer
    {
        [JsonProperty("items")] public List<NearbyPoi> Items { get; set; }
        [JsonProperty("ranking_basis")] public string RankingBasis { get; set; }
        [JsonProperty("selection_policy")] public string SelectionPolicy { get; set; }
        [JsonProperty("required_clinic_included")] public bool RequiredClinicIncluded { get; set; }
        [JsonProperty("candidate_count")] public int CandidateCount { get; set; }
        [JsonProperty("coverage")] public string Coverage { get; set; }
        [JsonProperty("attribution")] public string Attribution { get; set; }
    }

    public sealed class NearbyClinicGuarantee
    {
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("scope")] public string Scope { get; set; }
    }

    public sealed class NearbyPointCoverage
    {
        [JsonProperty("poi")] public string Poi { get; set; }
        [JsonProperty("routes")] public string Routes { get; set; }
        [JsonProperty("candidate_limit_per_layer")] public int CandidateLimitPerLayer { get; set; }
        [JsonProperty("ranking_basis")] public string RankingBasis { get; set; }
    }

    public sealed class NearbyPointResponse
    {
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("point_id")] public string PointId { get; set; }
        [JsonProperty("education")] public string Education { get; set; }
        [JsonProperty("origin")] public NearbyOrigin Origin { get; set; }
        [JsonProperty("dataset_id")] public string DatasetId { get; set; }
        [JsonProperty("osm_snapshot_at")] public string OsmSnapshotAt { get; set; }
        [JsonProperty("layers")] public Dictionary<string, NearbyPointLayer> Layers { get; set; }
        [JsonProperty("free_clinic_guarantee")] public NearbyClinicGuarantee FreeClinicGuarantee { get; set; }
        [JsonProperty("coverage")] public NearbyPointCoverage Coverage { get; set; }
        [JsonProperty("attribution")] public string Attribution { get; set; }
        [JsonProperty("registry_snapshot_id")] public string RegistrySnapshotId { get; set; }
        [JsonProperty("model_run_id")] public string ModelRunId { get; set; }
        [JsonProperty("map_run_id")] public string MapRunId { get; set; }
    }

    public sealed class NearbyViewportLayer
    {
        [JsonProperty("items")] public List<NearbyPoi> Items { get; set; }
        [JsonProperty("coverage")] public string Coverage { get; set; }
        [JsonProperty("attribution")] public string Attribution { get; set; }
    }

    public sealed class NearbyViewportCoverage
    {
        [JsonProperty("poi")] public string Poi { get; set; }
    }

    public sealed class NearbyViewportResponse
    {
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("dataset_id")] public string DatasetId { get; set; }
        [JsonProperty("osm_snapshot_at")] public string OsmSnapshotAt { get; set; }
        [JsonProperty("education")] public string Education { get; set; }
        [JsonProperty("bbox")] public double[] Bbox { get; set; }
        [JsonProperty("layers")] public Dictionary<string, NearbyViewportLayer> Layers { get; set; }
        [JsonProperty("truncated")] public bool Truncated { get; set; }
        [JsonProperty("returned_count")] public int ReturnedCount { get; set; }
        [JsonProperty("coverage")] public NearbyViewportCoverage Coverage { get; set; }
        [JsonProperty("attribution")] public string Attribution { get; set; }
        [JsonProperty("registry_snapshot_id")] public string RegistrySnapshotId { get; set; }
        [JsonProperty("model_run_id")] public string ModelRunId { get; set; }
        [JsonProperty("map_run_id")] public string MapRunId { get; set; }
    }

    // Additive model-candidate wire types. Native registry browsing remains unsupported;
    // these DTOs must not route unlinked mapped construction into demo offers.
    public sealed class ModelCandidateCounts
    {
        [JsonProperty("scanned")] public int Scanned { get; set; }
        [JsonProperty("ready")] public int Ready { get; set; }
        [JsonProperty("review")] public int Review { get; set; }
        [JsonProperty("rejected")] public int Rejected { get; set; }
        [JsonProperty("unavailable")] public int Unavailable { get; set; }
    }

    public sealed class ModelCandidateFacet
    {
        [JsonProperty("value")] public string Value { get; set; }
        [JsonProperty("count")] public int Count { get; set; }
    }

    public sealed class ModelCandidateFacets
    {
        [JsonProperty("reason_codes")] public List<ModelCandidateFacet> ReasonCodes { get; set; }
        [JsonProperty("source_groups")] public List<ModelCandidateFacet> SourceGroups { get; set; }
    }

    public class ModelCandidateSnapshot
    {
        [JsonProperty("dataset_id")] public string DatasetId { get; set; }
        [JsonProperty("registry_snapshot_id")] public string RegistrySnapshotId { get; set; }
        [JsonProperty("model_run_id")] public string ModelRunId { get; set; }
        [JsonProperty("checked_at")] public string CheckedAt { get; set; }
    }

    public sealed class ModelCandidateSummaryResponse : ModelCandidateSnapshot
    {
        [JsonProperty("run_status")] public string RunStatus { get; set; }
        [JsonProperty("counts")] public ModelCandidateCounts Counts { get; set; }
        [JsonProperty("facets")] public ModelCandidateFacets Facets { get; set; }
        [JsonProperty("ready_depictions")] public Dictionary<string, int> ReadyDepictions { get; set; }
    }

    public sealed class ModelCandidateListResponse : ModelCandidateSnapshot
    {
        [JsonProperty("dataset_total")] public int DatasetTotal { get; set; }
        [JsonProperty("records_total")] public int RecordsTotal { get; set; }
        [JsonProperty("limit")] public int Limit { get; set; }
        [JsonProperty("offset")] public int Offset { get; set; }
        [JsonProperty("items")] public List<ModelCandidate> Items { get; set; }
    }

    public sealed class ModelCandidate
    {
        [JsonProperty("candidate_id")] public string CandidateId { get; set; }
        [JsonProperty("registry_id")] public string RegistryId { get; set; }
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("source_group")] public string SourceGroup { get; set; }
        [JsonProperty("entity_type")] public string EntityType { get; set; }
        [JsonProperty("decision")] public string Decision { get; set; }
        [JsonProperty("asset_readiness")] public string AssetReadiness { get; set; }
        [JsonProperty("project_link_status")] public string ProjectLinkStatus { get; set; }
        [JsonProperty("ready_to_render")] public bool ReadyToRender { get; set; }
        [JsonProperty("depiction")] public string Depiction { get; set; }
        [JsonProperty("model_quality")] public string ModelQuality { get; set; }
        [JsonProperty("geometry_quality")] public string GeometryQuality { get; set; }
        [JsonProperty("geometry_as_of")] public string GeometryAsOf { get; set; }
        [JsonProperty("render_height_m")] public double? RenderHeightM { get; set; }
        [JsonProperty("height_evidence")] public ModelCandidateHeightEvidence HeightEvidence { get; set; }
        [JsonProperty("reason_codes")] public List<string> ReasonCodes { get; set; }
        [JsonProperty("explanation_ru")] public string ExplanationRu { get; set; }
        [JsonProperty("next_step_ru")] public string NextStepRu { get; set; }
        [JsonProperty("model_tileset")] public ModelCandidateTileset ModelTileset { get; set; }
        [JsonProperty("attempts")] public List<ModelCandidateAttempt> Attempts { get; set; }
    }

    public sealed class ModelCandidateAttempt
    {
        [JsonProperty("code")] public string Code { get; set; }
        [JsonProperty("label_ru")] public string LabelRu { get; set; }
        [JsonProperty("result_ru")] public string ResultRu { get; set; }
        [JsonProperty("count")] public int? Count { get; set; }
    }

    public sealed class ModelCandidateHeightEvidence
    {
        [JsonProperty("mapped_height_m")] public double? MappedHeightM { get; set; }
        [JsonProperty("mapped_levels")] public int? MappedLevels { get; set; }
        [JsonProperty("estimated_render_height_m")] public double? EstimatedRenderHeightM { get; set; }
        [JsonProperty("render_height_m")] public double? RenderHeightM { get; set; }
        [JsonProperty("render_height_basis")] public string RenderHeightBasis { get; set; }
    }

    public sealed class ModelCandidateDetailResponse : ModelCandidateSnapshot
    {
        [JsonProperty("item")] public ModelCandidate Item { get; set; }
    }

    public sealed class ModelCandidateTileset
    {
        [JsonProperty("url")] public string Url { get; set; }
        [JsonProperty("kind")] public string Kind { get; set; }
        [JsonProperty("model_quality")] public string ModelQuality { get; set; }
        [JsonProperty("attribution")] public string Attribution { get; set; }
    }

    public sealed class ModelCandidateGeometryResponse : ModelCandidateSnapshot
    {
        [JsonProperty("candidate_id")] public string CandidateId { get; set; }
        [JsonProperty("center")] public double[] Center { get; set; }
        [JsonProperty("bbox")] public double[] Bbox { get; set; }
        [JsonProperty("footprint")] public List<double[]> Footprint { get; set; }
        [JsonProperty("preview_geometry")] public ModelCandidatePreviewGeometry PreviewGeometry { get; set; }
        [JsonProperty("render_height_m")] public double? RenderHeightM { get; set; }
        [JsonProperty("height_evidence")] public ModelCandidateHeightEvidence HeightEvidence { get; set; }
        [JsonProperty("model_tileset")] public ModelCandidateTileset ModelTileset { get; set; }
    }

    public sealed class ModelCandidatePreviewGeometry
    {
        [JsonProperty("type")] public string Type { get; set; }
        // Polygon and MultiPolygon have different nesting depths. Native registry
        // browsing is unsupported; preserve the additive wire without guessing a ring.
        [JsonProperty("coordinates")] public JToken Coordinates { get; set; }
    }

    public sealed class District
    {
        [JsonProperty("district_id")] public string DistrictId { get; set; }
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("boundary_version")] public string BoundaryVersion { get; set; }
    }

    public sealed class DistrictResponse
    {
        [JsonProperty("data_mode")] public string DataMode { get; set; }
        [JsonProperty("items")] public List<District> Items { get; set; }
    }

    public sealed class CriteriaResponse
    {
        [JsonProperty("snapshot_id")] public string SnapshotId { get; set; }
        [JsonProperty("data_mode")] public string DataMode { get; set; }
        [JsonProperty("version")] public string Version { get; set; }
        [JsonProperty("criteria")] public List<CriterionDescriptor> Criteria { get; set; }
    }

    public sealed class CriterionDescriptor
    {
        [JsonProperty("key")] public string Key { get; set; }
        [JsonProperty("label_ru")] public string LabelRu { get; set; }
        [JsonProperty("group")] public string Group { get; set; }
        [JsonProperty("type")] public string Type { get; set; }
        [JsonProperty("value_type")] public string ValueType { get; set; }
        [JsonProperty("unit")] public string Unit { get; set; }
        [JsonProperty("operators")] public List<string> Operators { get; set; }
        [JsonProperty("enum_values")] public List<string> EnumValues { get; set; }
        [JsonProperty("availability")] public string Availability { get; set; }
        [JsonProperty("filterable")] public bool Filterable { get; set; }
        [JsonProperty("coverage_requirement")] public string CoverageRequirement { get; set; }
    }

    public sealed class CriterionFact
    {
        [JsonProperty("value")] public JToken Value { get; set; }
        [JsonProperty("unit")] public string Unit { get; set; }
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("scope")] public string Scope { get; set; }
        [JsonProperty("observed_at")] public string ObservedAt { get; set; }
        [JsonProperty("coverage_status")] public string CoverageStatus { get; set; }
        [JsonProperty("approximate")] public bool? Approximate { get; set; }
        [JsonProperty("method")] public string Method { get; set; }
    }

    public sealed class Price
    {
        [JsonProperty("amount_rub")] public long AmountRub { get; set; }
        [JsonProperty("basis")] public string Basis { get; set; }
        [JsonProperty("observed_at")] public string ObservedAt { get; set; }
    }

    public sealed class Source
    {
        [JsonProperty("quality")] public string Quality { get; set; }
        [JsonProperty("record_mode")] public string RecordMode { get; set; }
        [JsonProperty("observed_at")] public string ObservedAt { get; set; }
        [JsonProperty("rights_status")] public string RightsStatus { get; set; }
    }

    public sealed class Coverage
    {
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("observed_categories")] public List<string> ObservedCategories { get; set; }
        [JsonProperty("required_categories")] public List<string> RequiredCategories { get; set; }
    }

    public sealed class Recommendation
    {
        [JsonProperty("score")] public double? Score { get; set; }
        [JsonProperty("explanation")] public List<string> Explanation { get; set; }
        [JsonProperty("coverage")] public Coverage Coverage { get; set; }
    }

    public sealed class Offer
    {
        [JsonProperty("offer_id")] public string OfferId { get; set; }
        [JsonProperty("unit_id")] public string UnitId { get; set; }
        [JsonProperty("complex_id")] public string ComplexId { get; set; }
        [JsonProperty("district_id")] public string DistrictId { get; set; }
        [JsonProperty("rooms")] public int? Rooms { get; set; }
        [JsonProperty("area_sqm")] public double? AreaSqm { get; set; }
        [JsonProperty("floor")] public int? Floor { get; set; }
        [JsonProperty("price")] public Price Price { get; set; }
        [JsonProperty("source")] public Source Source { get; set; }
        [JsonProperty("availability")] public string Availability { get; set; }
        [JsonProperty("recommendation")] public Recommendation Recommendation { get; set; }
        [JsonProperty("facts")] public Dictionary<string, CriterionFact> Facts { get; set; }
    }

    public sealed class Clarification
    {
        [JsonProperty("message")] public string Message { get; set; }
    }

    public sealed class SearchResponse
    {
        [JsonProperty("data_mode")] public string DataMode { get; set; }
        [JsonProperty("snapshot_id")] public string SnapshotId { get; set; }
        [JsonProperty("items")] public List<Offer> Items { get; set; }
        [JsonProperty("count")] public int Count { get; set; }
        [JsonProperty("clarification")] public Clarification Clarification { get; set; }
        [JsonProperty("coverage")] public Coverage Coverage { get; set; }
    }

    public sealed class CompareResponse
    {
        [JsonProperty("data_mode")] public string DataMode { get; set; }
        [JsonProperty("snapshot_id")] public string SnapshotId { get; set; }
        [JsonProperty("items")] public List<Offer> Items { get; set; }
        [JsonProperty("fields")] public List<string> Fields { get; set; }
    }

    public sealed class ComplexDetail
    {
        [JsonProperty("complex_id")] public string ComplexId { get; set; }
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("developer")] public string Developer { get; set; }
        [JsonProperty("model_quality")] public string ModelQuality { get; set; }
        [JsonProperty("source")] public Source Source { get; set; }
        [JsonProperty("facts")] public Dictionary<string, CriterionFact> Facts { get; set; }
    }

    public sealed class EventAck
    {
        [JsonProperty("accepted")] public bool Accepted { get; set; }
    }

    public sealed class DataCoverageResponse
    {
        [JsonProperty("snapshot_id")] public string SnapshotId { get; set; }
        [JsonProperty("data_mode")] public string DataMode { get; set; }
        [JsonProperty("generated_at")] public string GeneratedAt { get; set; }
        [JsonProperty("criteria_total")] public long CriteriaTotal { get; set; }
        [JsonProperty("datasets")] public List<CoverageDataset> Datasets { get; set; }
    }

    public sealed class CoverageDataset
    {
        [JsonProperty("dataset_id")] public string DatasetId { get; set; }
        [JsonProperty("data_mode")] public string DataMode { get; set; }
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("snapshot_at")] public string SnapshotAt { get; set; }
        [JsonProperty("records_total")] public long RecordsTotal { get; set; }
        [JsonProperty("complexes_total")] public long? ComplexesTotal { get; set; }
        [JsonProperty("fields")] public List<CoverageField> Fields { get; set; }
        [JsonProperty("distributions")] public List<CoverageDistribution> Distributions { get; set; }
    }

    public sealed class CoverageField
    {
        [JsonProperty("key")] public string Key { get; set; }
        [JsonProperty("unit")] public string Unit { get; set; }
        [JsonProperty("populated_count")] public long PopulatedCount { get; set; }
        [JsonProperty("unknown_count")] public long UnknownCount { get; set; }
        [JsonProperty("stale_count")] public long StaleCount { get; set; }
        [JsonProperty("quality_counts")] public CoverageQualityCounts QualityCounts { get; set; }
    }

    public sealed class CoverageQualityCounts
    {
        [JsonProperty("observed")] public long Observed { get; set; }
        [JsonProperty("derived")] public long Derived { get; set; }
        [JsonProperty("estimated")] public long Estimated { get; set; }
        [JsonProperty("illustrative")] public long Illustrative { get; set; }
        [JsonProperty("render_fallback")] public long RenderFallback { get; set; }
    }

    public sealed class CoverageDistribution
    {
        [JsonProperty("key")] public string Key { get; set; }
        [JsonProperty("unit")] public string Unit { get; set; }
        [JsonProperty("basis")] public string Basis { get; set; }
        [JsonProperty("population_count")] public long PopulationCount { get; set; }
        [JsonProperty("sample_count")] public long SampleCount { get; set; }
        [JsonProperty("missing_count")] public long MissingCount { get; set; }
        [JsonProperty("excluded_count")] public long ExcludedCount { get; set; }
        [JsonProperty("bins")] public List<CoverageBin> Bins { get; set; }
    }

    public sealed class CoverageBin
    {
        [JsonProperty("lower")] public decimal Lower { get; set; }
        [JsonProperty("upper")] public decimal Upper { get; set; }
        [JsonProperty("upper_inclusive")] public bool UpperInclusive { get; set; }
        [JsonProperty("count")] public long Count { get; set; }
    }
}
