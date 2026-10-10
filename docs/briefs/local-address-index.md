# Local OSM address index

The pinned Moscow OSM extract now has an offline, strict address index for
exact joins from records without coordinates. The builder is
[`sources/city/local_address_index.py`](../../sources/city/local_address_index.py);
the generated data and provenance live under
[`data/city/local_address_index/`](../../data/city/local_address_index/).
This is an OSM-derived address lookup, not a cadastral register or evidence of
OSM completeness. The city mask is relation 102269 and is explicitly not
legally certified.

`addresses.jsonl` has one row for each unique exact key. `ambiguous.jsonl`
keeps every candidate for duplicate keys, conflicting building/address tags,
or address points contained by multiple building polygons. Joiners should use
only `addresses.jsonl`; absence there means no exact unique match was produced,
and all such records should retain a null location. The builder never selects a
nearest building.

The key tuple order is
`(street_key, housenumber_key, block_key, building_key, conscriptionnumber_key)`.
`street_key` applies NFC, casefold, whitespace collapse, and a fixed Russian
street-type alias list; a recognized type is canonicalized to the prefix while
both type and street name remain present. Other punctuation and the `ё`/`е`
distinction are preserved. `housenumber_key` is the entire normalized
`addr:housenumber` value, including any `д.`, `вл.`, `корп.`, or `стр.` text.
Optional keys come only from `addr:block`, `addr:building`, and
`addr:conscriptionnumber`; they are not inferred from housenumber wording, and
null differs from every explicit value. There is no fuzzy matching.

Join code can import `address_key_tuple(street, housenumber, *, block=None,
building=None, conscriptionnumber=None)` from the builder. A source should pass
optional values only when it can identify the same component unambiguously.
Each index row stores the key list, raw `addr:*` tags, OSM identity/version and
timestamp, representative-point method, and source members. Address/building
polygons are clipped to the pinned city mask before computing GEOS
PointOnSurface; standalone address nodes keep their source coordinate. An
address node is attached to a building only when exactly one polygon strictly
contains it and explicit key components do not conflict. A point inside
multiple footprints is ambiguous.

Rebuild from the pinned local sources with
`rtk .runtime/geodata-env/bin/python -m sources.city.local_address_index`.
The builder verifies both input hashes and refuses to overwrite an existing
index directory's output files.

The pinned input hashes were checked before the index build: OSM PBF
`78aff2d4f6707c584f66febd8fb594c679b434f9046562d8d6a2c40d55da6014` and city
mask `f08ff6b0c794fd3f079ae0f9daf004f9d8de64529a7327e0fc6444788561da29`. The
extract source snapshot is dated 2026-10-08, with OSM replication timestamp
`2026-10-08T20:21:06Z`; its license is ODbL-1.0 with OpenStreetMap attribution.
The manifest records exact byte counts, hashes, mask relation metadata, tool
versions, counts, and limitations.

The index contains 120,260 unique keys and 19,395 ambiguous candidates. Unique
rows include 113,959 address building polygons, 4,256 address-point/building
composites, 1,131 standalone address nodes, and 914 other address polygons.
All unique and ambiguous output coordinates were checked against the pinned
city mask; JSONL row counts, output hashes, and unique-key uniqueness passed.
The focused tests pass in `.runtime/dev-env` (5 tests). Address coverage and
the correctness of OSM tags remain source limitations; incomplete address
tags, missing polygon geometry, and geometry-construction failures are counted
in the manifest and are not promoted to guessed coordinates.
