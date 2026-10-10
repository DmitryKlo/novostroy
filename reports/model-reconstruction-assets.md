# Staged mapped-construction asset audit

Read-only audit of staged run `models-v1-92dc659b5e75fa52` (v1.3), completed 2026-10-09. The audit read local manifests, assets, assessments, and the pinned OSM construction GeoParquet. It did not make network requests, modify source or pipeline data, or change the active asset pointer.

## Result

**PASS.** The full geometry/GLB audit for v1.2 established that all 756 ready models match their exact typed/versioned OSM source footprints. For v1.3, the complete public asset-file set is byte-identical to v1.2, and all 756 new preview geometries were independently matched to the source WKB coordinate sequences. No full mesh re-decode was repeated for v1.3.

| Check | Result |
| --- | ---: |
| v1.3 assessments | 2,317 (ready 756, review 60, rejected 29, unavailable 1,472) |
| Ready models | 756: 273 volume, 483 flat-only |
| v1.3 preview geometry | 756/756 exact source coordinate sequences; Polygon 738, MultiPolygon 18 |
| Preview changes from v1.2 | 31 previously null previews now exact; 725 prior preview footprints preserved |
| Preview topology | 12 source courtyard holes retained; maximum 277 coordinate pairs per preview |
| v1.3 vs v1.2 public asset files | identical: 1,784/1,784 SHA-256s and 2,222,318 bytes |
| v1.2 vs v1.1 GLB set | identical: 1,028/1,028 content-addressed hashes |
| Full mesh QA (v1.2) | 756/756 flat meshes matched exact source footprint; max symmetric difference 0.00638 m²; 6,531 triangles retained |
| Full mesh QA (v1.2) | max vertical bound error 0.00172 m; tileset geographic regions matched source bounds with 0 rad measured error |
| OSM rights gate | 756/756 cleared, ODbL-1.0; required attribution present |
| Public metadata scan (v1.2) | 0 private metadata/path leaks in public manifests, tilesets, and GLB JSON |

The v1.3 run manifest SHA-256 is `e729ac65bcf9b436ab3dfa4b8f76c5bda643c3c2bcf9cbc33e7a9c4f18c28c6b`; its public manifest SHA-256 is `e669532499b1101ad184a35de412be43a92a72dad25dfc255aa58978100efda2`, and the assessments SHA-256 is `3646f39be1281f7a9fac9511cb9856070bd2d45efce69fc7581d32d4cc86df71`. The v1.2 full-audit run manifest SHA-256 is `a2835d1d3c86ef79b38b0ab7b0fbe3ef938cd9102374db54b5c5b435f4dd817e`; public manifest SHA-256 is `ed1d6afb04252ceb40b07f2d3a649391872db8364d668655f2724a3ad36db665`. The pinned OSM source GeoParquet SHA-256 is `6593a9ee9e4877a477f27b4bce7d214c540494c65411da0da1a11ef1dd68f2c68`.

The v1.2 audit verified every public asset file against manifest byte length, SHA-256, and content-addressed filename; checked for unmanifested files; decoded all GLBs and validated finite vertices, bounds, indices, materials, and height; compared flat triangle unions to source WKB using the GLB Y-up mapping `(x, -z, y)`; and checked the ODbL-1.0, attribution, and per-record `display_cleared=true` gates. The v1.3 audit independently verified its full public asset-file hash set against v1.2, exact preview geometry/type/coordinate sequence for each ready record, validity and finite coordinates, preservation of all 12 holes, and that non-ready records had no preview geometry.

These files depict mapped construction footprints and schematic height where available. The checks establish file integrity and correspondence to the pinned OSM geometry; they do not establish survey accuracy, current site activity, approved design, or facade fidelity. The staged root is not the active pointer.

## Reproduction

Run the full GLB/footprint audit for v1.2:

```bash
rtk .runtime/geodata-env/bin/python integration/audit_model_assets.py --run-dir data/models/models-v1-9fabfcc68bd1811f --compare-run-dir data/models/models-v1-b553758cd13765aa
```

Run the v1.3 preview and complete public asset-set comparison:

```bash
rtk .runtime/geodata-env/bin/python integration/audit_model_assets.py --preview-only --run-dir data/models/models-v1-92dc659b5e75fa52 --compare-run-dir data/models/models-v1-9fabfcc68bd1811f
```

The auditor is read-only, writes no files, opens no network connections, and does not activate or change a staged pointer.
