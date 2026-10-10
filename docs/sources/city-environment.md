# Moscow environmental and district data audit

Reviewed 2026-10-09. The machine-readable product matrix is [environment.json](../criteria/environment.json). This document records only primary-source findings and local extraction evidence; mapped proximity is kept separate from measured exposure.

## Official environmental data and access

The [2024 Moscow environmental report](https://www.mos.ru/upload/content/files/ce91e7d457ac339d0b3ceb1fd881298e/DokladosostoniiokryjausheisredivgorodeMokvev2024gody.pdf) links the mobile air-laboratory dataset as [dataset 2457](https://data.mos.ru/opendata/2457), calls it “Данные передвижной лаборатории контроля загрязнения атмосферного воздуха,” and says it is updated monthly with measurement date, place, and visit results. The same report says current automatic-station observations are displayed through MosEcoMonitoring, but a stable machine endpoint, schema, retention, and reuse terms were not confirmed in this audit.

For noise, the report identifies a monthly open-data set titled “Результаты мониторинга уровней шума по данным передвижной экологической лаборатории,” with measurement address, date, and visit results. Its current dataset ID and row fields were not verified. These are field observations from mobile surveys, not a continuous acoustic grid or a district score. The report describes monitoring activity tied to citizen or agency reports; no measurement at a location must remain unknown, not “quiet.”

On 2026-10-09, the official API `https://apidata.mos.ru/version` returned HTTP 200 with `Version=1`. Requests to `/v1/datasets/2457` and `/v1/datasets/2457/rows?$top=1` returned HTTP 401 and the portal message that `api_key` is required. No key was available, no rows were fetched, and no credential was recorded. The [developer documentation](https://data.mos.ru/developers/documentation) is an SPA; it did not yield a static schema in this bounded check. Portal terms and live schema still need review after authorized access.

No current primary source was verified that publishes a cleanliness rating for each Moscow district. Station readings and mobile surveys are not a valid substitute for that rating. Keep `air.district_cleanliness_rank` unknown until a current district-level source and its method are found.

## District polygons and version

The legal references are [Moscow Law No. 14 of 2024](https://vestnikmoscow.mos.ru/wp-content/uploads/2024/05/zhurnal-vestnik-moskvy-%E2%84%96-27.pdf), which amends the city territorial-division law, and [Government Decree No. 967-PP of 2024-05-14](https://www.mos.ru/upload/documents/docs/9303/967-PP-bngh1.pdf), whose district-administration list is numbered through 133. These are legal texts, not a current GIS polygon download. The Overture divisions guide also warns that sub-county coverage may be spotty ([schema](https://docs.overturemaps.org/schema/reference/divisions/division_area/), [guide and licensing](https://docs.overturemaps.org/guides/divisions/)).

For a real, versioned map candidate, fetched only Overture `division_area` for the existing Moscow city-boundary envelope `[36.8031012, 55.1421745, 37.9674277, 56.0212238]` (about 7,169 km²) from release `2026-09-23.1`, schema `2.0.0`, with a 32 MB file-size cap and 300 s timeout. The completed GeoParquet has 2,798 rows, 17,615,648 bytes, SHA-256 `e81cfa02e68a7bd99dae58ba83fb3a8c7978b10ddcbac02364025b3aeec83cef`; see [fetch manifest](../../data/city/environment/overture_division_area_2026-09-23.1_attempt01_20261009T100517Z.manifest.json) and [checkpoint](../../data/city/environment/fetch_checkpoint.jsonl). Two overlapping attempts completed with identical hashes; both manifests are retained. Use attempt01 as the canonical source file.

The normalized [district-candidate GeoJSON](../../data/city/environment/moscow_district_candidates_2026-09-23.1.geojson) contains 130 unique named polygons with valid geometries and upstream OSM relation IDs/versions/timestamps; SHA-256 `113724900f2e90e9210255c4857c0eb1c30f030c68ba9d43c5477697e5beb060`. One polygon was clipped to the stored city mask, OSM relation 102269 version 422. The candidate union covers 98.817697% of that mask in a planar WGS84 degree-area ratio; 1.182303% remains uncovered and there is a small overlap. The audit is [machine-readable](../../data/city/environment/moscow_district_candidates_audit.json). These metrics are a geometry QA signal, not a legal completeness score. The candidate count of 130 differs from the 2024 administration-list index 133. District assignment and district aggregates therefore stay blocked pending a current authoritative GIS set and name/coverage reconciliation.

Overture Divisions are ODbL and require source-aware attribution. This extraction's source rows include OpenStreetMap; retain `sources[]` and upstream OSM IDs/versions. Attribution: `© OpenStreetMap contributors, Overture Maps Foundation` ([Overture attribution](https://docs.overturemaps.org/attribution/), [OSM license](https://www.openstreetmap.org/copyright)).

## Playground sample and proximity helper

No new Overpass call was made. The [playground pilot](../../data/city/environment/playgrounds_osm_pilot.geojson) derives six `leisure=playground` elements from the existing [bounded social snapshot](../../data/social/poi_manifest.json): bbox `[55.751, 37.600, 55.759, 37.615]` in Overpass south/west/north/east order, OSM base time `2026-10-09T09:18:09Z`, retrieved `2026-10-09T09:21:12Z`, ODbL 1.0, attribution `© OpenStreetMap contributors`. One geometry is a source node point; five use Overpass centers and are explicitly approximate. Coverage completeness remains unknown.

[environment_derived.py](../../sources/city/environment_derived.py) computes local equirectangular point-to-geometry distance in metres, handles lines, polygon boundaries and holes, and returns the reference feature ID and derivation method. It supports a pinned policy selecting road classes `motorway`, `trunk`, `primary`, and mapped `aeroway=aerodrome` geometries, but no city road-network or aerodrome dataset has been collected here. These distances remain explicit map proximity proxies; they do not estimate dB(A), aircraft routes, or acoustic exposure.

An offline run against the existing Overture building pilot (31 footprints) and this six-feature OSM sample is in [environment_proxy_smoke.json](../../data/city/environment/environment_proxy_smoke.json). Its median nearest mapped playground-center distance is 626.1 m; the 500 m mapped-feature count ranges 0–1. These values only exercise the adapter against two small samples. A zero means no feature was found in this query result, not confirmed real-world absence.

Reproduction from a clean checkout or with a new output path:

```bash
rtk python sources/city/bounded_environment_fetch.py \
  --type=division_area \
  --bbox=36.8031012,55.1421745,37.9674277,56.0212238 \
  --max-bytes=32000000 --timeout-seconds=300
rtk uvx --system-certs --with pyarrow==26.0.0 --with shapely==2.2.0 python \
  sources/city/normalize_overture_districts.py \
  --output=data/city/environment/moscow_district_candidates_replay.geojson
rtk uvx --system-certs --with pyarrow==26.0.0 --with shapely==2.2.0 python \
  sources/city/audit_overture_districts.py \
  --candidates=data/city/environment/moscow_district_candidates_replay.geojson \
  --candidate-manifest=data/city/environment/moscow_district_candidates_replay.manifest.json \
  --output=data/city/environment/moscow_district_candidates_replay_audit.json
rtk python sources/city/normalize_osm_playground_sample.py \
  --output=data/city/environment/playgrounds_osm_replay.geojson
rtk python sources/city/smoke_environment_sample.py \
  --output=data/city/environment/environment_proxy_smoke_replay.json
rtk python -m unittest sources.city.tests.test_environment_derived
```
