# Developer building menu and selected reconstructions — 2026-10-11

The immutable developer map remains `developer-map-v1-7092646e252c8681`
(1,163 corpus records, 1,027 map points, 136 list-only). Its apartment menu and
selected models are published together through the
`data/property/developer_experience/active.json` pointer. The map itself, legacy
source snapshots, and prior model runs were not replaced.

## Staged inputs and results

| Artifact | Run / SHA-256 | Result |
| --- | --- | --- |
| Map | `developer-map-v1-7092646e252c8681` / manifest `330994e0574233aca34e896ad2563bbf82a5ea83601a3624c841ad03c66446df` | Existing active corpus identities |
| Apartment menu | `developer-units-v1-e527bb9c8cd04378` / manifest `eaff2d8fd96bbde8a75f866746965f7fe30e58d54fb4717d353710941d2a5406` | 1,163 buildings, 45,466 primary source unit observations, 3 separate cross-source review aliases, 12,982 section/floor groups, 62,553 private plan refs |
| Prepared plans | Within the menu run | 4 local PNGs / 567,726 bytes: three apartment plans bound to FSK unit IDs, one source floor-plan image bound to section 2, floor 5 of The Lake. Five traced regions are anonymous; no region is asserted to match a listed unit. |
| Selected models | `developer-models-v1-5a525ff1aaba871b` / manifest `278c3aa54149ea6c04251b1c8cb4e3cf10d8ef994d7d1ea31935e56fdebf1da4` | 856 hash-checked GLBs / 4,045,264 bytes; no citywide model tileset |

These are *provider-scoped source unit observations*, not a deduplicated physical
apartment inventory across providers. Prices and source update dates are source
observations, not confirmed current offers. Only the four prepared plans have
locally verified image bytes; other plan refs remain private metadata. Original
media URLs and publisher IDs remain in the private run and are excluded from
public DTOs.

## Height and model basis

Of 1,026 mapped Proplanirovki corpora, 855 have one nonconflicting source
`apartments[].floors` value and receive a selected-only GLB using the exact
source corpus polygon and **estimated** 3 m per reported floor. The field is
reported by the aggregator in apartment records; it is not an engineering
measurement. Another 170 corpora have multiple values and stay flat. The
Намёткин Тауэр corpus is also flat even though those apartment rows all report
29: the [official project page](https://nametkin-tower.ru/) says its four
sections range from 24 to 29 floors, without assigning sections to the mapped
corpus polygon. One unlocated Proplanirovki corpus retains a single source
floor count in the menu but has no map model.

FSK exposes section-level totals for 29 corpora, including The Lake: section 1
has 16 floors and section 2 has 10. The menu keeps those counts separately; it
does not promote them to one building total. The other 107 A101 corpus groups
have only the maximum floor of *listed apartments*, a lower bound rather than
total floors. Floor/section identifiers are exact source labels; apartment
floor maxima never produce a model height.

The Lake adds one UV-textured GLB (`77a852cf2a0d90ab4e90ae46316a54b379c989140fe6edafa5b9168d2cff0899`)
on the reviewed exact OSM way/1317814900 v2 footprint. It uses a generated,
photo-informed façade repeat and a **schematic** 10 × 3 m minimum-section
height, labelled «реконструкция». The 16-floor section is not placed on the
polygon: no source identifies which geometric part belongs to either section
or the camera orientation. The generated texture is not a source photograph,
an as-built façade, a surveyed height, or an approved design. OSM footprint
attribution is `© OpenStreetMap contributors` (ODbL 1.0); source media rights
remain recorded as unknown. The original three unit SVGs were screened for
external/active elements and rasterized; only PNGs are serveable.

The floor-five image's five traced regions use normalized image coordinates,
not building coordinates or measured floor geometry. The highlighted 83.7 m²
label differs from the official 84.3 m² listing for unit 169451, so all five
regions keep `unit_id=null` and `match_status=unconfirmed`. They may open an
anonymous reconstructed floor volume/label, never a claimed unit link.

## Publication and verification

The one commit marker has schema `developer-experience-pointer/1` and pins the
active map run/snapshot/manifest plus exact menu and model run/manifest hashes.
`sources.property.developer_experience` validates both readers and every
allowlisted PNG/GLB before atomic CAS replacement. Rollback restores the
previous pointer (or the prior absence) while retaining immutable runs. The
selected GLB is loaded on demand. During selection the web renderer hides only
the city-volume tileset and restores it on deselection, avoiding duplicate
prisms; ground, roads, parks, water, and markers remain.

The producer's focused tests passed (12). Backend direct stage validation
passed for 1,163 buildings, 45,466 units, 4 PNGs, 5 floor regions, and
856/856 model assets; all local asset hashes passed. Root activated the
pointer by CAS; its current SHA-256 is
`6f4ee2fcb8b2740e13c403694e140508ca5fdf8cbe1ee339c5c6cd037db1865a`.
Root's active menu/model/filter direct and ASGI gates and `dev.py restart-api`
plus `verify` passed. The existing live desktop/mobile browser report at
`.runtime/browser-developer-live/2026-10-10T21-16-40.917Z/report.json` passed.
The expanded live menu/model gate also passed on desktop and mobile Chromium:
`.runtime/browser-developer-experience-live/2026-10-10T21-19-47.494Z/report.json`
records the final map/menu/model run IDs, four filter totals, five anonymous
plan regions, a 2,429,484-byte Lake GLB, and no page errors. WebKit could not
launch because host libraries are missing; a physical iPhone was not tested.

To inspect the active pointer:

```bash
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_experience status
```

The exact first CAS publication command used this prior-absence hash:

```bash
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_experience publish --map-run-id developer-map-v1-7092646e252c8681 --unit-run-id developer-units-v1-e527bb9c8cd04378 --model-run-id developer-models-v1-5a525ff1aaba871b --expected-active-sha e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

Rollback uses the current published pointer hash:

```bash
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_experience rollback --expected-active-sha CURRENT_POINTER_SHA256
```
