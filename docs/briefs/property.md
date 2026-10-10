# Property data brief for the contracts pass

The product is a Moscow new-build apartment finder with strict filters, side-by-side comparison, recommendations, and a CesiumJS web map plus native Unity Cesium apps for Android/iOS. Data contracts must separate project/building facts from dated asking offers; none of the available sources alone proves a unit is still available to reserve or buy.

## Source capabilities and rights boundary

| Source | What it can support | Contract treatment |
| --- | --- | --- |
| EISZhS / наш.дом.рф | Developer, project/building, declarations, permits, construction and legal/status facts. Integration docs describe REST access and require an onboarding request. | `data_mode=registry_status`; record project/building identifiers and source dates. This is not a live apartment inventory, offer, price, reservation, or booking feed. Never fabricate a unit or price from a registry record. |
| Developer YRL/XML feed | One `<offer>` per advertised object; `offer@internal-id`, project/address, unit fields, asking price/currency, area, rooms, floor, handover, and optional discounts. Yandex's format requires UTF-8 and documents `<realty-feed>/<offer>`; a permitted developer feed can be parsed locally. | `data_mode=developer_offer`; `offer_id` is the feed's `internal-id`. Keep a separate `unit_id` only when the source explicitly supplies a stable physical-unit key; do not equate it to `offer_id`. Preserve raw XML fields. Treat as dated asking offer, not a booking/reservation or guaranteed current stock. |
| bnMAP `GET Прайсы` | Authenticated JSON price-list rows with project/building, section, floor, rooms, area, price, price/m², collection date, source URL, and lot hash. Documentation requires a personal `pbi` token. | `data_mode=asking_price`; retain `a_lot_hash` as source lot ID and `create_time` as source collection time. Price-list history is not a transaction, reservation, or proof of current availability. No call until access and usage rights are granted; never put a token in code or logs. |
| Profitbase | Commercial export/API product for developer inventory and portal feeds. | Rights and allowed use depend on customer plan/contract and credentials. Keep gated until licensed access exists; do not scrape or infer authorization. |
| Yandex `newbuildings.tsv` | Public project/building reference labels and identifiers for reconciliation (name, region, IDs, address/stage/delivery, URL/building label in the sampled rows); no apartment prices or bookable stock. | `data_mode=taxonomy_reference`; use only for matching/labels. We sampled the public endpoint, but found no explicit rehosting license on the endpoint/docs. Public download does not grant redistribution rights: do not bundle or rehost the raw TSV pending permission. A raw sample, if retained for audit, stays local and access-controlled. |

## Intermediate contract recommendation

Keep these concepts distinct for downstream mapping: wire `data_mode` is `demo|real`; source classification (`registry_status`, `developer_offer`, `asking_price`, `taxonomy_reference`) belongs under `source`. Use `record_type` (`project`, `building`, `offer`, `reference_label`), `source.source_id`, `source.source_record_id`, `source.source_url`, explicit `source_rights` (access basis, permitted storage/display/redistribution, evidence/contract reference), `observed_at` (our ingest time), and `source_updated_at` (publisher timestamp, if supplied). Keep `offer_id` separate from nullable `unit_id`. Place normalized values beside a lossless `source_fields` object; preserve source names, identifiers, raw values and units. A price must carry amount, currency, basis (`total` or `per_sqm`), and source time. Availability/booking needs an explicit source status and its own timestamp; absence of a status means unknown, never available.

## Evidence captured

On 2026-10-09, a byte-range request to `https://realty.yandex.ru/newbuildings.tsv` returned HTTP 206 (`text/plain; charset=utf-8`, `Content-Range: bytes 0-4095/22259466`, `Accept-Ranges: bytes`) and a 4 KiB sample. The endpoint has no header row in the sample. Only retrieval metadata is recorded in the repository; raw rows are not. See [`data/property/yandex_tsv_sample.json`](../../data/property/yandex_tsv_sample.json) and [`docs/sources/property.md`](../sources/property.md).

## Primary references

- [bnMAP API: GET prices](https://docs.bnmap.pro/api-docs/api-methods/api-get-prices)
- [Yandex: new-build XML feed requirements](https://yandex.ru/support/realty-partner/ru/requirements-sale-new)
- [Yandex: public new-build TSV](https://realty.yandex.ru/newbuildings.tsv)
- [наш.дом.рф: integration](https://наш.дом.рф/интеграция)

## Criteria and adapter contract (2026-10-09)

The full machine-readable coverage matrix is [`docs/criteria/property.json`](../criteria/property.json). It lists each requested price, apartment, building, amenity, security, utility, and construction criterion with stable key, type, unit, scope, documented source field, automation feasibility, evidence, unknown policy, and refresh rule. It also carries concise Russian UI labels. The current official YRL format supports only a subset: total/conditional price, area, floor, rooms, renovation, balcony, bathrooms, window view, handover, ceiling height, lift presence, guarded territory, and guarded parking presence. Structured payment methods, lift counts/freight lift, CCTV, staffed security/concierge, playground, internet providers, stove type, apartments per floor, and several requested amenities are not documented in the inspected sources and remain unknown unless another authorized feed supplies them.

Adapters emit field-level `record.facts` using `{value, unit, status, scope, observed_at, provenance}`. Missing fields are `unknown` with a null value; explicit source false remains false. Each provenance identifies source/record/field and rights status. `offer.price_per_sqm_rub` is an exact ratio derived only from an eligible total RUB asking price and positive area; it is never rounded for filtering and is not substituted from a vendor-declared per-m² value. `offer_id` identifies an offer while `unit_id` remains separate and nullable. Top-level `data_mode` is only `demo|real`; source classification remains `source.record_mode`.

Runnable local adapters are in `sources/property/adapters.py`. They parse an authorized local YRL/XML feed or local bnMAP JSON response; neither performs network access or handles credentials. Real inputs require explicit rights context and capture `--observed-at`. bnMAP access requires an entitled personal token, and YRL documentation does not authorize access to a developer's feed. The importer validates/preserves facts and rejects demo/uncleared production records. Tests and fixtures are synthetic only.

The reviewed primary sources do not provide two sources of current unit-level asking offers with documented public rehosting rights. EISZhS has a professional/commercial integration path whose exact schema and use rights require authorized API documentation/agreement. We captured and normalized Minstroy's two legally reusable 2026-05-29 XLS releases: 5,735 construction-permit rows and 3,044 commissioning-permit rows. A conservative title-prefix scan yields 18 residential construction events and 9 residential commissioning events; all 27 explicitly name Sochi, and none establish a Moscow residential project. A broader text scan found two Moscow mentions tied to gas-pipeline relocation for a future South Butovo residential microdistrict; those records are not apartment buildings and are excluded from the residential registry layer. The registry layer is historical permit evidence, not unique complexes, live construction status, or offers. It has no structured address, construction start/completion dates, floors, area, or price. Raw XLS, SHA-256, reuse terms, HTTP metadata, field audit, and normalized JSONL/CSV are in [`data/property/minstroy/`](../../data/property/minstroy/README.md). Yandex's public TSV is reference labels only; no rehosting right was established. No licensed live offer feed was obtained and no supplier was contacted. No sample or test offer is evidence of a real sale or live inventory.

## Additional real registry coverage

The local OSM PBF snapshot contains 756 Moscow residential-construction
footprint candidates with explicit tags (`building=construction` plus
`construction=apartments|residential`: 741 + 15). The output preserves each OSM
way/relation as a separate mapped object with exact tags, source version/edit
time, WKB geometry clipped to the pinned Moscow boundary, and only source-given
names/addresses. OSM terms explicitly permit reuse under ODbL-1.0 with
attribution and share-alike; the local manifest records both. This is useful
real spatial registry evidence, but OSM construction tags can be stale and do
not identify apartment projects, units, or prices. It is not joined to the
Minstroy event layer or offer catalogue. See
[`data/property/osm_construction/`](../../data/property/osm_construction/README.md)
and the runnable adapter at
[`sources/property/osm_construction_registry.py`](../../sources/property/osm_construction_registry.py).

Two narrow Moscow sources were checked: the official urban-planning service
shows an item-specific request form rather than a bulk download, while public
mos.ru weekly-permit PDFs contain useful fields but no explicit reuse license
was found on the examined material, so neither was loaded. The maximum
rights-reviewed coverage currently available is therefore the
Minstroy historical registry events plus OSM mapped construction footprints;
neither provides current Moscow apartment asking prices or available units.
At least one licensed live offer feed and an authorized integration path remain
absent.
