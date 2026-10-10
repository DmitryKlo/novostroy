# Additive data coverage route

`GET /v1/data-coverage` reports counts about the currently loaded, displayed
datasets. It contains no provider identity, raw row, offer ID, analytics event,
or inferred market/conversion measure. The response is
`{snapshot_id,data_mode,generated_at,criteria_total,datasets}`. Four
dataset IDs distinguish `demo_offers`, `real_current_offers`,
`city_geometry`, and `building_registry`. Each dataset has `data_mode`, `status`, `snapshot_at`,
`records_total`, `complexes_total` (integer for offer datasets, `null` for city),
`fields`, and `distributions`. `complexes_total` counts distinct complexes
among the dataset's eligible offers, not every registered ЖК.
`building_registry` has `data_mode=registry`, no offers or price distributions,
and `complexes_total=null`: a source record may describe a building or phase,
so the record count is not a verified count of legal ЖК. Its `snapshot_at` is
the latest report-window end date, not evidence of current construction status.
It is `not_loaded` with zero records until an internally supplied import is
validated. Source identity, local paths and raw URL IDs stay server-side.
The separate flat `ground_buildings` tileset renders the same city source
population as `city_geometry`; it is not an additional dataset or building
count, and does not introduce height facts.

Each field row has `key`, `unit`, `populated_count`, `unknown_count`,
`stale_count`, and `quality_counts` with `observed`, `derived`, `estimated`,
`illustrative`, and `render_fallback`. Counts refer only to that dataset's
records. `populated_count` means a non-null value, **not** a verified real-world
fact. Quality counts divide populated values and must sum to
`populated_count`; `unknown_count` counts missing/null other than explicit stale,
and `stale_count` counts explicit stale fields. For each field,
`populated_count + unknown_count + stale_count = records_total`. Neither demo
values nor estimated rendering heights
can be reported as observed. For the city layer, a source height is observed,
a 3 m/floor render estimate and a conservative local-shape height estimate are
both estimated, and a fixed display extrusion is render_fallback.
`city.render_height_imputed_m` counts only the local-shape overlay; it is not a
source height, measured floor count, or unit-level fact. The priority is positive
source height, then positive floors × 3 m, then approved local-shape estimate,
then 15 m renderer fallback. `city.footprint_geometry` remains source-backed mapping data,
not a surveyed or legally complete city inventory.
Each offer dataset includes one field row for every registry criterion; absent
facts therefore appear as unknown counts. A dashboard may initially display
the five common numeric fields and let readers expand the rest, but cannot
silently treat a registered criterion as populated.

`distributions` contains only bounded charts for the selected offer dataset:
`offer.price_total_rub` (asking basis only) and `unit.area_sqm` (total area).
Each distribution is `{key,unit,basis,population_count,sample_count,
missing_count,excluded_count,bins}`. Price rows with conditional basis are
`excluded_count`, not treated as missing asking prices. Counts satisfy
`sample_count + missing_count + excluded_count = population_count`, where
`population_count = records_total`; bin counts sum to `sample_count`. A bin is
`{lower,upper,upper_inclusive,count}` with inclusive lower bound and a stated
upper-bound rule. Up to five bins are produced from current values without
extrapolation. Empty real offers return empty bins and all counts zero; city
geometry has no offer distributions. Charts retain their dataset label and
`data_mode`, so demo price charts cannot be mistaken for a market sample.

The demo dataset has `records_total=0,status=not_loaded` in real mode. The
real-current dataset has `records_total=0,status=no_licensed_feed` in the
current demo deployment; its count must never use demo offers. Once a cleared
real feed is configured, it counts only current eligible offers after the v1
freshness/availability/rights policy. `criteria_total` is the number of
registry definitions; it does not mean those criteria have populated values.
Clients may use these numbers for a compact data-quality view, while placement
of that view follows the user's product decision. `/v1/metrics` QA traffic is
not part of this route.
