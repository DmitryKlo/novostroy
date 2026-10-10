# City data source audit

## Bounded Moscow building sample

Fetched 2026-10-09 with the official Overture CLI `overturemaps==1.0.2`, pinned data release `2026-09-23.1` (schema v2.0.0). The request was only `building` in WGS84 bbox `[37.616, 55.750, 37.620, 55.754]` around central Moscow, approximately 0.11 km². Overture's bbox query retains full building geometries; it does not clip their vertices to the rectangle.

The raw download is [overture_moscow_sample_raw.geojson](../../data/city/overture_moscow_sample_raw.geojson), 76,582 bytes, SHA-256 `3c501d842a3234220af9f8a1d04011026901c1b1a9cdcde1c29dc92aae32d30b`. The normalized, machine-readable FeatureCollection is [overture_moscow_sample_normalized.geojson](../../data/city/overture_moscow_sample_normalized.geojson); the machine-readable provenance/counts and both file hashes are in [overture_moscow_sample_manifest.json](../../data/city/overture_moscow_sample_manifest.json). Raw feature properties are retained under normalized `properties.source_properties`; aliases include `height_m`, `floors_above_ground`, facade/roof properties, and source IDs.

Audit: 31 unique Polygon features; all have upstream `OpenStreetMap` provenance and `ODbL-1.0`; 8 have a height (4.0–34.0 m), 5 have above-ground floor counts (1–4). All 31 report OSM source version `2026-09-06T00:00:00Z`; per-record `update_time` spans `2017-10-23` to `2026-08-05`. Thirteen feature bounding boxes extend beyond the query bbox, so use the full returned geometry and deduplicate tiled downloads by Overture ID. Heights/floors are sparse. These are real footprints and source attributes, not detailed ЖК facade models.

Overture's [Buildings schema](https://docs.overturemaps.org/schema/reference/buildings/building/) permits Polygon/MultiPolygon and optional height, floors, facade/roof fields. Overture's [license page](https://docs.overturemaps.org/attribution/) assigns the Buildings theme ODbL and requires source-specific attribution; this sample's source records are OSM. Attribution: `© OpenStreetMap contributors, Overture Maps Foundation`. The [release calendar](https://docs.overturemaps.org/release-calendar/) lists release `2026-09-23.1`; public releases are retained for a limited period, so preserve the downloaded bytes and manifest for repeatability.

## Reproduction and Moscow-scale preparation

The official download-by-bbox guide documents transfer from Overture's cloud GeoParquet. With `uvx` installed, reproduce a fresh sample with:

```bash
rtk proxy python3 sources/city/overture_adapter.py \
  --bbox=37.616,55.750,37.620,55.754 \
  --release=2026-09-23.1 --label=moscow_sample_replay
```

The adapter pins CLI 1.0.2 and refuses to overwrite existing outputs. It writes raw GeoJSON, normalized GeoJSON, and a JSON manifest with observed record count, sizes and SHA-256. A failed download may leave a partial raw file; use a new label after inspecting/removing that incomplete artifact manually.

### Boundary and bounded city envelope

Retrieved one OSM API relation, not Overpass: [relation 102269 full XML](../../data/city/osm_moscow_relation_102269_full.osm), source version 422 at `2026-09-16T19:01:21Z`, fetched `2026-10-09T09:20:13Z`; 2,283,765 bytes, SHA-256 `e610095b8c3b8ada7d50d6718dd7c5e2f1a98d725b07a8de937f44da29f4d851`. The derived [WGS84 boundary GeoJSON](../../data/city/osm_moscow_city_boundary.geojson) has SHA-256 `f08ff6b0c794fd3f079ae0f9daf004f9d8de64529a7327e0fc6444788561da29`, 10 outer rings, no inner rings, and bbox `[36.8031012,55.1421745,37.9674277,56.0212238]`. Its approximate spherical area is 2,550.749 km². This is an OSM mapping polygon used as a data mask; it is not asserted to be an authoritative legal boundary. OSM license: ODbL-1.0; attribution: `© OpenStreetMap contributors`.

Using that bbox, fetched just the Overture `building` layer, with CLI `1.0.2` and pinned release `2026-09-23.1` (schema v2.0.0), under 1,000,000,000-byte and 1,200-second process limits. The rectangular envelope is about 7,169 km²; its GeoParquet is 266,855,257 bytes, SHA-256 `8899869abbd36ccc140130f8658ce7771240d7661bbbcf5054bde5db01810ca7`, with 1,885,578 rows. It completed in 144 seconds on `2026-10-09`; checkpoint is [overture_moscow_bbox_checkpoint.jsonl](../../data/city/overture_moscow_bbox_checkpoint.jsonl). The raw envelope is retained at `data/city/overture_moscow_envelope_2026-09-23.1_attempt01_20261009T092726Z.parquet`. This is bounded bbox coverage, not the whole Russian extract; retain the original before clipping.

Re-run the same release/bbox collection with the boundary already saved. The fetch script enforces the 1 GB / 20 minute caps, writes an append-only checkpoint and refuses completed duplicate runs:

```bash
rtk python sources/city/bounded_moscow_overture_fetch.py \
  --max-bytes 1000000000 --timeout-seconds 1200
```

To rebuild a clipped artifact from a new raw parquet, pass explicit paths so the existing checked output remains intact:

```bash
rtk uvx --with pyarrow==26.0.0 --with shapely==2.2.0 python \
  sources/city/clip_city_geoparquet.py \
  --input data/city/NEW_BOUNDED_ENVELOPE.parquet \
  --boundary data/city/osm_moscow_city_boundary.geojson \
  --boundary-manifest data/city/osm_moscow_city_boundary.manifest.json \
  --output data/city/NEW_CITYCLIPPED.parquet
```

The derived [city-clipped GeoParquet](../../data/city/overture_moscow_cityclipped_2026-09-23.1.parquet) is 82,529,245 bytes, SHA-256 `a905f22507f4e676479dccd984d1f338985bce9ac40c25694b21546b8e6538da`; its [manifest](../../data/city/overture_moscow_cityclipped_2026-09-23.1.manifest.json) records the input and boundary hashes, release, tool versions and counters. Output has 564,031 unique IDs and valid geometries (563,329 Polygon; 702 MultiPolygon), with no duplicates or invalid outputs. Of input rows, 1,321,542 were outside the polygon, 541 intersected and were clipped, and 5 touched only at non-polygonal results and were dropped; the counts partition all 1,885,578 rows. Height is present on 6,595 output features (1.17%); `num_floors` on 119,335 (21.16%). No row lacks `sources`; each has one source record: OpenStreetMap 289,858 (51.39%) and Microsoft ML Buildings 274,173 (48.61%). All 564,031 source records report ODbL-1.0. Preserve the per-feature `sources[]` as well as the dataset-level attribution `© OpenStreetMap contributors, Overture Maps Foundation`.

### Scalable consumer schema for S1

Use GeoParquet batches; do not load/serialize the city as one GeoJSON object. The pinned file uses Overture schema 2.0.0 and [GeoParquet 1.1](https://geoparquet.org/releases/v1.1.0/). Important columns are `id` (string), `geometry` (binary WKB, Polygon/MultiPolygon), `bbox` (xmin/xmax/ymin/ymax struct), nullable `height` (meters), `num_floors`, `min_height`, `min_floor`, `num_floors_underground`, `sources[]` (property, dataset, license, record ID, time/version), `class`, `subtype`, facade/roof fields, `has_parts`, and `version`. The GeoParquet `geo` metadata declares WKB and bbox covering; absent CRS metadata uses the GeoParquet 1.1 default OGC:CRS84 (longitude, latitude). Stream only needed columns and decode WKB within each bounded batch:

```python
import pyarrow.parquet as pq
import shapely

path = "data/city/overture_moscow_cityclipped_2026-09-23.1.parquet"
columns = ["id", "geometry", "bbox", "height", "num_floors", "min_height",
           "min_floor", "num_floors_underground", "sources", "class", "subtype",
           "facade_color", "facade_material", "roof_shape", "roof_material",
           "roof_color", "roof_height", "has_parts"]
for batch in pq.ParquetFile(path).iter_batches(batch_size=8192, columns=columns):
    geometry_idx = batch.schema.get_field_index("geometry")
    geometries = shapely.from_wkb(batch.column(geometry_idx).to_pylist())
    # Process this batch, then release it before reading the next one.
```

The raw bbox source file preserves source geometries. In the clipped derivative, fully covered geometries are kept byte-for-byte; a geometry crossing the boundary is intersected with the OSM polygon and only polygonal components retained; outside features and touch-only results are dropped. `bbox` is recomputed for each output geometry. Keep source geometry/attributes immutable for provenance and maintain rendered/extruded GLB geometry as a separate derived artifact. Any fallback extrusion height is synthetic and must be labeled; do not represent it as surveyed height or a real facade model.

**Status and limits:** the clipped file contains the Overture building footprints returned for the pinned bbox that intersect the selected OSM polygon. This does not guarantee exhaustive source coverage or prove 100% city coverage. Release, boundary relation version, checksums, metrics and all source bytes are pinned in the files above. A separately licensed terrain/elevation source is still needed for ground heights. Overture/OSM footprints are not confirmed ЖК facade models; detailed facades need licensed BIM/CAD/photogrammetry assets and project-to-building identity matching. Apartment floor slices remain later product data.

## Render-height imputation (map-only estimate)

The immutable input is [the clipped GeoParquet](../../data/city/overture_moscow_cityclipped_2026-09-23.1.parquet), SHA-256 `a905f22507f4e676479dccd984d1f338985bce9ac40c25694b21546b8e6538da`. It contains 564,031 footprints: 6,595 positive source `height` attributes, 119,335 positive `num_floors`, 117,132 floors-only, 4,392 height-only, 2,203 with both, and 440,304 with neither. All 6,595 height-attribute rows trace to OSM source records in this release; this does not claim independent survey verification. No source height, floors, or apartment attributes were overwritten.

The renderer priority is positive source `height` attribute → positive floors × 3 m → this estimated overlay → existing 15 m renderer fallback. The keyed [overlay](../../data/city/overture_moscow_render_height_imputation_v1_1.parquet) carries only `estimated_render_height_m` with `status=estimated`, donor IDs/heights, method, confidence, source missingness, and thresholds. It has 59 unique IDs (0.0134% of the 440,304 neither-height-nor-floors targets), all produced by similar-footprint matching; there were no canonical exact-geometry duplicate donors. Values are 2–20 m; all values are the minimum among suitable positive source Overture `height` attributes. The [manifest](../../data/city/overture_moscow_render_height_imputation_v1_1.manifest.json) contains source/overlay hashes, ODbL attribution, and validation links. Estimates are internal render inputs, not observed/source facts or real facade models.

The pinned profile uses OGC:CRS84 → EPSG:32637; polygon and centroid distances ≤35 m; area ratio and both minimum-rotated-rectangle dimension ratios ≥0.80; centered/rotation-normalized footprint IoU ≥0.90; exact class match (including null) and equal subtype when both are known. Similarity donors must have positive source `height` attributes of 2–300 m; underground and `has_parts` features are excluded; target footprint area is 8–40,000 m². When more than one donor passes all gates, the lowest source height attribute is selected. `confidence` is an ordinal geometry-based heuristic, not a probability. The area/dimension gates prevent scale-normalized squares from matching very different absolute sizes; class gates reject known garage/residential conflicts.

Calibration used a deterministic SHA-256 ID bucket `[0,0.20)`; the independent final test used `[0.20,0.40)`, with both buckets withheld from its donor pool. The 35 m profile matched 90/911 calibration cases (9.88%): MAE 0.411 m, median absolute error 0 m, P90 1 m; no overestimate >5 m and no absolute error >10 m. On 52/862 independent test cases (6.03%), MAE was 0.404 m, median 0 m, P90 2 m; again no overestimate >5 m or absolute error >10 m. On the same matched sets, the 15 m fallback MAE/median/P90 were 10.007/10/13 m (calibration) and 9.769/10/12 m (test). These are sparse, mapped-height holdouts, not a citywide accuracy guarantee.

A single 250 m comparison retained the same size/shape/class/minimum gates but matched 359/911 calibration cases (39.41%) with worse MAE 3.387 m, median absolute error 1 m, P90 3 m, six overestimates >5 m, five overestimates >10 m, and seven absolute errors >10 m. One null-class 6 m target received a 220 m donor estimate from 36.4 m away. The wider radius is rejected; the overlay remains at 35 m.

Reproduce from the pinned local source without fetching more data:

```bash
rtk uvx --with pyarrow==26.0.0 --with shapely==2.2.0 --with pyproj==3.7.2 python \
  sources/city/impute_render_heights.py validate --split calibration \
  --shape-iou-min 0.90 --area-ratio-min 0.80 --dimension-ratio-min 0.80 \
  --max-distance-m 35 --report data/city/REPLAY_CALIBRATION.json

rtk uvx --with pyarrow==26.0.0 --with shapely==2.2.0 --with pyproj==3.7.2 python \
  sources/city/impute_render_heights.py validate --split final_test \
  --shape-iou-min 0.90 --area-ratio-min 0.80 --dimension-ratio-min 0.80 \
  --max-distance-m 35 --report data/city/REPLAY_FINAL_TEST.json

rtk uvx --with pyarrow==26.0.0 --with shapely==2.2.0 --with pyproj==3.7.2 python \
  sources/city/impute_render_heights.py build --report data/city/REPLAY_FINAL_TEST.json \
  --output data/city/REPLAY_RENDER_HEIGHTS.parquet \
  --manifest data/city/REPLAY_RENDER_HEIGHTS.manifest.json
```

The tool refuses to overwrite artifacts. Superseded zero-match pilot reports in `data/city/` are marked with their normalization/donor-pool issue; use only the corrected calibration, 250 m comparison, and disjoint final-test reports linked by the current manifest.
