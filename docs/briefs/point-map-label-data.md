# Point-map OSM labels and addresses

Read-only source audit, 2026-10-10. Scope: the 756 records in `data/property/osm_construction/osm_moscow_residential_construction_2026-10-08.jsonl` and the 10 geocoded rows in `data/property/estateline/projects.jsonl`; the active projection is `models-v1-92dc659b5e75fa52`. No source files or code changed.

## What exists in the 756 OSM records

The audit JSONL keeps `source_record_id` in exact typed/versioned form (`way/<osm_id>/v<version>` or `relation/<osm_id>/v<version>`), plus `osm_type`, `osm_id`, `osm_version`, `name`, JSON-string `address_json`, JSON-string `raw_tags`, `source_updated_at`, `geometry_type`, bbox, and geometry SHA. The matching GeoParquet stores the same exact identity/tags and source polygon WKB. Active-run `source_evaluations.jsonl` maps each `candidate_id` back to this `source_record_id`; joins should use typed ID and version, never name or proximity.

| Raw source field | Rows | Interpretation |
| --- | ---: | --- |
| `building` / `construction` | 756 / 741 apartments + 15 residential | All 756 are mapped as construction; this is an OSM source status, not independent confirmation that each is still being built |
| `name` | 33/756 (6 contain “ЖК”) | Exact OSM object name; not automatically an EstateLine/catalog link |
| `addr:place` | 38/756; 33 rows explicitly contain “ЖК” | Address-place label; two exact ЖК values: `ЖК Шагал` (31 rows) and `ЖК «Саларьево Парк»` (2 rows) |
| Any `addr:*` | 282/756 | Address metadata is partial or absent for the rest |
| `addr:street` + `addr:housenumber` | 153/756 | Safe to format from the two raw values as a street/house address; no pre-composed cleaned address field exists |
| `addr:street` / `addr:housenumber` / `addr:city` | 165 / 163 / 145 | Values remain source tags; 12 rows have street without number, 10 have number without street |
| `official_name` / literal `residential` | 0 / 0 | Residential use is instead `construction=apartments` (741) or `construction=residential` (15) |
| `operator` / `brand` / candidate `site` / `website` | 4 / 0 / 0 / 3 | Operator is not a project label; no candidate has a brand or direct site tag |

The current active model assessment copies only the OSM `name` tag (33 rows); it sets `address=null` for all 756 and does not carry `addr:place` or parent-relation labels. Thus 723 assessment names are null and raw addresses/complex labels are lost in that projection, although the exact-ID source join remains available.

## Exact source relation check

A bounded scan of the pinned city PBF checked relation membership only for the exact 756 candidate IDs. It found 21 candidate-member occurrences across 19 parent relations. One parent is `relation/8916920/v2`, tagged `type=site, site=building`, and contains `relation/8916919/v3`; it has no project name. One direct named building relation is `relation/19782953/v2`, tagged `type=building`, `name=ЖК "Зиларт-Спарк" (дом 21)`, and contains candidate `way/1450746348/v3`. That is an explicit OSM relation/member label for this source building, not an EstateLine link. Other membership hits are building, associated-street, or multipolygon relations. Parent relations contain no `operator` or `brand` fields.

Safe source examples:

- `relation/17439362/v1`: `name=ЖК Ahead`, `construction=residential`; the object has no street/house tags.
- `relation/19271041/v2`: `addr:place=ЖК «Саларьево Парк»`, `addr:housenumber=к20`; no street tag.
- `relation/14555599/v3`: `addr:street=улица Довженко`, `addr:housenumber=4`, `addr:city=Москва`; no explicit ЖК label.
- `way/1450746348/v3`: `addr:street=Автозаводская улица`, `addr:housenumber=23/5`; exact member of the named `relation/19782953/v2` above.
- EstateLine `estateline:10653` retains its own name, address (`ул. Остоженка, д. 9/14`), page ID, and verified point; this user-supplied row is internal-use only.

The 10 geocoded EstateLine rows all retain `registry_id=estateline:<page_id>`, `source_url_page_id`, `name`, and `address` in the project JSONL. Their page IDs are `10653`, `1431303`, `1440752`, `1468734`, `1476352`, `1518871`, `1562351`, `2874610`, `41658`, and `49061`; all use `location_quality=verified` from a strict exact OSM address tuple. For example, `estateline:49061` retains its EstateLine name/address/page identity, but the matching OSM object remains an unverified cross-source project link. EstateLine rights remain `user_supplied_internal_use`.

There is one exact cross-source coordinate and typed-object overlap: EstateLine `estateline:49061` and `way/1502857414/v1`. The active run stores three link hypotheses, all `review` with `project_identity_verified=false`. Do not transfer that EstateLine name onto the OSM object or collapse their project identities. OSM `name`, `addr:place`, and the exact parent relation above may be preserved as explicitly sourced OSM labels; they do not prove a link to the EstateLine catalog. The OSM layer requires ODbL-1.0 attribution and its applicable share-alike treatment.
