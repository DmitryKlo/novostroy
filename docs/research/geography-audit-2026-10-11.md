# Geography audit for developer-map-v1-7092646e252c8681

Audit date: 2026-10-11. Scope: Moscow city-boundary signal and current district/administrative-okrug filter candidates for the active developer-map points. This audit is read-only with respect to map/application data; it does not remove or alter any active points.

## Result

All 1,027 `map_eligible` coordinates fall inside the stored OSM Moscow city relation 102269 (version 422). The check found no point outside that mapped city mask and therefore no positive signal of Moscow-region points leaking into the active map. This is a cartographic comparison, not a cadastral/legal boundary certificate.

The join resolves every mapped point to one of 132 named district polygons and the current 12-okrug hierarchy. There are 965 unique assignments with ordinary mapped confidence and 62 unique assignments within approximately 100 m of a polygon edge; the latter remain assigned and are marked `confidence=low`. No point had zero or multiple containing district polygons after supplementing the three missing district polygons. No point was removed. The eight Zelenograd points remain classified in Zelenogradsky AO.

The prior candidate-only audit had seven uncovered points. Exact point-in-polygon checks against the added polygons resolve five to Novo-Peredelkino (ZAO) and two to Troparevo-Nikulino (ZAO). These are retained as exact polygon hits, not inferred from names or bounding boxes. The Orekhovo-Borisovo Yuzhnoye polygon fills the third missing official district footprint; no active point hit it in this batch.

The active inventory has 1,163 buildings, of which 136 have no mapped point. Those 136 remain `uncertain` for geographic scope; this audit did not infer a district from project/building names or incomplete addresses.

## Data and method

The active coordinates come from `data/property/developer_map/runs/developer-map-v1-7092646e252c8681/points.jsonl` (SHA-256 `34f974b8a31bbec4e120e2da91f8a50e0460d6c39419fa4e12b164faa57aa25e`). The city mask is the local GeoJSON for OSM relation 102269 v422 (SHA-256 `f08ff6b0c794fd3f079ae0f9daf004f9d8de64529a7327e0fc6444788561da29`). It is explicitly marked as not a legal authority in the source metadata.

The starting district footprint layer has 130 Overture Maps `division_area` features from release 2026-09-23.1, filtered by country RU, `subtype=locality`, a primary name containing “район”, and intersection with the saved city mask (layer SHA-256 `113724900f2e90e9210255c4857c0eb1c30f030c68ba9d43c5477697e5beb060`). Overture’s schema describes `locality` as a populated place that may or may not have administrative authority, and its geometry as suitable for mapped/carto display. The polygons are therefore used here as source-backed cartographic district footprints, not as legal boundary certification. The 130 layer records retain upstream OSM source IDs; the saved layer itself warns that candidates can be incomplete or overlap.

Three exact-name footprint gaps were supplemented: Overture source feature `dacdf29f-a685-49bf-b469-ac55d20d85ad` / upstream OSM `r446117@40` for Novo-Peredelkino; Overture feature `578b2907-9a88-45b1-a057-d4f403748ace` / upstream OSM `r456807@71` for Orekhovo-Borisovo Yuzhnoye; and OSM administrative relation 1292731 for Troparevo-Nikulino, captured as a public Nominatim polygon response and retained with its canonical OSM relation URL. The combined set has one polygon per official normalized district name (132/132).

For each point, a `covers` point-in-polygon test checks outer rings and holes. A result is assigned only when exactly one named district polygon covers the coordinate. The district name is normalized only for exact join (`ё`→`е`, case/punctuation normalization, removal of “район”); it is then mapped through the separate explicit current district→okrug hierarchy. No nearest-polygon, bounding-box, source project name, or address-name guess is used. Distances to the selected polygon’s edges are approximate local-metric calculations; `<=100 m` is a low-confidence flag, not an exclusion rule. The 62 low-confidence IDs exactly match the 62 near-edge IDs in the earlier candidate-layer audit.

Current hierarchy membership is supported by Moscow Mayor Order 256-RM (14 May 2024), which updates ZAO, NAO, and TAO membership, plus the Moscow Government’s 24 December 2024 Decree 3146-PP table enumerating district names by okrug. The official Moscow Youth Chambers pages for SAO and VAO were used as current-list cross-checks for districts omitted from the decree’s housing-coefficient table. The hierarchy JSON contains all 12 okrugs and 132 district entries. Generated `okrug_id` and `district_id` values are deterministic normalized-name handles, not official government codes.

## Output files

- `.runtime/research/geography-20261011/point_admin_candidates.jsonl` — 1,027 rows keyed by `registry_id`, with `municipality_scope`, `evidence_basis`, okrug/district IDs and names, current filter key, polygon source/ID, OSM record ID, hit count, edge distance, confidence, and review reason. SHA-256: `017a3617b5ce25cbda067dc646c7fcd8380056ee82df610e70629e1b778790d0`.
- `.runtime/research/geography-20261011/flagged_geography_cases_final.jsonl` — 62 low-confidence near-edge rows; no unresolved/outside/overlap rows. SHA-256: `6f017defbac4ca2d644bfb6b2ffcc9bde869800c6dcead13da248866c96e9570`.
- `.runtime/research/geography-20261011/supplemental_district_polygons.geojson` — three supplemental source-backed footprints. SHA-256: `98c4a83791585c9e35ccd8d6feff1cd5e5bc416fc7e79569146c32d3a3c3aef0`.
- `.runtime/research/geography-20261011/official_district_hierarchy.json` — explicit 12/132 name-to-okrug crosswalk. SHA-256: `7c57690cecc4aafd40b440557395206bd9577213bd20ad71c1b5c76f42f75a7e`.
- `.runtime/research/geography-20261011/summary_final.json` — counters, input/output hashes, and unresolved IDs (none).

## Sources

- [Moscow Mayor Order 256-RM, 14 May 2024](https://www.mos.ru/upload/documents/docs/8217/256-RM-bngh4.pdf): current membership updates for West, Novomoskovsky, and Troitsky okrugs.
- [Moscow Government Gazette №72, vol. 4 (December 2024), Decree 3146-PP](https://vestnikmoscow.mos.ru/wp-content/uploads/2024/12/zhurnal-vestnik-moskvy-%E2%84%96-72-tom-4.pdf): district names grouped by administrative okrug, including Zelenograd, Troitsky, and Novomoskovsky districts.
- [Moscow Law №13 of 8 May 2024](https://www.mos.ru/upload/documents/docs/343/Zakon13-ajbn2.pdf): legal establishment of the 2024 transformed/New Moscow district boundaries; this audit does not translate its textual/geodetic legal descriptions into cadastral GIS.
- [Moscow Department of Youth Policy SAO district list](https://dk.mos.ru/map/sao/) and [VAO district list](https://dk.mos.ru/map/vao/): current-list cross-checks, including Molzhaninovsky and Vostochny.
- [Overture DivisionArea schema v1.18](https://docs.overturemaps.org/schema/v1.18.0/reference/divisions/division_area/) and [DivisionSubtype definitions](https://docs.overturemaps.org/schema/v1.18.0/reference/divisions/types/division_subtype/): source model and limits of `locality` polygons.
- [OpenStreetMap relation 1292731](https://www.openstreetmap.org/relation/1292731): Troparevo-Nikulino supplement identity.
