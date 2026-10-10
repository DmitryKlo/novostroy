# EstateLine local project registry

`sources/property/estateline.py` builds a local project/building registry from
the user-supplied XLSX corpus at `/home/pirojban/Загрузки/1_xls_estateline`.
It reads Moscow and Moscow-region report books, retains project-level fields,
and uses the linked EstateLine page ID as the canonical registry identity.
Workbook ID remains separate as `source_row_id`; it is not a merge key. The
source workbooks are neither modified nor copied.

## Frozen import

The import parsed 730 of 730 books and 11,693 object blocks. Report periods
span 2022-04-10 through 2026-09-06; `snapshot_at` is 2026-09-06. Selection
first chooses the latest report-period record per page ID across all source
categories and report regions, then applies the Moscow/category filters. This
prevents an older residential record from surviving when a later workbook
reclassifies the same page as an office, another use, or a Moscow-region
object. Of 5,841 page IDs with a latest observation, 112 older target-category
identities were superseded by a latest non-target category, and 616 latest
target-category identities were outside Moscow. Conflicting same-date facts
are sent to review.

`data/property/estateline/projects.jsonl` contains 1,473 accepted records:

| Record kind | Rows |
| --- | ---: |
| Multifamily | 1,347 |
| Apartment complex | 114 |
| Low rise | 12 |

`data/property/estateline/review_required.jsonl` contains 59 latest records;
the disjoint primary buckets are:

| Review reason | Rows |
| --- | ---: |
| Commercial record has no explicit residential/apartment use | 33 |
| Residential wording only describes a neighboring complex | 7 |
| Moscow and Moscow-region source-region conflict | 12 |
| Explicit Moscow-region address | 3 |
| Latest source report is Moscow region | 2 |
| Explicit Kazan address | 2 |

The six reviewed “Офисный центр” cases were handled by their latest records.
Page IDs 1466108, 1479936, and 1586538 are absent from the registry because
their latest category is office buildings. IDs 1339567, 2025659, and 2974095
remain review-only: their descriptions place the office in a residential or
multifunctional complex but do not establish apartments in the office itself.
The classifier does not treat a neighboring residential project or the word
`нежилые` as evidence of housing; explicit residential and apartment use in a
mixed-use object remains eligible.

Every accepted row has `city` equal to `Москва`. `latest_known_date` is the
report-period end and means “last observed in this source,” not a live update.
Status and handover remain dated source assertions; quarter-only handover stays
`YYYY-Qn`, and floor ranges stay ranges. The registry contains no apartment
rows, unit prices, personal contacts, phone numbers, or email addresses.
Project area and investment amounts are not treated as apartment prices.

Ten addresses have a unique exact match in the local Moscow OSM address index;
the other 1,463 accepted rows have `location: null`. The ten saved tuples,
OSM identities/versions, representative points, and coordinates were checked
against the index. Address parsing rejects holdings, plots/cadastral references,
demolition/nearby descriptions, and unmapped trailing components such as
corpus or building. Only an explicit `дом`/`д.` prefix is removed as house-number
syntax; the complete house value is then matched exactly. No ambiguous OSM
candidate or nearest-object fallback is used. OSM identity and matching method
stay in server-side provenance. The pinned local OSM index uses the Moscow OSM
boundary mask and requires © OpenStreetMap contributors attribution under
ODbL-1.0.

## Data rights and reproducibility

The manifest sets `rights_status` to `user_supplied_internal_use`: the user
supplied this source for this local project. This does not assert an open
license or permission to redistribute EstateLine data publicly. The manifest
stores hashes and sizes for the 730 input books, output hashes/counts,
report-period metadata, and the local OSM index manifest. Raw workbook contents
and non-allowlisted fields are not copied to the registry.

Rebuild in the geodata environment, which provides the shared strict street
normalizer:

```sh
rtk .runtime/geodata-env/bin/python -m sources.property.estateline \
  --source-dir '/home/pirojban/Загрузки/1_xls_estateline' \
  --output-dir data/property/estateline \
  --address-index data/city/local_address_index
```

The importer replaces JSONL/schema files atomically and writes the manifest
last. The backend gate checks `projects_sha256`, `records_total`,
`rights_status`, unique registry IDs, Moscow city, and allowed record kinds.
The focused importer suite passed 9 tests in the geodata environment; it covers
real workbook controls, record selection before filtering, same-date
conflicts, residential/commercial classification, and exact address parsing
including house-prefix normalization and unsafe trailing-component rejection.

Frozen backend gate values:

- `projects_sha256`: `6edc6b68b8b29dbb892d1080dbe8e616db5462876aa2c8e152925080aa606e3e`
- `records_total`: `1473`
- `rights_status`: `user_supplied_internal_use`
- `snapshot_at`: `2026-09-06`
- verified OSM locations: `10`
