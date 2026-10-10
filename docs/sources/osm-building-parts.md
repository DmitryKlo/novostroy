# Moscow OSM building parts and height tags

## Pinned inputs and licensing

The adapter reads the existing Geofabrik Central Federal District PBF, `data/social/osm_moscow_city_261008.osm.pbf` (79,913,674 bytes, SHA-256 `78aff2d4f6707c584f66febd8fb594c679b434f9046562d8d6a2c40d55da6014`). Its pinned manifest records Geofabrik `source_last_modified=2026-10-08T22:25:33Z`, OSM replication timestamp `2026-10-08T20:21:06Z`, and last object timestamp `2026-10-08T20:19:30Z`. The PBF is filtered locally for `w/building`, `w/building:part`, `r/building`, `r/building:part`, and `r/type=building`; the full PBF is neither changed nor re-downloaded.

Footprints are clipped to the pinned OSM Moscow relation 102269, version 422, timestamp `2026-09-16T19:01:21Z` (boundary SHA-256 `f08ff6b0c794fd3f079ae0f9daf004f9d8de64529a7327e0fc6444788561da29`). This is an OSM mapping boundary, not a certified legal limit. OSM data is ODbL-1.0; attribution: `© OpenStreetMap contributors`. The Overture Building and BuildingPart inputs use release `2026-09-23.1`, schema v2.0.0, and the pinned extracts report ODbL-1.0; attribution: `© OpenStreetMap contributors, Overture Maps Foundation`.

## Outputs and scope

The final v2 OSM file has 324,375 clipped polygonal rows, all unique by `(osm_type, osm_id)`, with source and clipped geometries kept separately. Its SHA-256 is `57f5719ee264c4ed17f302ae5fb51f253af9251791a9774206dd7a9229605244` (82,722,680 bytes). The manifest carries feature-kind counts, parsed-value/status counts, source-ID crosswalk metrics, and boundary/source hashes. The final files are:

- [OSM building/part GeoParquet](../../data/city/osm_moscow_buildings_parts_261008_v2.parquet) and [manifest](../../data/city/osm_moscow_buildings_parts_261008_v2.manifest.json).
- [Multilevel part examples with clipped polygons](../../data/city/osm_moscow_multilevel_building_examples_261008_v2.geojson).
- [Overture BuildingPart clipped extract](../../data/city/overture_moscow_building_parts_2026-09-23.1_cityclipped.parquet), its [manifest](../../data/city/overture_moscow_building_parts_2026-09-23.1_cityclipped.manifest.json), and the final [parent/overlap audit](../../data/city/overture_moscow_building_parts_geometry_audit_2026-09-23.1_final.json).
- Final [exact-ID field/version audit](../../data/city/osm_overture_building_parts_field_audit_261008_verified.json) and [strict multilevel sample](../../data/city/osm_overture_clean_multilevel_part_samples_261008_final.geojson) with [selection manifest](../../data/city/osm_overture_clean_multilevel_part_samples_261008_final.manifest.json).

GeoParquet rows carry typed `osm_type`/`osm_id`, OSM object version/timestamp, `entity_kind`, original source geometry WKB, Moscow-clipped geometry WKB, raw tag JSON, relation member references and roles, plus normalized nullable height/floor/roof fields and per-field parse status. Parsed values do not replace raw OSM tags. Relations are retained as distinct rows; tagged and untagged member ways are retained when they form polygonal part/outline features. Coverage is the set of returned PBF objects that can be represented as polygonal geometry and intersect the selected boundary; it is not a complete inventory of Moscow buildings. Missing tags do not establish absence.

The OSM-only [example GeoJSON](../../data/city/osm_moscow_multilevel_building_examples_261008_v2.geojson) includes six `type=building`/part relations with clipped polygons and OSM version/time provenance. Three examples are: relation 8909665, part ways 23108982 (12 levels) and 53794811 (1 level); relation 8909664, ways 23108992 (12) and 53809016 (1); relation 8909342, ways 23108993 (12) and 53809017 (1). These are actual OSM mapped sections, not survey-confirmed or named ЖК facade models.

The adapter found 1,189 clipped relations whose `part` members include at least two distinct explicit height or level values. This is a conservative count of relations with visible differing tags, not a complete multipart-building count; many valid parts have no height/levels tags or are not grouped by a relation.

## Source IDs and field meanings

Overture `sources[].record_id` values such as `w159755858@1` are parsed as exact typed OSM ID plus source version and joined to `(osm_type, osm_id)`. An ID match establishes record linkage, not measurement accuracy or equality of every field. Version differences are recorded. If no ID exists, the adapter can report only a strict geometry candidate for an outline (IoU at least 0.85, area ratio 0.80–1.25, and at least 0.05 IoU lead); it is explicitly not an identity join. No nearest-building association is made. Overture `BuildingPart.building_id` links to the parent `Building.id`; the pinned Moscow extract has 29,714 non-null part links across 6,263 parent IDs, all of which exist in the clipped 564,031-row Building table.

The adapter preserves `height`, `min_height`, `building:levels`, `building:min_level`, `roof:height`, `roof:levels`, `roof:shape`, and `roof:direction` from OSM on ways and selected relations. Bare height values default to metres. The parser also accepts spaced `m` aliases, `km`, `mi`, `nmi`, `yd`, `ft`/`feet`, inches and combined `12'5"` notation; unsupported syntax stays in the raw tag and yields a null normalized value. Floor counts accept only scalar counts. `height` is not a survey flag: these are community-mapped OSM tags, and `source:height` metadata does not independently prove a survey.

For OSM Simple 3D Buildings, `height` is the ground-to-top value while `min_height` is the bottom offset; extrusion thickness is `height - min_height` when both are present. `roof:height` is part of the total height, not an amount to add. OSM `building:levels` describes above-ground floors; `building:min_level` describes skipped lower levels in a part. [OSM Simple 3D Buildings](https://wiki.openstreetmap.org/wiki/Simple_3D_Buildings) states that the building outline represents the union of its parts, and the outline is not used for 3D rendering when parts exist; [OSM relation:building](https://wiki.openstreetmap.org/wiki/Relation:building) defines the `outline`/`part` member roles. Unit handling follows [OSM Map Features/Units](https://wiki.openstreetmap.org/wiki/Map_Features/Units).

Overture's [BuildingPart v2.0.0 schema](https://docs.overturemaps.org/schema/reference/buildings/building_part/) describes `height` as a lowest-to-highest distance, `min_height` as the bottom offset, and `roof_height` as roof rise. However, exact source-ID matches in this OSM-only city extract show that Overture serializes the source OSM `height` and `min_height` values without transforming them. For example, `r3112284@3` has OSM `height=85`, `min_height=75`, `roof:height=10`, and the exact Overture part row has `height=85`, `min_height=75`, `roof_height=10`. Treat this pinned source-linked extract as OSM semantics: interval `[min_height, height]`; do not compute `min_height + height` or add `roof_height`. The generic Overture wording remains an unresolved ambiguity for records from other providers and must not be generalized beyond this OSM-backed sample.

| Exact OSM `record_id` | OSM height / min_height | Overture height / min_height | OSM / Overture roof height | Parent |
|---|---:|---:|---:|---|
| `r2138156@1` | 80 / 76 m | 80 / 76 m | — / — | `574adf30-4e3d-4b57-afce-a80a2acc605c` |
| `w1153978105@3` | 9 / 3 m | 9 / 3 m | — / — | `d2450a92-19a4-46d5-9911-48e0bb393df1` |
| `r3112291@1` | 55 / 52 m | 55 / 52 m | — / — | `bd60ee19-743d-4d99-8e34-e5e90343796b` |
| `r3112284@3` | 85 / 75 m | 85 / 75 m | 10 / 10 m | `93f74080-2eee-4f1e-bae1-b2852e2a5e0a` |
| `r3112288@1` | 43 / 40 m | 43 / 40 m | — / — | `ba8ae07a-cbc3-4074-ac09-16be22d5b73f` |

The clipped Overture BuildingPart extract has 29,714 source rows; 29,700 exact OSM IDs join to the local clipped OSM artifact (29,661 with matching OSM object versions; 39 with a different version; 14 IDs absent locally). In the full Building/BuildingPart exact-ID comparison, same-version values agree within tolerance for height (17,833/17,833), min-height (7,413/7,413), and roof height (3,902/3,902). For BuildingPart rows alone, same-version equality is height 11,306/11,306, min-height 7,381/7,381, and roof height 3,535/3,535. Older source-version BuildingPart rows have height differences in 1/20, min-height 2/5, roof height 1/1; preserve both versions/values rather than overwrite. Across Building and BuildingPart exact matches, floors have 14 differences of at least one floor; inspect source-version provenance before choosing a value.

`roof:direction` and Overture `roof_direction` are preserved independently: OSM documents a direction toward the main roof face, while Overture documents a ridge-line bearing, so a numeric difference is not reported as a data conflict. Raw roof shape values can be compared for exact equality, but differing vocabularies are not promoted to a conflict without a mapping.

## Rendering contract for S1

For an exact, same-version OSM-linked part with valid positive `height`, render its vertical interval as `[min_height or 0, height]`; use explicit positive `building:levels` only under a separately declared floor-to-metre rule when height is absent. Overture `height`, `min_height`, `num_floors` and `min_floor` remain separate source facts; never overwrite one with another. If OSM and Overture source versions differ, prefer the newer pinned local OSM value for rendering and retain both values and versions for audit.

When a building has parts, render the part volumes and suppress the parent outline's full extrusion only when the part union covers at least 99% of the outline and every rendered part passes the source, height, parent, underground and overlap gates. The outline remains a 2D/context footprint and the parent/part association must come from relation roles or `BuildingPart.building_id`, never proximity. If part geometry or source heights are incomplete, do not fill the whole parent at its maximum height; leave the uncovered area unextruded or render only a separately identified residual after review. Keep overlapping part geometry separate and resolve 3D visibility from their vertical intervals; do not merge into one maximum-height block. A `building:part=roof` feature is roof geometry, not a full facade volume. Preserve roofs as a separate roof component; never add `roof:height` on top of total `height`.

The Overture audit reports 82 part polygons with more than 1 m² outside their exact parent footprint (2 exceed 25 m²; largest about 71.05 m²). It also finds 34,062 pairs with positive-area overlap above 1 m²; 25,444 have known non-overlapping vertical intervals, 6,157 have apparent overlapping 3D intervals (6,151 within a single parent; 6 cross-parent), and 2,461 lack a complete vertical interval. These are geometric diagnostics, not proof of bad source geometry: retain parts separately, do not silently clip them to parent, and do not collapse overlap pairs. The audit uses a local approximate metric projection and a 1 m² threshold.

The [strict clean sample](../../data/city/osm_overture_clean_multilevel_part_samples_261008_final.geojson) selected 8 parent groups / 16 parts from the 29,714 rows. Every selected part has same-version exact OSM source identity, positive height, a direct parent link, no more than 1 m² outside the parent, no overlapping 3D volume above the area threshold, and belongs to a part union covering at least 99% of its parent. The first parent `70aa6147-a8e5-466a-968f-d105a4098733` (center 37.531456, 55.670429) has `w36515115@8` at 66 m and `w1452436979@1` at 6 m; both are exact current OSM versions. Those are QA examples, not a city coverage claim.

## Reproduction

Run from the project root; choose unused output names because the adapter refuses to overwrite files:

```bash
rtk .runtime/geodata-env/bin/python sources/city/osm_building_parts.py \
  --output data/city/osm_moscow_buildings_parts_261008_repro_20261009.parquet \
  --examples data/city/osm_moscow_multilevel_building_examples_261008_repro_20261009.geojson
rtk .runtime/geodata-env/bin/python sources/city/audit_overture_building_parts.py \
  --output data/city/overture_moscow_building_parts_geometry_audit_repro_20261009.json
rtk .runtime/geodata-env/bin/python sources/city/audit_osm_overture_building_fields.py \
  --output data/city/osm_overture_building_parts_field_audit_repro_20261009.json
rtk .runtime/geodata-env/bin/python sources/city/select_clean_multilevel_parts.py \
  --output data/city/osm_overture_clean_multilevel_part_samples_repro_20261009.geojson
rtk .runtime/geodata-env/bin/python -m unittest sources.city.tests.test_osm_building_parts -v
```

The adapter streams output in batches. It makes no network calls; all inputs are pinned local artifacts. Both scripts record input hashes and version/boundary provenance in manifests or audit reports.
