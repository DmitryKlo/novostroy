# Moscow building registry wire contract

The application automatically selects `catalog_view=building_registry` when a
validated internal import exists. `GET /v1/health` also returns
`registry_enabled` and `registry_snapshot_id`; the latter identifies the
registry snapshot and is intentionally distinct from the offer catalogue's
`snapshot_id`. `NOVOSTROY_REGISTRY_MODE=off` disables this view. An incomplete
or invalid configured import is an error, not a reason to show demo inventory.

`GET /v1/registry/buildings` lists source-backed building or project records.
The optional `query` searches name and address, `status` matches the historic
source string, and `kind` is one of `multifamily`, `apartment_complex`, or
`low_rise`. `limit` is 1–100 (default 48), and `offset` starts at zero. The
response contains a registry `snapshot_id`, date-only `snapshot_at`, full
`dataset_total`, filtered `records_total`, unfiltered status/kind facets, and
paginated `items`. `GET /v1/registry/buildings/{registry_id}` returns one item.
The public ID is opaque and is not a source row ID, URL ID, or legal complex ID.

Each item has name, record kind, city, source address, optional verified
point `location`, `latest_known_date`, status and handover **as reported on that
date**, any source-reported floors/building type, `has_apartment_data=false`,
`offer_count=0`, and `price=null`. The date is the report-window end; it does
not establish current construction status or availability. A missing or
ambiguous location is `null`. `/v1/registry/map` accepts the same three
filters and returns only items with confirmed source-matched point locations,
along with credited ground footprints, city volumes, road, park, and water
tilesets when published. Its `counts.records`
includes filtered records without points, while `counts.geocoded` counts map
markers. It never emits synthetic facade models for these records.

`/v1/data-coverage` includes the separate `building_registry` dataset. Its
`records_total` counts source records, `complexes_total` is null, and offer
distributions are empty. Offer search, recommendations, comparison, and demo
routes retain their existing strict semantics. The web's primary catalogue
uses the registry view when enabled and does not show apartment filters or
prices. Source names, local file paths, raw IDs, and provenance remain internal
unless server configuration explicitly enables source exposure; map copyright
credits remain visible.

The `ground_buildings` tileset is a separate flat rendering of the city
building footprints (`model_quality=cartographic_surface`). It shares the
mapped city population with `city_volumes`; neither map nor data coverage
adds the building count a second time. The active sanitized ground tileset is
located through the server-only `ground_tileset_url` manifest pointer.

Versioned, sanitized public GLB and tileset assets use immutable one-year
caching with ETag and Last-Modified validators and byte-range support. Dynamic
`/v1/` responses use `Cache-Control: no-store`, with no public validators.
