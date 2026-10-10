# Current EstateLine geocoding audit

Audit date: 2026-10-10. The active age-filtered registry currently has 639 visible EstateLine records and 0 accepted coordinates among those records. The raw EstateLine workbooks provide project IDs, addresses, report dates, and project descriptions, but no coordinates. No project data, importer, or sidecar was changed in this audit.

## Unverified candidates

### `estateline:1416611` — «Мичуринский парк», корпуса 4.4 и 4.5

EstateLine's latest source observation is 2026-06-21 (report `moskva-week-21062026-b5821615a8a7438e60c73929f01c2292.xlsx`; source page: <https://estateline.ru/baza-stroyashchikhsya-obektov/1416611>). It describes the 3rd queue, stage 1, corps 4.4/4.5; address Moscow, postcode 119361, Озёрная, владение 44, with cadastral parcel `77:07:0014002:4523` at Никулинская, 33А/Б.

Local OSM has a plausible exact-address building candidate, `relation/17615621` v2, <https://www.openstreetmap.org/relation/17615621>. Its tags are city Moscow, postcode 119361, Озёрная улица, 44; building=apartments, 33 levels, 1103 flats. Its local footprint point-on-surface is `[37.44095911850066, 55.66819315]` (lon, lat), based on the 2026-10-08 OSM snapshot. The local address index flags the bare street/house key as ambiguous because another same-named address exists in Настасьино. City and postcode tags distinguish the Moscow feature, but this manual disambiguation does not change the index result or establish the specific corpus identity.

Official Moscow [decree 410-РП](https://www.mos.ru/upload/documents/docs/410-RP-isad2.pdf) ties parcel `77:07:0014002:4523` to Никулинская, 33А/Б and developer ООО СЗ «Озёрная 44». The city's [construction page](https://stroi.mos.ru/construction/3392) lists corps 4.4/4.5 at that parcel; PIK's [project inventory](https://www.pik.ru/search/m-ozernaya?settlement=1.2028) also lists both corps and their floor counts. These sources identify the wider project/site, but do not establish that OSM relation 17615621's seven-part footprint is specifically corps 4.4/4.5. The OSM 33-level tag being close to the listed 32-storey corpus is only consistency evidence, not identity evidence. **Status: `candidate_unverified`; do not import its point.**

To promote it, obtain an official georeferenced site/genplan or address/corpus record that identifies the individual 4.4/4.5 footprint and permits an exact contour-to-OSM-ID match. Shared parcel, project name, or similar height is insufficient.

### `estateline:1344630` — «Сигнальный 16», 2-я очередь

EstateLine's latest source observation is 2026-02-22 (report `moskva-week-22022026-e47db691fa74e396f040ce8dd8f34549.xlsx`; source page: <https://estateline.ru/baza-stroyashchikhsya-obektov/1344630>). It describes corps 2.1/2.2 and gives address `Сигнальный проезд, владение 16, строение 2`.

The local exact-address index has one building candidate `relation/21000467` v1, <https://www.openstreetmap.org/relation/21000467>, tagged `Сигнальный проезд`, `16 к2`, apartments, 33 levels. The official [Moscow project page](https://uznai.mos.ru/catalog/8949_signalnyj_16) identifies ЖК «Сигнальный 16» and the city [planning PDF](https://www.mos.ru/upload/documents/files/6256/Aktsprilojeniyami_Signalnii16.pdf) identifies the wider site at Сигнальный проезд, владение 16. An official EIS housing listing uses address 16, корпус 2: <https://xn--80az8a.xn--d1aqf.xn--p1ai/сервисы/каталог-квартир/квартира/9ec5705eb587ea1827839dfa13f06ec7>.

The `строение 2` vs `корпус 2` component is unresolved, and no source yet ties relation 21000467 to the specific 2.1/2.2 footprint. **Status: `candidate_unverified`; do not import its point.** Needed proof: an official address/corpus or georeferenced plan explicitly crosswalking the EstateLine building component and OSM relation.

## Rejected address match

### `estateline:1441536` — Springs, Малая Филёвская, владение 46

The fresh EstateLine record (latest observation 2026-08-02; <https://estateline.ru/baza-stroyashchikhsya-obektov/1441536>) identifies Springs, a new project with a 2024 construction permit and 25-storey description. The unique local address-index match for Малая Филёвская, 46 is OSM `relation/7384309` v1, timestamp 2017-07-09, tagged only `building=yes`, `building:levels=2`: <https://www.openstreetmap.org/relation/7384309>. This is an old low-rise address footprint, not evidence for the fresh Springs tower. **Reject this match; do not use its point** `[37.45061416829394, 55.73471345]`.

## Result

There are **0 accepted coordinates** for the current visible EstateLine registry from this audit. The two candidate geometries remain review-only; Springs is a confirmed false positive for project identity. No timestamp or project status was refreshed from these crosswalk attempts.
