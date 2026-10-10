# Property source handling

This source inventory is scoped to Moscow new-build discovery. Keep registry facts, advertised price, and offer availability as separate evidence. Every normalized record preserves publisher fields and emits field-level facts with status, scope, observation time, and provenance. Real-mode adapters require an explicit capture `observed_at`; publisher update/collection fields remain distinct under `source_updated_at` or raw source fields.

The machine-readable coverage, evidence, unknown policy, and refresh matrix is [`docs/criteria/property.json`](../criteria/property.json). Missing source fields are `unknown`; explicit false is retained as false only when the source documents and supplies a false value.

## Sources and access

| Source | Classification | Access and handling |
| --- | --- | --- |
| EISZhS (`наш.дом.рф`) | Registry/declaration facts, not a live inventory feed. | DOM.RF documents a commercial REST API for professional market participants and says structured declaration data may be provided to legal entities. The documented path is `https://наш.дом.рф/api-еисжс` / `https://наш.дом.рф/интеграция`, contact `nash@domrf.ru`. We have not applied. Exact schema, public display, storage, and redistribution rights require the API documentation and agreement. Its public 2023 API note confirms premises/building attributes and energy-efficiency information broadly; it does not validate an exact energy-class field. |
| Developer YRL/XML | Per-object advertised new-build offers, when supplied by an authorized publisher. | Accept only a feed the operator is permitted to process. The adapter reads local files only. `offer@internal-id` is the offer ID; `unit_id` is emitted only for explicit `unit-id`, `apartment-id`, or `flat-id` tags. YRL documents price/currency/unit/discount, area, rooms, floor, renovation, balcony, bathroom type/count, view, ceiling height, lift presence, closed territory, guarded-parking presence, and completion year/quarter. It does not provide structured buyer payment methods, lift counts, freight-lift counts, CCTV, concierge, or provider fields. A price is asking evidence and does not prove availability or booking. |
| bnMAP GET Prices JSON | Authenticated price-list observations. Official docs require a personal `pbi` token. | `normalize_bnmap_prices` reads only a local response and requires explicit source rights and a capture timestamp; it never requests a token or calls the API. `price` is price-list asking evidence, `squarePrice` remains raw, `numInPlatform` is not apartments-per-floor, `start_sales` is not construction start, and `agreement` is not a buyer financing method. No entitlement/token is present. |
| Profitbase | Commercial inventory/export/API service. | Rights depend on the paid plan and customer agreement. Keep gated until the project has approved access and documented display/storage/redistribution rights. |
| Yandex `newbuildings.tsv` | Public project/building reference labels and IDs. | Use as labels only, not prices, unit listings, or stock. Public endpoint availability does not establish redistribution rights. The repository retains retrieval metadata only; no raw sample is checked in. |
| Minstroy open-data registers | Official open permit/commissioning-register data. | Minstroy's official reuse terms allow copying, publishing, redistribution, modification, and commercial use when lawful, not distorted, and attributed. The 2026-05-29 construction-permit XLS has six columns: row number, EGRUL number, object name, developer, permit issue date, permit number. The commissioning-permit XLS has six: row number, object name, applicant, permit issue date, permit number, EGRUL number. The captured residential-name subset has 18 construction events and 9 commissioning events; all explicitly mention Sochi, none Moscow. Two broader matches containing both Moscow and housing-related words are gas-pipeline relocation events whose narrative refers to a future residential microdistrict; both are excluded and preserved in an audit-only file. This is a historical registry-event layer, not an offer or live construction catalogue. Neither workbook has a structured address, construction start/completion date, floor count, area, or apartment price. Original files, checksums, reuse evidence, and normalized JSONL/CSV are under [`data/property/minstroy/`](../../data/property/minstroy/README.md). |
| Minstroy price benchmarks | Historic aggregate market/model-housing averages per m². | Official open-data reuse terms apply with attribution. Catalog entries are dated 2018 and 2020; do not interpret these aggregate benchmarks as current apartment asking prices or synthesize `Offer` rows from them. |
| Moscow Analytical Center market summary | Aggregate price trend chart, attributed on-page to CIAN. | Its official use terms require prior agreement for site materials. No permission or unit-level export is available here; do not rehost these materials or treat the chart as an offer feed. |

The source review found **zero** sources with both current unit-level asking-price rows and explicit public rehosting rights. Minstroy provides a real, legally reusable registry-event corpus, but it has no Moscow residential hits in the captured version and does not fill the offer-price gap. See `data_access_findings` in the criteria matrix for concrete EISZhS, bnMAP, and developer-feed access paths. No supplier was contacted.

The current retrieval evidence is recorded in [`data/property/yandex_tsv_sample.json`](../../data/property/yandex_tsv_sample.json). The first 4 KiB of the endpoint returned HTTP 206 on 2026-10-09, with `Content-Range: bytes 0-4095/22259466`; 13 complete rows from that partial sample normalized as `reference_label` records with no price. The sample had no header row; positional names in the evidence record are an inference from rows. The normalizer preserves unrecognized columns so that this inference cannot silently discard source data.

## Intermediate record emitted by the adapter

The pure-Python adapter emits one JSON object per line. Developer YRL offers follow the contract's wire-level `data_mode` (`demo|real`) and `Offer` fields. The source classification and evidence stay under `source`:

```json
{
  "schema_version": "property-intermediate/0.1",
  "record_type": "offer",
  "data_mode": "demo",
  "offer_id": "feed internal-id",
  "unit_id": null,
  "complex_id": "source newbuilding-id",
  "district_id": null,
  "price": null,
  "availability": null,
  "source": {
    "name": "Synthetic developer YRL fixture",
    "quality": "illustrative",
    "record_mode": "demo",
    "observed_at": "2026-10-09T09:30:00+00:00",
    "source_updated_at": null,
    "rights_status": "demo",
    "source_id": "developer_yrl",
    "source_record_id": "feed internal-id",
    "url": null,
    "source_fields": {},
    "source_rights": {
      "access_basis": "synthetic_fixture",
      "local_storage": "test_only",
      "public_display": "not_applicable",
      "redistribution": "not_applicable"
    }
  },
  "normalized": {
    "project_name": null,
    "address": null,
    "rooms": null,
    "floor": null,
    "area_sqm": null,
    "price": null
  },
  "source_fields": {}
}
```

For a real developer feed, use `data_mode=real`, `source.record_mode=developer_offer`, and keep `source.rights_status=unknown` until the contract explicitly clears display. `source.quality=dated_asking` is emitted only when a numeric RUB total asking price is present; `Offer.price` remains null for per-square-metre amounts, non-RUB values, and non-integer RUB totals. Raw source prices and all XML fields remain in `normalized` and `source.source_fields` without conversion. `availability` is always null because a price feed does not prove a unit is available or booked.

Each offer also has `facts`, keyed by the criteria matrix. A fact carries `value`, `unit`, `status`, `scope`, `observed_at`, and `provenance` (source ID, record ID, field path, source timestamp, and rights status). Missing fields stay `unknown` with null values. Synthetic fixture facts are `illustrative`; they are rejected by the real importer. `building.lift_present=false` is separate from `building.elevator_count`, which remains unknown when the source gives only presence. `building.parking_type` is also separate from YRL's `parking` (presence of guarded parking).

The canonical `offer.price_per_sqm_rub` fact is an exact ratio object `{numerator_rub, denominator_sqm}`. It is derived only from total RUB asking price and positive total area, with both inputs and the formula in provenance. YRL's area-priced `price` and bnMAP's advertised `squarePrice` remain raw source evidence and are not substituted.

The TSV normalizer emits a separate record with top-level `data_mode=demo|real` and `record_type=reference_label`; `source.record_mode=reference_label` and `source.quality=illustrative` for synthetic fixtures or `unknown` for the actual public labels. It emits no `offer_id`, `unit_id`, price, or availability claim. Source TSV columns remain under `source.source_fields`. A registry feed adapter is intentionally out of scope until an authorized EISZhS export/API sample and its terms are supplied.

## Run locally

From the repository root, parse the clearly synthetic fixture as a JSONL demo record:

```bash
rtk python -m sources.property.adapters developer-xml \
  sources/property/tests/fixtures/developer_feed.synthetic.xml \
  --source-id synthetic_demo \
  --data-mode demo \
  --rights-json sources/property/tests/fixtures/rights.synthetic.json
rtk python -m sources.property.adapters bnmap-prices \
  sources/property/tests/fixtures/bnmap_prices.synthetic.json \
  --source-id synthetic_bnmap_demo \
  --data-mode demo \
  --rights-json sources/property/tests/fixtures/rights.synthetic.json
rtk python -m unittest discover -s sources/property/tests -v
```

The real-feed/API-response path requires a local JSON file containing the operator's explicit rights context and a capture `--observed-at` timestamp. It does not take API credentials and does not fetch remote URLs. The TSV subcommand also reads a local file only; keep display and redistribution uncleared until written permission is documented.

The tests use synthetic values only and can be run with:

```bash
rtk python -m unittest discover -s sources/property/tests -v
```

## Rights-gated PostGIS import

`sources.property.importer` accepts canonical JSONL from the local developer
or bnMAP adapter. It validates top-level `data_mode=real`, a non-demo offer
`record_mode`, ISO timestamps, field-fact status/provenance, price/quality
consistency, unique source record IDs, and preserved `source.source_rights`.
It persists facts in offer display JSON and raw fields in `source_fields`. It requires a separate rights
manifest with `rights_status=cleared`, explicit `permitted_storage=true`, and
`permitted_display=true`; demo feeds, unknown rights, and reference labels
cannot be committed. Per-square-metre inputs remain raw source evidence with
no canonical price and cannot pass price-based search. The target complex must already
exist in `complexes` with cleared rights. Each batch is one transaction, writes
an immutable content-digested `catalogue_snapshots` row, updates the
`source_manifests` audit row, and upserts offers while rejecting cross-source
ID collisions. Raw source keys remain in `source_fields` and the display
record's `source.source_fields`.

The importer accepts a libpq service name only. It defaults to a dry run and
does not connect to PostGIS unless `--commit` is supplied. For a permitted
feed, first create adapter JSONL and a separate importer manifest outside the
repository; do not put credentials in either file:

```bash
rtk python -m sources.property.adapters developer-xml /local/authorized/feed.xml \
  --source-id developer_feed_01 --data-mode real \
  --rights-json /local/adapter-rights.json \
  --observed-at 2026-10-09T12:00:00+03:00 > .runtime/authorized-offers.jsonl
rtk python -m sources.property.importer \
  --input .runtime/authorized-offers.jsonl \
  --rights-json /local/import-rights.json --service novostroy
rtk python -m sources.property.importer \
  --input .runtime/authorized-offers.jsonl \
  --rights-json /local/import-rights.json --service novostroy --commit
```

The importer rights manifest is distinct from the adapter's string-valued
rights context. It includes `source_id`, `rights_status: "cleared"`, a
documented `access_basis`, and boolean `permitted_storage` and
`permitted_display`; the offer's source ID and embedded clearance must match.
Review the dry-run report before explicitly committing. Do not use a test-only
manifest or service for a real import.

The integration acceptance runner uses a separate `property_test_*` schema and
libpq service, and accepts synthetic fixtures only with `--test-only` gates.
The fixture's fake price is visibly labeled and never inserted into the main
`novostroy` schema or published by the regular API configuration. Run it with
the project-local API/PostGIS test environment:

```bash
rtk proxy env PGSERVICEFILE="$PWD/.runtime/pg_service.conf" \
  .runtime/property-test-env/bin/python -m sources.property.devdb.run_integration
```

The runner exercises adapter → transactional import → PostGIS reader → FastAPI
search, including stale, uncleared, unknown-price, demo, and unknown-district
exclusion. It retains the isolated schema under ignored `.runtime/` for local
inspection; it does not seed the `novostroy` service.

## OSM mapped construction registry

`data/property/osm_construction/` contains a separate real-data registry layer
extracted from the pinned Geofabrik Central Federal District OSM snapshot
(`2026-10-08T20:21:06Z`). The adapter accepts only objects with the exact pair
`building=construction` and `construction=apartments|residential`, and clips
their mapped polygon geometry to pinned Moscow relation 102269. The snapshot
contains 756 candidate OSM objects inside the city polygon (741 tagged
`apartments`, 15 tagged `residential`), out of 1,755 mapped `building=construction`
areas. Generic, untyped, and non-residential construction is excluded. Ways
and relations are retained as distinct OSM objects; these rows are not
deduplicated into projects or complexes.

GeoParquet stores WKB geometry, OSM type/id/version, object edit time, capture
time, the exact selection rule, optional `name` and `addr:*` tags only where
present, and lossless JSON tags. The JSONL audit records geometry hashes and
bounding boxes. The artifact is `data_mode=real`, `record_mode=registry_status`,
and `record_type=registry_event`, with no offer, unit, asking price, or
availability. An OSM `building=construction` tag may be stale and is not an
official permit or proof of current construction. Do not join this layer to
offer/complex records without independent identity evidence.

OpenStreetMap data is licensed under ODbL-1.0. The repository output records
attribution (`© OpenStreetMap contributors`) and the share-alike obligation;
redistribution must preserve these obligations. See the [OSM copyright and
license page](https://www.openstreetmap.org/copyright). Input and output hashes,
counts, selected fields, and limitations are in
[`source_manifest.json`](../../data/property/osm_construction/source_manifest.json).
The offline extractor and tests are `sources/property/osm_construction_registry.py`
and `sources/property/tests/test_osm_construction_registry.py`; optional
dependencies are listed in `sources/property/requirements-osm.txt`.

We also checked Moscow's official urban-planning information service at
`ogd.mos.ru`, which presents an item-specific request form rather than a bulk
download. Mos.ru publishes weekly construction-permit PDFs with
address/object/developer/permit fields; the reviewed 2026 PDF is informational,
and we found no explicit open-data reuse terms for it. We did not ingest those
PDFs. Neither source provides a rights-cleared apartment asking-price feed.

## 3D buildings and height references

Reviewed 2026-10-09. No vendor or government 3D building model was downloaded.
One Microsoft GlobalML partition was downloaded separately for a bounded local
coverage audit; it is not a 3D model. The only Moscow network capture here was a
public 18,197-byte WMS `GetCapabilities` XML from Moscow Transport's GeoWebCache;
it advertised 25 entries, including sample and transport layers, and no named
building, height, or 3D layer. The cache page itself says it may not provide full
WMS service. This does not establish that Moscow has no internal 3D data; it
means this public endpoint is not an evidenced building-model feed. No reuse
terms for that cache were found, so its render access is not treated as
permission to store the output.

| Source | Documented access and format | Coverage / value | Rights and ingestion decision |
| --- | --- | --- | --- |
| Yandex Maps | JS API, MapKit, Static API and Tiles API; Tiles API returns a requested map tile image (not building geometry). The current API terms require an API key. | Maps may render 3D-looking buildings, but the reviewed public docs expose no height/section geometry export or building-height API. | Terms prohibit saving, processing, or modifying API data except temporary in-service cache of up to 30 days. Do not extract browser tiles, network payloads, or model assets into our catalog. A different written license would be needed. |
| 2GIS MapGL | Official styles include 3D residential, administrative, industrial, under-construction and detached-building layers; styles can render raised polygons / 3D models. API access requires a key. Direct MVT export is documented for the private On-Premise platform. | Renderer can display vendor building geometry; docs do not define a public self-service dump of the underlying Moscow building geometries/heights. | API rules prohibit storing, processing, modifying, distributing, or publishing service data. Data.2gis has no open/free license; MVT/On-Premise would need a separate paid contract spelling out retention and reuse rights. No key or license is available. |
| Moscow AГР model submissions | Official Moscow instructions require project applicants to submit 3D models; current documents describe FBX/PNG/JSON or FBX/PNG/GeoJSON packages, including buildings/corps, local Moscow coordinates and absolute heights. Models are submitted through a government service/upload workflow. | Potential project-specific design models, possibly more useful than a height raster for a section shape. Not evidence of a public citywide download or of as-built measurements. | Submission instructions are not a data-release license. No public bulk endpoint, public sample archive, or reuse permission was found. Treat as restricted/unknown; no login, request, or scrape was attempted. |
| Microsoft GlobalML Building Footprints | Public `dataset-links.csv` points to country/L9 gzipped GeoJSONL partitions (`.csv.gz`); footprint polygons carry an estimated scalar `height` in metres or `-1` when no height estimate exists. | The 2026-08-13 links index has 2,308 Russia partitions; six L9 partitions intersect Moscow (20.5–64.0 MB each). We sampled only `120310101`, selected because it contains the Kremlin point and has the largest city-boundary overlap. Its 67,070,338-byte compressed file has 645,532 features, of which 324,812 intersect the Moscow boundary; every feature has `height=-1` and `confidence=-1`, so this partition contributes zero usable height estimates. Positive-area geometry overlap finds 7,895 GlobalML rows against 10,573 Overture BuildingPart records (1,290 candidate pairs have IoU ≥0.5); these are spatial candidates, not identity matches. | Microsoft states CDLA Permissive 2.0 for data; sharing data requires including the license text. The audit partition is retained under ignored `.runtime/` only, not checked in or publicly rehosted. |
| TUM GlobalBuildingAtlas (GBA.LoD1) | Public Hugging Face JSON tiles with LoD1 height/variance data, indexed in `representative/lod1.geojson`; the Moscow tile is `europe/e035_n60_e040_n55`, a 5° × 5° extent. A HEAD request followed the public redirect to HTTP 200 and confirmed a 745,598,834-byte JSON file. This is not a standalone footprint GeoJSON: the card says JSON links to building polygons in `GBA.Polygon`; `produce_lod1.py` joins JSON heights/variance with separate Polygon and ODbLPolygon GeoJSON files to generate LoD1 GeoJSON. The small index also identifies the same-extent `GBA.Height` ZIP tile; the full height tile was not fetched. | The area covers Moscow, but the city-specific rows were not downloaded or inspected: one LoD1 JSON tile is ~746 MB. TUM derives a 3 m height map from PlanetScope imagery (mostly 2019, some 2018); each LoD1 building gets the maximum modeled height within its fused footprint, with local prediction variance as uncertainty. The paper reports 4.1 m height RMSE for Europe and 5.5 m globally, but no Moscow-specific error. These are simplified, single-height whole-building models, not section/building-part geometry. | The GBA.LoD1 data card explicitly declares CC BY-NC 4.0; attribution and indication of changes are required and commercial use is excluded absent separate permission. This may fit a genuinely noncommercial educational prototype, subject to its context and license terms, but is not cleared for the commercial product. The separately distributed OSM/Microsoft polygon component has ODbL terms. Article and code licenses are distinct from data rights. No tile was downloaded or rehosted. |
| Microsoft TEMPO / Building Density & Height | Direct Cloud-Optimized GeoTIFFs referenced by a public GeoPackage tile index; 2020 Q4 and 2023 Q4 global layers, two bands: building-density fraction and normalized height × 100 m. | Global layer includes Moscow at about 100 m/pixel; the finer ~40 m quarterly series only names Bamako, Guangdong, Guatemala, Lusaka, and Nakuru. This is an urban-context grid, not a building or section feature. | Dataset and tile index are CDLA Permissive 2.0 with attribution. Suitable for contextual city-scale shading only, not tower/section filters. No index/raster download was made (the current index advertises a 384 MB object). |
| EU JRC GHSL GHS-BUILT-H R2023A | Global GeoTIFF raster of mean gross/net building height, derived from AW3D30, SRTM30 and Sentinel-2 (2018). | Global coverage includes Moscow. Resolution is 100 m Mollweide or 3 arc-seconds WGS84 (about 93 m north-south × 52 m east-west at Moscow latitude). The 2018 grid average is not a per-building height or shape. | GHSL explicitly licenses data CC BY 4.0; retain the dataset/release citation, attribution, and change notice. This is the best low-friction open contextual height reference, not an apartment-complex input. No raster was downloaded. |
| Copernicus DEM GLO-30 | 1° GeoTIFF/COG tiles; the public AWS bucket exposes GLO-30/GLO-90 objects, and a Moscow GLO-30 tile was verified by HTTP HEAD. | The Moscow tile is `Copernicus_DSM_COG_10_N55_00_E037_00_DEM`; its public object is 30,719,198 bytes. It is a DSM: roof surfaces, vegetation, and infrastructure all contribute; it is not a building-height or terrain-only surface. At 30 m a typical building footprint spans too few pixels for reliable section height. | GLO-30/90 have a free license with mandatory source notice. The CDSE 30 m **view service** has required CCM registration since 2026-07-28, but that service restriction does not apply to the separately documented public AWS tile route. The public tile set is documented as limited; no raster pixels were downloaded. Keep this as a lower-priority visualization/terrain-context candidate, not building facts. |

Yandex's official Народная карта help explains the provenance better than the
render/API documentation alone. The building contour and integer-metre height
are user-editable; if exact height is unknown, its rule permits an estimate of
three metres per floor. It recommends drawing different-height parts as separate
building contours, and explicitly gives a stylobate example where the upper
part's height includes the stylobate. Edits are moderated for plausibility;
moderators are advised to inspect satellite images, panoramas, and street-level
photos. The same help warns that geometry can be schematic or adjusted by Yandex
staff. A 2025 company post says roughly 600 detailed models were created by
Yandex cartographic designers and that submitted user/business models are
reviewed. These documents support a mixed user-edited/editorial origin, but do
not give per-building survey provenance or prove that values came from official
plans, LiDAR, or a measured survey. Separate contours are useful visual massing
for differing heights, not stable legal building-section IDs or apartment
geometry.

The one-partition GlobalML audit is stored with HTTP metadata, ETag, Last-Modified
and SHA-256 in ignored `.runtime/microsoft-global-footprints-russia-120310101-2026-08-13.metadata.json`;
the compressed sample is
`.runtime/microsoft-global-footprints-russia-120310101-2026-08-13.csv.gz`.
The partition has 67,070,338 compressed bytes and SHA-256
`3ed3f639bfa7d44e765276fb234c8b65b76685848fa6bff2d36544ba2fc689c6`; its results
are in `.runtime/microsoft-global-footprints-russia-120310101.analysis.json`.
All 645,532 features have `height=-1` and `confidence=-1`; 324,812 intersect
the Moscow boundary and still have no height estimate. These counts are specific
to that one partition; they must not be extrapolated to all six
Moscow-intersecting partitions. The geometry audit found positive-area overlap
for 7,895 GlobalML rows against 10,573 Overture BuildingPart source records; 1,290
pairs have intersection-over-union of at least 0.5. GlobalML has no Overture/OSM
source ID, so these overlaps are spatial candidates, not identity joins.

The TUM GlobalBuildingAtlas has a real, public Moscow-coverage candidate, but it
does not clear the production height-source gate. Its small public tile indexes
place Moscow in `europe/e035_n60_e040_n55`; the LoD1 JSON tile resolves anonymously
to HTTP 200, but is 745,598,834 bytes. The checked Hugging Face revision was
`9da24b3a8dce436a7420d5c3589de718d7ba14d6` (observed 2026-10-09). I did not
download it. The repository documents a WFS endpoint, but a HEAD request to its WFS
`GetCapabilities` URL returned HTTP 403; I did not attempt a feature query. The
347 kB height-tile index lists
`Height/europe/e035_n60_e040_n55.zip` (and SHA-512
`d2fbde4a5f3f5c7a7307b1f65c92698faf7f64dd32a736525622a41910306d09d9093ce0321137c2529f099b5a7e75a314eb6b5de8d8bca5d1aaebea2fa82af0`),
but its raster was not downloaded.

The TUM paper describes height modeling from roughly 3 m PlanetScope surface
reflectance imagery acquired mainly in 2019, supplemented with 2018 imagery,
using LiDAR-derived normalized surface models from 168 training regions. This
is model inference, not a Moscow building survey. For LoD1, the maximum
predicted raster height under each fused building polygon becomes that
building's single height; prediction variance is its uncertainty measure.
Published height RMSE is 4.1 m for Europe and 5.5 m globally, with no Moscow
tile-specific validation. The pipeline fuses footprints from OSM, Microsoft,
Google Open Buildings, CLSM, and model-generated polygons. The LoD1 JSON is an
attribute package linked to the separate polygon layers, not a standalone
ready-to-display geometry file: the dataset card's `produce_lod1.py` workflow
joins height/variance data to GBA.Polygon and ODbLPolygon GeoJSON files. That
differs from the existing OSM/Overture `BuildingPart` data, where mapped part geometry and
typed OSM IDs can be preserved: GBA.LoD1 is useful for approximate whole-building
mass, but does not provide a verified section-level join or surveyed section
shape. I did not infer feature matches from tile coverage metadata.

The GBA.LoD1 repository declares CC BY-NC 4.0. Its terms permit attribution and
adaptation for noncommercial use, but exclude use primarily intended for
commercial advantage or monetary compensation. Therefore this is a possible
separately isolated educational prototype source only when that use is truly
noncommercial and meets attribution/change-notice terms; it is not a source for
the commercial product without separate permission. The paper's CC BY 4.0
publication license and the code repository's MIT-with-Commons-Clause license
do not replace the dataset license. No GBA data were downloaded, stored, or
rehosted.

The practical open-data path is to keep the existing OSM mapped heights and
`building:part` tags as feature-level evidence, then optionally add Microsoft
footprints only after checking the six Moscow partitions and preserving CDLA
terms. GHSL/TEMPO may provide subdued area-level context with explicit
`approximate=true`; neither should populate a building's canonical height. Any
future city/vendor 3D model needs explicit storage/display/redistribution rights
and a stable source record before it can enter the canonical catalog.

## References

- [bnMAP API: GET prices](https://docs.bnmap.pro/api-docs/api-methods/api-get-prices) documents a personal token, JSON response, and price-list row fields.
- [Yandex new-build XML requirements](https://yandex.ru/support/realty-partner/ru/requirements-sale-new) documents `<offer internal-id>`, asking price/currency, areas, unit attributes, and new-build fields.
- [Yandex general feed requirements](https://yandex.ru/support/realty/ru/feed/content-requirements) specifies YRL/XML with `<realty-feed>` and `<offer>`, UTF-8, and valid current listings.
- [Yandex public TSV](https://realty.yandex.ru/newbuildings.tsv) is the sampled reference endpoint.
- [EISZhS integration](https://наш.дом.рф/интеграция) describes the integration onboarding route. Its public information and registry purpose do not imply apartment-level inventory rights.
- [Profitbase export](https://reserve.profitbase.ru/solutions/export) and [tariffs](https://profitbase.ru/tariff) describe commercial feed/export products.
- [OpenStreetMap copyright and license](https://www.openstreetmap.org/copyright) establishes ODbL-1.0, attribution, and share-alike obligations.
- [Moscow urban-planning information service](https://ogd.mos.ru/) exposes an item-specific request form, not a bulk dataset in the reviewed view.
- [Mos.ru construction-permit information PDF for 06–12 February 2026](https://www.mos.ru/upload/documents/files/9950/OformlennieRS2026.pdf) demonstrates address, object, developer, and permit fields, but is not treated as licensed open data here.
- [Yandex Maps API terms](https://yandex.ru/legal/maps_api/ru) and [Tiles API request format](https://yandex.ru/maps-api/docs/tiles-api/request.html) cover keys, image tiles, and the storage restriction.
- [2GIS configured MapGL layers](https://docs.2gis.com/en/maps/styles/layers/configured/mapgl), [MapGL access key setup](https://docs.2gis.com/en/mapgl/start/first-steps), [API rules](https://law.2gis.ru/api-rules), and [Data.2gis licensing/pricing](https://law.2gis.ru/cost-data2gis) cover 3D display and access constraints.
- [Moscow 3D AГР model requirements](https://www.mos.ru/upload/content/files/Trebovaniya3DAGRitog.pdf) and [model upload instructions](https://www.mos.ru/upload/content/files/Instrykciya%2878%29.pdf) describe applicant submissions; neither grants rights to a citywide dataset.
- [Moscow Transport GeoWebCache](https://transport.mos.ru/geoserver/gwc/) documents WMS/WMTS endpoints; our public WMS capabilities response did not advertise building-height layers.
- [Microsoft GlobalMLBuildingFootprints](https://github.com/microsoft/GlobalMLBuildingFootprints) and [CDLA Permissive 2.0](https://cdla.dev/permissive-2-0/) document footprint/height fields, downloads, and reuse terms.
- [Microsoft Building Density & Height dataset](https://github.com/microsoft/buildings) documents global 100 m COGs and CDLA terms.
- [GHSL GHS-BUILT-H](https://human-settlement.emergency.copernicus.eu/ghs_buH2023.php), [GHSL use conditions](https://human-settlement.emergency.copernicus.eu/GHSLhowToCite.php), and the [GHSL Data Package 2023](https://human-settlement.emergency.copernicus.eu/documents/GHSL_Data_Package_2023.pdf) document epoch, resolution, and CC BY 4.0.
- [Copernicus DEM collections](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM), [Copernicus DSM description](https://copernicus-dem-30m.s3.amazonaws.com/readme.html), and [30 m view-service access change](https://dataspace.copernicus.eu/news/2026-7-17-copernicus-dem-30m-view-service-license-acceptance) document surface semantics, resolution, and current access conditions.
- [Yandex Народная карта help: buildings](https://yandex.ru/support/nmaps/ru/cat_bld), [drawing rules](https://yandex.ru/support/nmaps/ru/cat_bld_rul1), [height attribution rules](https://m.yandex.ru/support/nmaps/ru/cat_bld_rul2), [moderation rules](https://yandex.ru/support/nmaps/ru/start_moder), [3D model technical requirements](https://yandex.ru/project/maps/3d/tech), and [2025 user-model announcement](https://yandex.ru/company/news/02-09-2025-01) describe editing, approximate height, split contours, review, and editorial/user-submitted models.
- [GlobalBuildingAtlas 2025 paper](https://essd.copernicus.org/articles/17/6647/2025/) documents imagery, LiDAR-trained height inference, per-building maximum-height assignment, uncertainty, and regional validation.
- [TUM GlobalBuildingAtlas dataset card](https://huggingface.co/datasets/zhu-xlab/GBA.LoD1) declares CC BY-NC 4.0 for LoD1 and explains that the [Moscow JSON tile](https://huggingface.co/datasets/zhu-xlab/GBA.LoD1/blob/main/LoD1/europe/e035_n60_e040_n55.json) must be joined to separate Polygon/ODbLPolygon GeoJSON through [`produce_lod1.py`](https://huggingface.co/datasets/zhu-xlab/GBA.LoD1/blob/main/produce_lod1.py); its [Moscow tile index](https://huggingface.co/datasets/zhu-xlab/GBA.LoD1/blob/main/representative/lod1.geojson) and [height tile index](https://huggingface.co/datasets/zhu-xlab/GBA.LoD1/blob/main/representative/height_zip.geojson) expose coverage metadata. The [mediaTUM dataset record](https://mediatum.ub.tum.de/1782307) and [official repository](https://github.com/zhu-xlab/GlobalBuildingAtlas) document the data portal and WFS route. The [CC BY-NC 4.0 deed](https://creativecommons.org/licenses/by-nc/4.0/) defines the commercial-use restriction.
- [Copernicus public AWS data listing](https://registry.opendata.aws/copernicus-dem/), [Moscow GLO-30 tile](https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N55_00_E037_00_DEM/Copernicus_DSM_COG_10_N55_00_E037_00_DEM.tif), and the fuller [raster-source brief](height-rasters.md) verify the anonymous tile route separately from the CDSE view-service gate.
