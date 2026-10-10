# Full reviewed multilevel building-part allowlist

Canonical S1 input: `data/city/osm_overture_clean_multilevel_parts_full_261009.parquet` (GeoParquet 1.1, WKB, OGC:CRS84). A matching feature collection with the same 800 selected IDs and the QA-compatible core fields is `data/city/osm_overture_clean_multilevel_parts_full_261009.geojson`. The existing eight-group QA sample remains unchanged at `data/city/osm_overture_clean_multilevel_part_samples_261008_final.geojson`.

## Contents and gates

The full allowlist has 800 part polygons in 166 parent groups. Each row carries Overture part and parent IDs, exact OSM source record ID, OSM type/ID/version, OSM raw and parsed height/min_height/roof_height/levels, Overture height/min_height/floors/roof fields, `vertical_base_m`, `height_top_m`, `vertical_thickness_m`, parent part count and footprint coverage, gate profile, passed-gate list, and geometry. The OSM crosswalk is exact by object type, ID, and version; all 800 selected part/source IDs are unique. All source matches in this export have `is_underground=false`.

Group gates require every parent part row to pass the row-level source/version/attribute/geometry checks; 2–12 parts; an exact Overture parent; each part no more than 1 m² outside that parent; part-union coverage at least 99%; at least 2 m difference between minimum and maximum top elevations; and no same-parent pair with intersection area over 1 m² and vertical overlap over 0.1 m. The 166 groups have no detected same-parent 3D overlap under those thresholds. This describes selector outcomes, not a claim that all source overlaps are errors.

For these OSM-linked same-version records, `height` is the top elevation above ground and `min_height` is the bottom elevation; render thickness is `height - min_height`, with missing min_height treated as zero. `roof_height` is already within total height and must not be added. These are community-mapped OSM attributes, not survey measurements. The part geometry is the clipped Overture geometry; original unclipped geometry is not included here. For rendering selected groups, use the selected part solids and do not overlay a complete parent extrusion across them. Their union covers at least 99% of the parent; the row fields retain the measured residual for any separate policy.

## Volume, exclusions, provenance

The pinned clipped input contained 29,714 Overture BuildingPart rows associated with 6,263 parent IDs. This run selected 166 groups / 800 parts and records the other 6,097 parent IDs in `data/city/osm_overture_clean_multilevel_parts_full_261009.excluded_parents.jsonl`. That file assigns one primary group-stage reason per parent and lists row-level reasons where present. Reason counts are in the manifest; row-level reasons may overlap. A group rejected for a geometry-overlap gate is not thereby declared source-invalid. This is a conservative subset, not an exhaustive Moscow set.

Input pins from the manifest: OSM-derived source table SHA-256 `57f5719ee264c4ed17f302ae5fb51f253af9251791a9774206dd7a9229605244`; Overture BuildingPart clipped input SHA-256 `d2fb311d589a738843f5064d12b062b0ba2e3e3711affd55a250fe6a713b66d1`; parent buildings input SHA-256 `a905f22507f4e676479dccd984d1f338985bce9ac40c25694b21546b8e6538da`. Overture release is `2026-09-23.1`; OSM data comes from the already-held local extract. Data attribution: © OpenStreetMap contributors, Overture Maps Foundation; OSM-derived content is ODbL-1.0.

Manifest with counts, every selected parent ID, output hashes, and filter profile: `data/city/osm_overture_clean_multilevel_parts_full_261009.manifest.json`. Full canonical Parquet SHA-256: `52f18decc192ef034cd36f9e8d3b1720264004ca3dbe01cc0c89f0202c4a3ae6`.

## Reproduction

From the repository root, using the existing geodata environment and only the pinned local inputs:

```bash
.runtime/geodata-env/bin/python sources/city/select_clean_multilevel_parts.py \
  --all --limit 8 \
  --output data/city/osm_overture_clean_multilevel_parts_full_261009.geojson \
  --parquet-output data/city/osm_overture_clean_multilevel_parts_full_261009.parquet
```

The selector refuses to overwrite outputs. `--all` exports all eligible groups; `--limit` only controls the default QA subset when `--all` is absent.
