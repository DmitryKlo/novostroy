# Geometry coverage for Moscow construction candidates

Read-only audit, 2026-10-09. No source files or candidate data were changed. The pinned OSM source snapshot is 2026-10-08T20:21:06Z; the city PBF's latest object time is 2026-10-08T20:19:30Z. These dates describe the extract, not independently verified construction status.

## 756 OSM construction candidates

The strict candidate layer is [`data/property/osm_construction/osm_moscow_residential_construction_2026-10-08.parquet`](../../data/property/osm_construction/osm_moscow_residential_construction_2026-10-08.parquet) and its audit JSONL/manifest. It has 756 typed OSM objects: 687 ways and 69 multipolygon relations. All 756 IDs match the corresponding object rows in [`data/city/osm_moscow_buildings_parts_261008_v2.parquet`](../../data/city/osm_moscow_buildings_parts_261008_v2.parquet); the two files came from the same pinned Moscow PBF. This exact ID match is identity of the OSM object, not proof of project/corpus identity.

Exact parsed tag coverage among 756 candidates:

| Source tag | Count | Implication |
| --- | ---: | --- |
| `height` | 33 | Explicit mapped height; preserve as source assertion, not survey measurement. |
| `building:levels` | 244 | Explicit above-ground floor count. |
| Both `height` and `building:levels` | 3 | Do not discard either field. |
| Either height or levels | 274 | 482 candidates have neither; tag presence does not establish accuracy. |
| `min_height` / `building:min_level` | 1 / 1 | Lower-Z/level offsets are rare. |
| `roof:height` / `roof:levels` / `roof:shape` / `roof:direction` | 0 / 0 / 17 / 0 | Only roof shape is mapped, with no roof dimensions. |
| `building:part` | 0 | No candidate is directly tagged as a part. |

All candidate relation members use `outer` or `inner` roles (374/17 member references); none has a `part` role. Fifteen candidate ways are members of building relations, all as `outline`. So the candidates do have exact relation IDs, but those links define polygon outlines, not stepped vertical components.

## Ready multipart geometry

The city-wide part corpus and schema are documented in [`docs/sources/osm-building-parts.md`](../sources/osm-building-parts.md). The OSM extract contains 12,817 clipped `building_part` rows, 34 clipped `relation_member_part` rows and 17,469 building-part relation rows. Its PBF scan counted 14,947 `role=part` relation references and 1,189 clipped relations with at least two distinct explicit height/level values. These are mapped examples, not complete coverage or survey models.

The strongest ready lookup is [`data/city/osm_overture_clean_multilevel_parts_full_261009.parquet`](../../data/city/osm_overture_clean_multilevel_parts_full_261009.parquet): 800 part rows across 166 parent IDs, admitted only after exact OSM source ID/version, positive height, direct parent, geometry coverage, vertical-difference and overlap checks. The full 800-row audit GeoJSON SHA-256 is `7762084b93c697cfcd2c5a69b665d7ab1d2ffa004198e8ef83d36d89dec9b7a4`. Exact candidate-ID join to its part source IDs returns **0/756**. Keep parent/part joins exact; do not transfer the nearest clean profile to a construction footprint. For this candidate layer, use its actual 2D footprint, attach the 33 heights/244 floor facts where present, and leave stepped massing unavailable. Label any uniform extrusion as synthetic massing; leave vertical extent unknown for the remaining 482 until another exact source is found.

Relevant routines: `sources/city/osm_building_parts.py`, `sources/city/select_clean_multilevel_parts.py`, and `pipeline/apply_building_parts.py`. The existing production logic should retain parts independently, render `[min_height, height]` for exact same-version OSM-linked parts, and suppress a parent shell only under its ≥99% coverage gate. It must not fill a partly mapped structure to the tallest part.

## Bounded public-document check

These are examples from the local EstateLine candidate registry; its source URL fields are null, and its dates are historical. Public online access does not establish reuse permission.

| Candidate | Public evidence found | Geometry value and limit |
| --- | --- | --- |
| `estateline:2874610`, ul. Glavmosstroya, 1 | [Project page](https://nst-m.ru/objects/zhilye-doma/zhiloy-dom-na-ul-glavmosstroya-vl-1/) gives 55,000 m², municipal client, start 2023-02-01 and planned customer handover 2025-09-01. A [Moscow city PDF](https://www.mos.ru/upload/documents/files/3058/Aktsprilojeniyami_ylGlavmosstroya1%281%29.pdf) lists cadastral/situation plans and geological stratigraphic sections. | Useful for address/project context and plot documentation; geological sections are not building sections. The page gallery is not a documented 3D model or floor-plan package. No claim about current stage. |
| `estateline:10653`, Ostozhenka 9/14 | A [Moscow city planning/utility table](https://www.mos.ru/upload/documents/files/720/Tom11Kniga111.pdf) identifies a mixed-use reconstruction with hotel/aparthotel, spa, shop, café, offices and underground parking; planned connection years were 2023. | Confirms this is an apartment-hotel/MFC-type project, not ordinary residential housing. The table supplies no building plan/section or usable mesh. |
| `estateline:49061`, b-r Matrosa Zheleznyaka, 11 | The [project/JСК site](https://xn--11-6kctaejygw2r.xn--p1ai/) has project/facade material. A public [2022 proceedings PDF](https://pguas.ru/images/pdf/nauka/nauchnie_meropriyatiya/Tatlin/2022/%D0%A2%D0%B0%D1%82%D0%BB%D0%B8%D0%BD_2022_1.pdf) includes section diagrams 1-1/2-2; a [2024 architecture article](https://archvestnik.ru/2024/12/13/kompleksnyy-metod-pereustroystva-mnogokvartirnogo-doma/) shows a project visualization and then-current site photos. | A section may help manual reconstruction, but the proceedings are not an approved design package and their reuse terms were not checked. The 2024 stage report is not current evidence. |

No design file was downloaded, and no reuse right was inferred for the non-OSM materials. OSM-derived shapes remain subject to ODbL attribution/share-alike obligations. For mass processing, current local data supports 756 footprints, 274 candidates with explicit height/level facts, but **no verified stepped profiles among those 756**.
