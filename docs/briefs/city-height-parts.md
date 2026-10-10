# City heights and multipart buildings: S1 integration brief

## Ready artifacts

- OSM clipped GeoParquet: `data/city/osm_moscow_buildings_parts_261008_v2.parquet` (324,375 unique polygon rows; SHA-256 `57f5719ee264c4ed17f302ae5fb51f253af9251791a9774206dd7a9229605244`). It preserves typed OSM IDs/versions/timestamps, raw tags/member refs, raw-source geometry and Moscow-clipped geometry separately. Use batch reads.
- The adapter found 1,189 clipped building relations with at least two part members carrying distinct explicit height/level values; this is a conservative tagged-example count, not the total number of multi-part buildings.
- Overture BuildingPart: `data/city/overture_moscow_building_parts_2026-09-23.1_cityclipped.parquet` (29,714 valid rows, all source-attributed to OSM/ODbL; SHA `d2fb311d589a738843f5064d12b062b0ba2e3e3711affd55a250fe6a713b66d1`).
- Strict same-version examples: `data/city/osm_overture_clean_multilevel_part_samples_261008_final.geojson` and `.manifest.json` (16 WGS84 polygons, 8 exact parent groups exported from 166 eligible groups, all union-cover ≥99%; not citywide coverage).
- Audits: `data/city/osm_overture_building_parts_field_audit_261008_verified.json` and `data/city/overture_moscow_building_parts_geometry_audit_2026-09-23.1_final.json`.

## Crosswalk and field evidence

Join `Overture sources[].record_id` (`w|r|n<ID>@version`) to OSM `(osm_type, osm_id)` first; enforce the version for field equivalence. Do not turn spatial candidates into identity: only 446 strict outline-overlap candidates are reported separately. BuildingPart `building_id` is the exact parent link: 29,714/29,714 part rows reference 6,263 unique parents, all found in clipped Building rows.

For BuildingPart exact source IDs, 29,700/29,714 join the local OSM artifact; 29,661 match object version, 39 do not, 14 are absent locally. Same-version source values agree within tolerance: height 11,306/11,306; min_height 7,381/7,381; roof_height 3,535/3,535. Older-source rows include mismatches (height 1/20, min_height 2/5, roof_height 1/1); retain both versions and values.

Use source-linked OSM semantics for these rows: `height` is ground-to-top and `min_height` is bottom offset, so vertical interval is `[min_height or 0, height]`; thickness is `height - min_height`. `roof:height` is inside total height and must not be added. Exact examples across distinct parent IDs include `r2138156@1` 80/76, `w1153978105@3` 9/3, `r3112291@1` 55/52, `r3112284@3` 85/75 with roof=10, and `r3112288@1` 43/40. Overture serializes the same fields for exact same-version source rows, although generic [Overture v2.0 BuildingPart prose](https://docs.overturemaps.org/schema/reference/buildings/building_part/) calls height a lowest-to-highest distance. This is a real schema/data ambiguity: do not apply `min_height + height` to the OSM-sourced Moscow rows and do not generalize their semantics to non-OSM sources. OSM tags are community mapping, not survey measurements; see [OSM Simple 3D Buildings](https://wiki.openstreetmap.org/wiki/Simple_3D_buildings).

## Render policy

1. For a same-version, exact-ID OSM part, use explicit positive height, then declared floors conversion only if height is absent. Keep raw OSM and Overture facts and their source versions intact. Treat `min_height` as lower Z; treat `building:min_level`/`min_floor` as distinct floor fields. Do not infer apartment interiors from these tags.
2. Link parent only through OSM relation `outline`/`part` membership or Overture `building_id`. Render parts, not a duplicate full parent shell. Suppress the shell only when part union covers ≥99% of its footprint. If parts or vertical facts are incomplete, leave gaps or review a separate residual geometry; never close a low wing with the parent maximum height.
3. Keep parts separate. The geometry audit finds 34,062 positive-area part pairs >1 m²; 25,444 have disjoint known Z intervals, 6,157 potentially overlap in 3D (6,151 same parent), and 2,461 lack complete Z intervals. Also 82 parts extend >1 m² beyond parent (2 >25 m²; max≈71 m²). Flag these; do not silently clip or merge. Keep underground features out of above-ground extrusion.
4. The clean example allowlist enforces exact source ID/version, positive OSM height, direct parent, non-underground, ≤1 m² overhang, ≥99% part coverage, at least 2 m distinct height range, and no same-parent 3D overlap above 1 m².

Source adapter, comparison and sample-selection reproduction commands are in `docs/sources/osm-building-parts.md`. No new network fetch or source PBF edit was made.
