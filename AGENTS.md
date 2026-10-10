# Novostroy repository guidance

Run host commands through `rtk` as specified in `/home/pirojban/.codex/RTK.md`. Use Python 3.12+, type hints and explicit source timestamps. Keep `contracts/` versioned and update the backend, web, mobile, and integration checks together when changing wire fields.

The runnable mode is `demo`: 100 fictional complexes, 1000 illustrative asking offers, and procedural facade examples. `real` mode must never load demo prices or inventory. City footprints may come from source-backed Overture/OSM data, but any extrusion is synthetic massing. Label source, geometry quality, rights status, coverage, and snapshot date separately. No exterior model proves unit/floor internals. Do not claim a licensed facade, current availability, citywide completeness, or an iOS build without evidence.

Use `recommendations/engine.py` for all map, search, and recommendation eligibility. Hard budget, district, complex, room, area, and price-basis filters apply before ranking. Unknown or stale real prices are excluded from current offers; missing social data never becomes a rating or zero count. `quality/acceptance_cases.json` has 100 independent cases; `integration/run_acceptance.py` executes them. Run relevant Python tests and the live contract check before reporting integration readiness. For web and Unity, also run the checks documented in their own READMEs.

Local generated/large files are excluded by `.gitignore`: `.runtime/`, virtual environments, node modules, raw downloads, GeoParquet, GLB/b3dm, demo catalogue, browser artifacts, and device builds. Keep immutable source manifests and attribution with any data export. Do not store API keys or personal data in the repository or analytics events.
