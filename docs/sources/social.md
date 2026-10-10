# Social POI source register

Review date: 2026-10-09.

| Source | Role | Evidence and constraints | Status |
| --- | --- | --- | --- |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | Candidate POI base; bounded central-Moscow samples cover pharmacy, health, education, grocery, green-space, and sport tags. | ODbL; retain attribution, source IDs/versions/tags and snapshot provenance. | Real samples fetched. Citywide import requires a pinned owned dump and coverage audit. |
| [Overpass public-use guidance](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html) | Small sample/manual check only. | Shared public service with runtime/memory quotas and load shedding. Avoid bulk or repeated citywide queries. | Two compact bbox queries saved with separate manifests; no citywide extraction. |
| [Moscow Open Data developer docs](https://data.mos.ru/developers/documentation) | Candidate official facilities/district datasets. | URL returned HTTP 403 in this review. Previously observed API-key 401 and dataset candidates 503/505/747 are not verified; schema, freshness, completeness, and terms are unknown. | Not integrated pending successful endpoint and license validation. |
| [2GIS API legal terms](https://law.2gis.ru/api-rules) | Candidate map/POI service. | Published terms restrict saving/caching and processing returned content; API access alone does not establish permission to persist a POI database. Check current product-specific terms and obtain written storage/derivative/redistribution rights. | Disabled/gated; no data fetched. |
| [Moscow Law No. 13, 8 May 2024](https://www.mos.ru/upload/documents/docs/343/Zakon13-ajbn2.pdf) | Evidence that municipal formations changed in 2024; helps distinguish them from city districts. | It is a legal act, not a ready-to-use GIS boundary file. Resolve an authoritative, versioned district boundary geometry separately. | Legal reference only; no polygons imported. |

## Bounded sample provenance

- Endpoint: `https://overpass-api.de/api/interpreter`
- Both requests used bbox order south, west, north, east: `55.7510, 37.6000, 55.7590, 37.6150`, a compact central-Moscow area inside the city boundary, not a city extract.
- The four bbox corners were checked against the OSM Moscow administrative relation 102269, version 422 (`data/city/osm_moscow_city_boundary.geojson`, SHA-256 recorded in both POI manifests). This is city-level context only, not a legal district boundary source.
- The first request selected `amenity=pharmacy`; query, OSM base timestamp, response hash, and byte count are in `data/social/manifest.json`. It returned 5 pharmacy nodes.
- The second request used `[out:json][timeout:90][maxsize:20000000];(nwr["amenity"~"^(school|kindergarten|college|university|clinic|doctors|hospital|dentist|pharmacy)$"](55.7510,37.6000,55.7590,37.6150);nwr["shop"~"^(supermarket|convenience)$"](55.7510,37.6000,55.7590,37.6150);nwr["leisure"~"^(park|playground|sports_centre|fitness_centre)$"](55.7510,37.6000,55.7590,37.6150););out meta center;`. It returned HTTP 200 at 2026-10-09T09:21:12Z, with OSM base timestamp 2026-10-09T09:18:09Z: 50 features (30 nodes, 8 ways, 12 relations), raw JSON 61,807 bytes. Observed normalized categories: pharmacy 5, healthcare 10, grocery 5, sports 10, green space 11, education 9. See `data/social/poi_manifest.json` for exact provenance and SHA-256.
- Both datasets mark `source_kind=osm` and `coverage=sample`. These are real source features, but the small bbox and observed category counts do not establish completeness even within the bbox. Normalized records preserve OSM IDs, versions, raw tags, and source coordinates. Node coordinates are not asserted to be entrances; way/relation centers are marked approximate. Entrance, ownership, quality, district assignment, and route time remain unknown.
- `sources/social/accessibility.py` deduplicates OSM records by `(type,id)`, exposes nearest POIs with great-circle meters and explicit access-point kinds, and offers an operator-configured, explicit-call Valhalla pedestrian adapter. It never converts straight-line distance into route time. The index is on the engine's 0–1 scale and is null unless requested POI and pedestrian-route coverage are complete with a source version. The current sample payload reports `coverage.status=unknown`, `coverage_completeness=unknown`, and `index=null`.
- No route source is connected, so no accessibility index is computed. The samples do not support neighborhood rankings.

## Local checks

From the repository root, run:

```bash
rtk python -m py_compile sources/social/osm_adapter.py sources/social/accessibility.py sources/social/validate_sample.py sources/social/validate_acceptance_cases.py sources/social/tests/test_accessibility.py
rtk python sources/social/validate_sample.py
rtk python sources/social/validate_acceptance_cases.py
rtk python -m unittest sources.social.tests.test_accessibility
```
