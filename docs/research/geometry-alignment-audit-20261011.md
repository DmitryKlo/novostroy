# Selected-model alignment and Lake facade audit — 2026-10-11

The selected-model renderer applies an unintended **90° counterclockwise yaw**. The producer geometry, metre units and ENU matrices are correct in the ten audited examples. Setting `forwardAxis: Cesium.Axis.X` on the standalone `Cesium.Model.fromGltfAsync` restores source-footprint agreement to IoU ≥ 0.99999990. Do not rotate or distort the producer coordinates to compensate.

This audit changed no application code, active pointer, source data or model asset. `sol_web_refresh` owns the renderer correction and camera change. The public-site observations below were captured **before its deployment**, at `2026-10-10T22:51:12.964Z` / `2026-10-11 01:51 Moscow`.

## Pinned scope and reproducibility

- Map: `developer-map-v1-7092646e252c8681`.
- Apartment menu: `developer-units-v1-e527bb9c8cd04378`.
- Selected models: `developer-models-v1-5a525ff1aaba871b`: 856 GLBs, comprising 855 schematic volumes and one photo-informed, generated-texture Lake reconstruction.
- Flat ground: `/assets/public-ground-v3/tileset.json`; source `data/city/overture_moscow_cityclipped_2026-09-23.1.parquet`, snapshot 2026-09-23. All closest sampled source features originate from OSM, as recorded by Overture.
- Sample: Lake plus nine deterministic farthest-point selections distributed across the active model extent. One bounded parquet query returned 126 nearby source rows. Only ten selected GLBs and ten local ground cell GLBs were decoded. This is a diagnostic sample, not a citywide matching survey.

Run from the repository root:

```bash
rtk proxy .venv/bin/python -W ignore .runtime/research/geometry-alignment-20261011/audit.py
rtk proxy node .runtime/research/geometry-alignment-20261011/browser-audit.mjs
```

The browser reproduction opens `https://79.141.74.15/`, searches `Лэйк`, selects the exact building card, waits 15 seconds, captures the original framing, then presses the existing zoom button once. It reads the Cesium model/viewer state through the page's React references because production intentionally disables the public diagnostic hook. No browser state persists after the isolated context closes.

Full numerical results, IDs, source versions and hashes: [geometry-audit.json](../../.runtime/research/geometry-alignment-20261011/geometry-audit.json). Polygon evidence: [geometry-samples.geojson](../../.runtime/research/geometry-alignment-20261011/geometry-samples.geojson). Live request/model evidence: [browser-audit.json](../../.runtime/research/geometry-alignment-20261011/browser-audit.json). Checksums: [manifest.json](../../.runtime/research/geometry-alignment-20261011/manifest.json).

## Why the rotation happens

`sources/models/geometry.py:62` stores each glTF position as `(east, up, -north)`. `pipeline/build_tiles.py:56` returns a column-major WGS84 ECEF-from-ENU transform with unit orthogonal axes. The selected model passes that matrix unchanged to Cesium.

The installed Cesium engine uses different defaults for the two loading paths:

| Path | Up axis | Forward axis | Effective local transform |
| --- | --- | --- | --- |
| City/ground `Cesium3DTileset` | Y | X | `(E,U,-N) → (E,N,U)` |
| Original standalone `Model.fromGltfAsync` | Y | Z | `(E,U,-N) → (-N,E,U)` |

`web/node_modules/@cesium/engine/Source/Scene/Model/ModelUtility.js:272` applies the Y-up conversion; line 278 additionally multiplies `Axis.Z_UP_TO_X_UP` when forward axis is Z. `GltfLoader.js:217` supplies that Z default; `Cesium3DTileset.js:2312` supplies X for tile contents. An independent Node import of the installed `ModelUtility.getAxisCorrectionMatrix` confirms the same matrices. The pre-fix public browser model also reports the Z-forward correction matrix `[0,1,0,0, 0,0,1,0, 1,0,0,0, 0,0,0,1]`.

The audit decodes actual GLB roof triangles, applies the axis correction and row's full 4×4 matrix, reverses ECEF to WGS84, and compares against that row's exact pinned source polygon in its local metre frame. No fitted translation, rotation or scale is used. All ten producer matrices have unit axes and determinant 1 to floating-point precision. Corrected maximum boundary residual is **0.00000343 m**; original residual reaches **67.75 m**.

## Footprint results and independent ground differences

IoU is intersection area divided by union area. “Ground IoU” below compares the selected model's original source polygon with the best-overlapping local Overture source polygon, **not an inferred building identity**. For zero-overlap samples the closest candidate is a neighbour; moving the model there would be wrong.

| Selected building | Original renderer/source IoU | Corrected renderer/source IoU | Ground/source IoU | Ground-source boundary difference |
| --- | ---: | ---: | ---: | ---: |
| Lake, corpus 1 | 0.464804 | 0.999999967 | **1.000000** | 0.00 m |
| Троицкий, corpus 6 | 0.086067 | 0.999999929 | 0 | No overlapping ground feature |
| Зеленый парк, corpus 5.6 | 0.337279 | 0.999999962 | 0 | No overlapping ground feature |
| ПаркСайд, corpus 1 | 0.246315 | 0.999999970 | 0.760897 | 4.60 m |
| СберСити, В16.1 Балансд | 0.153953 | 0.999999955 | 0.592587 | 8.89 m |
| Крекшино, К-1 | 0.212922 | 0.999999979 | 0 | No overlapping ground feature |
| Ситимикс Новокосино, corpus 1 | 0.228795 | 0.999999965 | 0.560968 | 19.08 m |
| Алхимово, corpus 12 | 0.485320 | 0.999999951 | 0.918511 | 1.30 m |
| Родина Переделкино, corpus 2 | 0.527049 | 0.999999946 | 0 | No overlapping ground feature |
| 1-й Ленинградский, corpus 5 | 0.326546 | 0.999999908 | 0.803637 | 2.87 m |

Lake is the strongest exact-identity control:

- Selected registry ID: `registry-b1feab9d36bced349dfa4b1d`; FSK building ID `639b34e2b176d8bf3ad3d6b1`; address Снежная улица, 20А.
- Selected source: exact OSM way `1317814900`, version 2.
- Ground ID: `3373b665-7676-478b-badd-e51b8bcf1af8`; source record `w1317814900@2`, OSM update `2026-04-18T04:51:58.000Z`, planet version `2026-09-06T00:00:00.000Z`.
- Both source polygons agree exactly: area 1216.23 m², IoU 1. The decoded ground asset `assets/public-ground-v3/cell_3764_5585.glb`, including every nested tileset transform, agrees with that source at IoU 0.999999463 and maximum boundary error 0.0000176 m.

The other selected models use Proplanirovki `aggregator_unverified` polygons collected on 2026-10-10, whereas ground uses an independent Overture/OSM snapshot. Exact developer-to-OSM identity was not established for those nine buildings. Their residual ground differences cannot be attributed confidently to “newer geometry”, nor can either source be declared correct from these spatial overlaps alone. Actual decoded ground assets agree with their own pinned source polygons at IoU ≥ 0.99999815 and boundary error < 0.000027 m, excluding a second ground-transform error in this sample.

Producer handoff: preserve exact source coordinates. If a persistent selected-ground outline is desired, derive it from the same selected source geometry and label its provenance. Do not snap to the nearest city polygon, borrow a neighbour's footprint, or replace city geometry without a separately verified crosswalk.

## Why the facade is hard to recognize

The live Lake metadata and content-addressed GLB returned HTTP 200, with no page errors. The actual model was `ready=true`, `_texturesLoaded=true`, and `_loader.texturesLoaded=true` at capture time. Its allocated texture memory was 22,369,621 bytes. Missing texture download or an unfinished incremental texture upload did **not** explain this captured view.

The GLB contains a valid embedded 2172×724 PNG, SHA-256 `d48441f62051b570206fdd86f02886dfd2a247576bf03a73f480b124f4febaef`. The wall primitive has `TEXCOORD_0`, a `baseColorTexture`, `KHR_materials_unlit`, double-sided rendering, and repeat wrap in both axes. UVs span U=0…15.8836 and V=0…10; the intended repeat is one generated three-bay strip per 9 m horizontally and one story per 3 m vertically. The roof is intentionally untextured.

At the old fixed selection framing (range 470 m, pitch −48°), the 1440×1000 browser's 930 px-wide map shows the 30 m facade height across **32.84 CSS pixels**, only **3.28 px per story**. Texture details blend into a gray striped pattern. One existing zoom-button click makes the horizontal bands and window repeats more apparent:

- [Public original view](../../.runtime/research/geometry-alignment-20261011/lake-public-default.png)
- [Public one-step closer view](../../.runtime/research/geometry-alignment-20261011/lake-public-closer.png)
- [Actual embedded texture](../../.runtime/research/geometry-alignment-20261011/lake-embedded-texture.png)

Renderer handoff: keep explicit forward axis X; observe `texturesReadyEvent` rather than treating `ready` as texture completion; use a closer and shallower selected-building view and verify readable pixels on desktop/mobile. `sol_web_refresh` is implementing and validating that view independently.

Coverage remains a separate issue: 855/856 selected models are plain schematic volumes. Lake is a generated, photo-informed repeating facade on a 30 m schematic extrusion, not a photo unwrap, verified facade, official 3D model or measured building. The known 16/10-floor source sections have no mapped part boundaries; the producer deliberately uses the 10-floor minimum and does not reconstruct the real stepped silhouette. Correcting yaw and framing does not change those evidence limits.
