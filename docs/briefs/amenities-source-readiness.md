# Moscow amenities: local sources and update readiness

Reviewed 2026-10-10. The currently usable citywide physical-feature base is the pinned local OSM extract, not a verified city inventory. Its source PBF is `data/social/osm_moscow_city_261008.osm.pbf` (OSM snapshot `2026-10-08T20:21:06Z`, SHA-256 `78aff2d4f6707c584f66febd8fb594c679b434f9046562d8d6a2c40d55da6014`). The POI derivative is `data/social/osm_moscow_social_pois_261008.parquet`; its manifest carries source IDs, per-object edit time, WKB geometry, raw tags, CRS84, rights, counts and coverage caveats. Rows are ODbL-1.0 and require `© OpenStreetMap contributors` attribution. OSM feature completeness is explicitly unknown.

## Usable local layers

| Layer | Export rows → distinct OSM IDs | Physical geometry and limits |
|---|---:|---|
| Metro station | 276 → 276 | All source points tagged as subway stations. They are station points, not public entrances. Use entrance layer for access distance. |
| Metro entrance | 1,272 → 1,272 | All are point features tagged `railway=subway_entrance`; every row has the explicit `entrance_known` access point. This is the strongest current metro distance/routing layer, though station/entrance completeness is unknown. |
| School | 3,484 → 2,121 | `amenity=school`; node points and polygon footprints are available. 1,363 line rows duplicate the same source way/category as its polygon; deduplicate `(osm_type, osm_id, category)` and prefer area geometry. |
| Kindergarten | 4,811 → 2,881 | `amenity=kindergarten`; 1,930 line rows duplicate same-ID area geometries. Deduplicate source identity before ranking. No enrollment, catchment, operation, or quality fields are present. |
| Clinic-tagged medical POI | 2,303 → 1,848 | Category `clinic` is emitted only for `amenity=clinic` or `healthcare=clinic`, but 43 distinct objects also carry another medical subtype; 3 have `healthcare=hospital`, one has `amenity=hospital`, and several carry doctors/rehabilitation/etc. Keep explicit subtype fields; do not treat every record as an ordinary outpatient clinic. |
| Ambulance station | 115 → 69 | Separate `emergency=ambulance_station` category. These are emergency facilities, not routine clinics; only 9 distinct IDs are point geometries, and no entrance point is recorded. |

The exported taxonomy has no separate hospital category. Four clinic-category identities carry a hospital tag; those are mixed/ambiguous and should not be promoted to the hospital or outpatient layer without subtype review. A clean hospital layer requires extending the local PBF extraction with direct hospital tags and preserving that raw classification separately. The POI builder and taxonomy are in `sources/social/build_citywide_pois.py` and `sources/social/taxonomy.py`.

Geometry handling: preserve source points and areas. If the map needs one marker for a polygon, use a clearly labeled representative point on the source footprint; this is an approximate map location, not a public entrance. For walking time, use an explicit access point when present or a route endpoint with recorded approximation and snap limit. Do not convert a centroid or straight-line distance into a travel-time claim. The existing offline Valhalla graph was built from this pinned OSM snapshot; its smoke routes do not establish complete access/route coverage.

## OMS status: verified seed and limits

The official 2026 Moscow territorial-program appendix 3 includes medical-organization registry codes and an explicit column for OMS-program participation. It marks GP8 (`771949`), GP191 (`771840`), GP67 (`772085`), GP46 (`772157`) and GP219 (`772688`) as participants. Official facility pages provide five exact site/address assertions: GP8 branch 2; GP191 branch 2; GP67 branches 1 and 5; GP46 branch 1; and GP219's head facility. These resolve to six physical OSM clinic ways across five organizations, using a unique clinic-number/name and exact street/house key; branch numbers are checked where OSM states them, and GP46's branch is established by the official branch page at its exact address. The GP67 branch-5 source address `25 с. 20` is matched to the same complete OSM house key `25 с20`, retaining the structure suffix. The evidence file records the specific official page and snapshot date for every site.

The POI export contains each matched way as line and polygon; the importer emits it once and prefers the polygon. All six marker locations use `point_on_surface` with quality `approximate_footprint`, not entrance coordinates. The original GP8 marker is `[37.491394897929, 55.6622822]`; exact coordinates and OSM identities for all rows are in the sidecar.

The normalized verified seed and provenance are `data/social/oms_verified_clinics_2026.json` and `data/social/oms_verified_clinics_2026.manifest.json`; they contain six rows joined to pinned OSM IDs/versions. The latest output SHA-256 is `b9d7c01b697af7b58f197a95c8f850f846806b26a4514439a385ffb5c35d4c6c` (`records_total=6`). Rebuild with:

```bash
rtk .runtime/geodata-env/bin/python -m py_compile sources/social/oms_registry.py
rtk .runtime/geodata-env/bin/python sources/social/oms_registry.py
```

The badge should say **«Участвует в программе ОМС»** or **«Приём по ОМС»** with a short scope note: the organization participates in the current program, but this does not promise that every service is covered or that a patient can attend without applicable referral/attachment rules. The MGFOMS directory separately lists current-year participating organizations and the services covered under the Moscow OMS territorial program. Do not infer OMS status from `amenity=clinic`, a `ДЗМ` name, or government ownership.

This is a six-site verified seed, not citywide OMS coverage. Each site is a top-five neighbor only where its measured distance ranks there. Other unverified facilities remain `OMS status unknown`; do not inject a far-away verified point into every nearest-five list.

## Update and rights path

For physical features, reuse the Geofabrik Central Federal District PBF download path, then run the already-pinned local city clip and POI builder. Geofabrik documents a daily extract process; retain each downloaded PBF hash, OSM base timestamp, city-boundary hash, per-feature edit timestamp, ODbL notice and attribution. Refreshing the PBF updates mapped features; it does not prove completeness or current operation. [Geofabrik extract](https://download.geofabrik.de/russia/central-fed-district.html), [daily extract process](https://download.geofabrik.de/technical.html), [OSM licence/attribution](https://www.openstreetmap.org/copyright).

For OMS membership, MGFOMS exposes a current-year organization directory and a registry page that says registry files are regularly updated; the 2026 territorial program is the stable dated authority for this year's membership. The accessible web view does not expose a stable bulk-file URL/API schema here, and no bulk reuse license was verified. Keep the importer input curated/versioned and fail closed; enable unattended refresh only after obtaining a stable official artifact URL and confirming its reuse terms. [MGFOMS directory](https://www.mgfoms.ru/spravochniki/med-org/), [registry page](https://www.mgfoms.ru/fond/reestri/), [official 2026 program](https://dc2.mos.ru/normativno_pravovaya_dokumentaciya/territorialnaya_programma_26), [GP8 branch addresses](https://gp8.mos.ru/contacts).

The existing social-source review found Moscow Open Data developer documentation inaccessible and the guessed API/data endpoints unauthenticated or unverified; no official machine-readable metro or school/childcare dataset is currently validated locally. Do not treat candidate portal dataset IDs as usable until schema, freshness, geometry and reuse terms are tested. See `docs/sources/social.md` for the earlier access audit.
