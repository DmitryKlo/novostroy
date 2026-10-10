# Проверка индивидуальных моделей

`python -m sources.models.build` читает локальные неизменяемые EstateLine,
OSM и Минстрой snapshot-файлы без сети. Входной clean-part corpus используется
только для exact same-version проверки: в текущих 756 OSM-кандидатах нет
подтверждённой привязки многоуровневых частей. Citywide Overture не считается
проектным реестром.

Runner создаёт `data/models/<run_id>/assessments.jsonl` по **каждой** из 2317
исходных записей, приватный `source_evaluations.jsonl` с provenance и
`candidates.jsonl` с гипотезами связей. `attempt_log` объясняет, какие строгие
проверки выполнены для каждой строки. Manifest фиксирует SHA всех входов,
выходов и rights-cleared public root. Имена кандидатов — opaque SHA namespace;
приватные source IDs не записываются в опубликованные GLB/tileset.

756 проверенных OSM-полигонов можно показывать под собственной OSM identity
как `mapped_construction`, без связи с конкретным ЖК. Плоский точный контур
появляется первым. Только явный `height` или отдельно помеченная оценка
`building:levels × 3 м` даёт упрощённый объём вторым LOD; неизвестная или
противоречивая высота остаётся плоской. OSM `building=construction` не
доказывает актуальную стадию, утверждённый проект или фасад. Права — ODbL 1.0,
обязательная атрибуция `© OpenStreetMap contributors`.

Из корня `novostroy` (в локальном `.runtime/dev-env` установлены `pyarrow`
и Shapely):

```sh
rtk proxy .runtime/dev-env/bin/python -m sources.models.build
rtk proxy .runtime/dev-env/bin/python -m unittest sources.models.tests.test_build
rtk proxy .runtime/dev-env/bin/python -m sources.models.audit \
  data/models/models-v1-92dc659b5e75fa52
```

Активный проверенный run: `models-v1-92dc659b5e75fa52`; manifest SHA-256
`e729ac65bcf9b436ab3dfa4b8f76c5bda643c3c2bcf9cbc33e7a9c4f18c28c6b`.
Для воспроизведения публикационного шага проверенного run (повторный вызов
идемпотентен):

```sh
rtk proxy .runtime/dev-env/bin/python -m sources.models.publish \
  models-v1-92dc659b5e75fa52 \
  e729ac65bcf9b436ab3dfa4b8f76c5bda643c3c2bcf9cbc33e7a9c4f18c28c6b
```

Проверенный текущий проход: 2317 source rows → 756 ready OSM mapped outlines,
60 review (59 EstateLine review + 1 EstateLine с тремя гипотезами связи),
29 rejected Minstroy events, 1472 unavailable accepted EstateLine records.
Все 756 имеют exact GeoJSON preview с MultiPolygon и внутренними отверстиями,
максимум 277 пар координат. 273 имеют схематический объём: 32 по OSM `height`
source assertion и 241 по отдельно помеченной оценке этажи × 3 м; 483
flat-only. Это **не** 756 уникальных ЖК и ни одна EstateLine-запись не стала
утверждённой моделью ЖК. IFC/CAD/лицензированные фасады отсутствуют. 1784
content-addressed public asset files занимают 2,222,318 байт raw;
1,368,200 байт — сумма gzip-5 каждого файла, а не размер одного запроса.

Новые данные сначала получают отдельный датированный snapshot через
соответствующий **офлайн** importer (см. [`EstateLine`](../../docs/briefs/estateline.md),
[`OSM`](../../data/property/osm_construction/README.md),
[`Минстрой`](../../data/property/minstroy/README.md)). Эти команды читают уже
локальные XLSX/PBF/XLS и не загружают источники из сети. После проверки прав,
схемы, новых count/hash и корпусных связей обновите pinned `INPUTS` и
`RULES_VERSION`, затем соберите **новый** immutable run и снова выполните
аудиты. Текущий runner намеренно проверяет ожидаемые source counts и остановится
при неожиданном изменении входа; он не превращает новый адрес/донор в
автоматическое доказательство корпуса.

Asset GLB хранятся в content-addressed cache; прерванная сборка сохраняет уже
готовые GLB. Повторный запуск с теми же входами валидирует готовый run и
возвращает его без перезаписи. Публикация переключает sanitized assets pointer,
затем active data pointer. Недопроверенный run остаётся staged и не попадает
в API.
