# Novostroy contract v1 (frozen for the first vertical slice)

This records the original v1 slice. Additive typed facts, criteria, data
coverage, map-layer kinds, and public source redaction are specified in
`IMPLEMENTATION-1.1.md`, `IMPLEMENTATION-1.2.md`,
`IMPLEMENTATION-1.3.md`, and `DATA-COVERAGE.md`;
`openapi.json`/`schemas.json` describe the current additive v1.3 wire format.

## Runtime split

The web client is React/TypeScript/CesiumJS. The native Android/iOS source is Unity/Cesium for Unity: it renders the same 3D Tiles on the map. FastAPI serves `/v1` JSON and `/assets` tiles. PostGIS is the target spatial store; a fixture-backed `demo` mode is allowed for a runnable local slice. All fixture data, prices, and geometry in demo mode are illustrative. The web UI must show a persistent demo banner and each offer's `source.quality`. There is no licensed facade model, live price feed, macOS/Xcode, or iOS signing access at this checkpoint.

## Stable routes and shapes

`GET /v1/health` -> `{api_version:"1",snapshot_id,snapshot_at,data_mode,status,counts}`. `counts` has `districts`, `complexes`, `offers`, `city_buildings`, `facade_models`. `data_mode` is `demo` or `real`; all list responses carry it and `snapshot_id`. `GET /v1/districts` -> `{snapshot_id,data_mode,items:[District],count}`. District IDs must be tied to `boundary_version`. `GET /v1/map` -> `{snapshot_id,data_mode,center:[lon,lat],tilesets:[Tileset],complexes:[MapComplex],counts}`. Each `MapComplex` has `complex_id,name,district_id,location:[lon,lat],model_quality,offer_count`. `Tileset.url` is an `/assets/.../tileset.json` route; original v1 kinds were `city_volumes` and `facade`, with qualities `synthetic_massing`, `synthetic_facade`, and `licensed_facade`. Additive v1.1 also permits `roads`/`cartographic_ribbon` and `parks` or `water`/`cartographic_surface`; see `IMPLEMENTATION-1.1.md`. A footprint extrusion is always `synthetic_massing`; a fictional facade demo is `synthetic_facade`. Preserve source and rights provenance per asset. Future unit details (`building_id`, `section_id`, `floor_id`, `unit_id`) are optional identifiers, never inferred from an exterior shell.

`GET /v1/complexes/{complex_id}` -> `Complex`. `GET /v1/offers/{offer_id}` -> `Offer`. `offer_id` is distinct from nullable `unit_id`. `Offer.price` includes integer `amount_rub`, `basis` (`asking_total` or `conditional_total`), and `observed_at`. Conditional prices do not silently substitute for asking prices. Offers carry `source` and `source.quality`; no demo price may appear in `real` mode. Explicit `availability="unavailable"` is ineligible for map/search/recommendation/detail/compare. Unknown/null availability may be shown only as unconfirmed, never as available.

`POST /v1/search` and `/v1/recommendations` accept `{query?,filters?,preferences?}`. Filters are `budget_min_rub`, `budget_max_rub` (inclusive), `district_ids`, `complex_ids`, `rooms`, `area_min_sqm`, `area_max_sqm`, `price_basis`, `limit`. District, complex, and price filters are hard: missing values fail a strict filter. `query` supports simple Russian budget, district, area and room expressions. Ambiguous district names return a `clarification`, with no guessed district. Results use `{snapshot_id,data_mode,items:[Offer],count,applied_filters,clarification?,coverage}`; `count` is the eligible strict total before `limit`. Recommendation items additionally have `recommendation:{score,explanation,coverage}`. Missing social coverage cannot raise a score by renormalization. Real offer freshness is assessed against current UTC by default (inject `as_of` for tests): price timestamp older than 24 hours excludes the offer. Demo has no age cutoff and is labeled illustrative.

`GET /v1/map` accepts the same hard filters (except `limit`) as query parameters; repeat list parameters (`district_ids=a&district_ids=b`). It also accepts `query`, uses the same parser, and returns `applied_filters` and optional `clarification`. `counts.offers` is the same eligible strict total as search before `limit`. A selected marker requests its offers with `POST /v1/search` and `filters.complex_ids:[complex_id]`, so complexes beyond the first search page still open correctly.

`POST /v1/compare` accepts `{offer_ids:[...]}` (2-4) and returns `{snapshot_id,data_mode,items:[Offer],fields:[...]}` in input order. `POST /v1/events` accepts only `{event_type,session_id?}` and returns `{accepted:true}`; a random session token may be omitted, and no query text, IDs, filters, or personal fields are accepted. `GET /v1/metrics` reports allowlisted counts and sufficiently sampled search-to-offer/compare timings. Standard errors use `{detail:{code,message}}`. `/assets/...` serves tile content and demo assets locally. `/v1` never proxies an unlicensed external feed.

`openapi.json` is the wire contract. `fixture.schema.json` validates the shared local fixture; version is `1`. A request/response example is in `examples.json`.

## Engine interface

`recommendations.engine.search(offers, request, *, snapshot_at, data_mode="demo", as_of=None, districts=()) -> dict` and `recommend(...) -> dict` accept plain JSON-like mappings. The backend adds `snapshot_id`/`data_mode` to the engine result. Call `resolve_filters(request,districts)` for map parity, then `eligible_offers(...,snapshot_at,data_mode,as_of)` and `filter_offers(...,filters)`. Never broaden constraints on no match. Both paths return deterministic ordering with `offer_id` as the final tie breaker.
