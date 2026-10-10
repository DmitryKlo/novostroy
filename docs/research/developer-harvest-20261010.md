# Частный сбор открытых каталогов застройщиков, 2026-10-10

Локальная точка входа для проверки результата — [машинный manifest частной поставки](../../.runtime/research/developer-harvest/deliveries/developer-delivery-919ca9ca87c416f4/manifest.json). Для таблиц без сети откройте [manifest CSV-экспорта](../../.runtime/research/developer-harvest/exports/developer-csv-31f897e309b5feb2/manifest.json): в той же папке лежат `units.csv`, `moscow_spatial_candidates.csv`, `projects_corpora.csv` и `media_refs.csv`. [Независимая проверка качества](../../.runtime/research/developer-harvest/qa/developer-delivery-919ca9ca87c416f4/quality.json) сверила SHA и количество строк восьми исходных файлов; две цены остались неизвестны, и текущая доступность не подтверждена ни для одной записи. [Доказательство проверки 12 прямых URL](../../.runtime/research/developer-harvest/media-probe-2a0774a5967b6311/manifest.json) и [двух буквальных proxy URL проектных изображений](../../.runtime/research/developer-harvest/media-probe-2a0774a5967b6311/proplan-literal-proxy-proof.json) отделены от исходного корпуса данных. Эти пути существуют только в текущем локальном workspace; файлы частные, на сайт не публиковались.

Отдельный [аудит входных страниц официальных доменов](developer-domain-entry-audit-20261010.md) завершён: 333 исследованных directory labels, 203 подтверждённых официальных хоста, из которых 197 вошли в ограниченный план; 271 попытка, 235 принятых HTML-ответов HTTP 200. Это проверка входных URL, а не полный сбор 203 каталогов. Последующий **отдельный** Level run прошёл весь буквальный `next`-каталог: 1 291 строковое место, 1 278 разных publisher unit ID, 3 047 URL планов. Отдельный [Etalon run](../../.runtime/research/developer-harvest/supplemental-etalon-api-final-20261010/manifest.json) охватил семь буквальных проектных каталогов и 677 разных source unit ID; три строки имеют конфликт между меткой Москвы и адресом в области, поэтому не считаются подтверждённо московскими. Эти supplemental записи не прибавлены к 75 721 provider-scoped unit ID основной поставки.

`sources.property.developer_harvest` собирает только обычные общедоступные HTTPS-страницы из локального списка точных хостов. Его результат остаётся в `.runtime/research/developer-harvest/`; ни один offer, снимок или медиафайл не публикуется на сайте автоматически. Доступность страницы не подтверждает право на копирование изображений или дальнейшее использование ценового каталога.

Запуск из корня проекта с установленным локальным Python-окружением:

```bash
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_harvest \
  --seed-file .runtime/research/developer-harvest/seeds.json status
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_harvest \
  --seed-file .runtime/research/developer-harvest/seeds.json run \
  --capture-manifest .runtime/research/developer-harvest/captures.json \
  --live --transport curl --max-pages 1000 --delay-seconds 1
```

Вторая команда возобновляется без повторного GET для уже успешно полученных URL. `--retry-transport-errors` повторно ставит в очередь только сетевые ошибки; HTTP 401/403/429 и страницы проверки человека блокируют хост. Лимит пяти страниц относится к адресному поиску, а не к ссылкам `next` подтверждённого каталога. Обход авторизации, CAPTCHA, приватных API и угадывание URL не используются. Сетевой запуск возможен только в окружении с обычным доступом к этим публичным хостам; локальный sandbox может не иметь DNS.

Каждый versioned run хранит SQLite-checkpoint, контентно адресованные тела ответов, `requests.jsonl`, `errors.jsonl`, `observations.jsonl`, `observations.csv`, `developer_units.jsonl`, `developer_projects.jsonl` и `manifest.json` с SHA-256 и счётчиками. `status` и `dry-run` читают checkpoint без новых сетевых запросов или изменений очереди. При смене парсера новый run может повторно разобрать проверенные тела прежнего run без сетевого повтора в пределах 24 часов; отрицательные HTTP-ответы не переводятся в успех. Старые частные run сохраняются для аудита.

После завершения обоих run единая частная поставка собирается без сети и только из совпадающих по SHA манифестов:

```bash
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_delivery \
  --run .runtime/research/developer-harvest/developer-harvest-da11a701fee80ae2 \
  --run .runtime/research/developer-harvest/developer-harvest-3b32494f59534076 \
  --boundary-geojson data/city/osm_moscow_city_boundary.geojson \
  --boundary-manifest data/city/osm_moscow_city_boundary.manifest.json
```

`developer_delivery` отклоняет незавершённый каталог, ошибки/блокировку и изменённые файлы. Выход `deliveries/developer-delivery-<hash>/` содержит all-source `observations.jsonl`, отдельные проектные/корпусные записи, раздельные `units_moscow.jsonl`, `units_moscow_oblast.jsonl`, `units_other_region.jsonl`, `units_outside_moscow_unresolved.jsonl`, `units_unknown.jsonl`, `units_geometry_conflict_quarantine.jsonl`, точные publisher-ID связи `relationships.jsonl`, URL и исходную роль медиа в `media_refs.jsonl`, а также SHA-манифест. Отдельный `units_moscow_spatial_candidates.jsonl` повторяет только часть unknown с источниковой точкой/корпусным полигоном внутри закреплённой границы; в каждой строке остаются `source_location_scope=unknown`, способ проверки и SHA границы, а точный адрес не присваивается. «Московская область» ставится лишь при явном тексте источника; агрегаторный `region=msk` сам по себе означает смешанный рынок. Проверка source project point/corpus polygon по закреплённой границе ОSM — отдельная ось `geometry_inside_moscow`; она не заменяет `location_scope`, а расхождения уходят в карантин. Это OSM-кандидатная граница, не юридическое подтверждение адреса. Для этой локальной геометрической проверки нужен Shapely. Отсутствующая связь publisher ID остаётся unresolved, а не заменяется сопоставлением по имени или расстоянию. Ссылки на планы и фото остаются частными URL без скачивания и права на публичный показ.

Один `observation_id` строится из provider + явного publisher ID и типа сущности; page-local identity отдельно помечается и не годится для автоматической привязки. Конфликт publisher project IDs на одном unit ID помечается `identity_conflict`. Времена `observed_at` для импортированных root captures получены из локального времени файла после ordinary GET и **не** являются утверждением девелопера о дате обновления объекта; `source_updated_at` заполнено только из поля источника. Цена сохраняется с исходным basis и условиями акции, если они явно указаны. Media остаётся URL-ссылкой с `rights_status=unknown`, `public_display_allowed=false`; изображения здесь не скачиваются.

`developer_units.jsonl` включает только записи со строгим `location_scope=in_scope_moscow`. При отсутствии адреса у unit этот scope может наследоваться исключительно от проектной записи того же provider с идентичным `publisher_project_id`, проверенным московским адресом и сохранённым `scope_evidence_observation_id`; название ЖК или близость не создают связи. Адрес проекта при этом не объявляется точным адресом корпуса. Каталоги A101, ФСК, Гранель и Брусники могут включать Московскую область и иные регионы; supplemental `proplanirovki.ru` — агрегатор, его 475 проектов в `region=msk` также не равны 475 проектам в границах Москвы. `manifest.json` отдельно показывает число исходных наблюдений, допущенных московских unit, источник denominator, завершённые страницы и оставшуюся очередь. `all_moscow_coverage_claimed=false` до отдельного аудита реестра всех официальных каталогов.

Проверка парсеров и сборщика на source-only fixtures:

```bash
rtk proxy .runtime/dev-env/bin/python -m pytest -q \
  sources/property/tests/test_developer_harvest.py \
  sources/property/tests/test_developers_a101.py \
  sources/property/tests/test_developers_fsk.py \
  sources/property/tests/test_developer_granel_brusnika.py \
  sources/property/tests/test_developers_proplanirovki.py
rtk proxy .runtime/dev-env/bin/python -m pytest -q \
  sources/property/tests/test_developer_delivery.py \
  sources/property/tests/test_developer_delivery_csv.py \
  sources/property/tests/test_developer_media_probe.py
```

Качество полученных записей нужно проверять по финальному manifest и raw hash: `catalog_exhausted` относится к опубликованному источником числу unit, `project_inventory_exhausted` — к числу проектных страниц. Ошибка парсера, ограничение размера ответа или флаг `unit_records_truncated` оставляют страницу незавершённой, а не превращают частичный список в полный. Сопоставление с EstateLine/OSM выполняется отдельно и не меняет production-данные без ревью.

Офлайн-аудит завершённых исходных цепочек выявил важное ограничение offset-пагинации. A101 отдал 4 807 строковых мест на 268 страницах, но 4 793 уникальных unit ID; 14 мест повторились. ФСК отдал 1 171 элементов `items[]` по 59 уникальным offset, но 1 167 уникальных ID; дополнительный захват offset 18 с другим порядком параметров имел идентичный SHA и в denominator не добавляется. Брусника отдала 424 элемента и 424 ID. Каноническая цепочка Гранель `/flats/` → 106 API-страниц содержит 24 SSR + 2 528 API-элементов = 2 552 исходных места, как и API `count=2552`; начальная SSR-страница отдельно показывала 2 554. Из API-элементов 7 имеют `type=apartments` и явно исключены из квартирного `type=flat` parser. У квартирных ID при разных offset обнаружены повторы, в том числе через несколько страниц; причина перестановки неизвестна. Отфильтрованные проектные/корпусные цепочки дополнительно перекрываются с общим каталогом, поэтому сумма всех их мест не служит числом разных квартир. Проверены 304 сохранённые связи «родительский каталог → следующий URL» Гранель: все были явными ссылками источника, с переводом HTTP-ссылки источника на HTTPS того же разрешённого хоста. Повторять сотни GET ради неустойчивого offset без стабильного snapshot/cursor оснований нет.

У дополнительного агрегатора все 20 страниц списка и 475/475 исходных проектных страниц получены. SHA каждого body совпал с checkpoint; сумма `apartments[]` по проектам, число разобранных строк, число разных unit ID и сумма `apartmentsTotal` совпали: 67 049. Максимальный массив на странице — 1 059, меньше ограничения парсера 4 999; обрезанных проектов нет. Это смешанный рынок Москвы и области, не доказательство полного каталога московских новостроек.

Во всех пяти run `catalog_pages_remaining=0` и `project_pages_remaining=0`. Оставшиеся в исходном манифесте 18 URL ФСК и 661 URL Гранель имеют `page_kind=listing`: это отдельные карточки квартир, намеренно не загруженные массово после получения каталожных полей, а не недойденные страницы `next`. При этом `catalog_exhausted=false` для A101, ФСК и Гранель остаётся консервативным: заявленный счётчик и число разных ID не совпали, поэтому завершение цепочки ссылок не выдаётся за доказанную полноту разных квартир. У агрегатора полный проверяемый denominator — 475 проектных страниц, а не отсутствующий в API общий unit count.

Итоговая частная поставка — `.runtime/research/developer-harvest/deliveries/developer-delivery-919ca9ca87c416f4/manifest.json` (SHA `31f897e309b5feb27ab8b73acb78388d9de10d8b9e15c36faa3aaf15332122f0`). Исходные SHA манифестов: четыре провайдера `582de4d4314f84a7a3a6f2bc8b141ccae27d5eab6346e566e788668ad4f5dee9`, агрегатор `9c0ad2c3dc83df312c25d01d9f497e62374e5f16926c09e31dceb88d7f042cda`. В ней 81 186 исходных наблюдений, 75 721 уникальный **внутри провайдера** publisher unit ID, 5 465 проектных/корпусных записей и 148 830 связей по явным publisher ID. Это не 75 721 доказанно разных физических квартир между источниками: межпровайдерный dedup не выполнялся. Разделы units: Москва по строгому источниковому scope 14 969; явно Московская область 26 623; иной регион 196; вне Москвы без доказанного региона 357; неизвестно 33 576; геометрические конфликты 0. Из unknown отдельно выделено 31 663 **пересекающихся с ним** пространственных кандидата, без подтверждённого адреса и без прибавления к strict Moscow. Для 4 469 из 4 470 агрегаторных корпусов сохранён проверенный синтаксис source Polygon; это геометрия агрегатора, а не подтверждённый чертёж или утверждённый контур. Поле `observed_at` покрывает `2026-10-10T15:14:41+00:00`—`2026-10-10T16:38:08+00:00`; время получения страницы не доказывает текущую доступность квартиры или дату обновления цены самим источником. Все 12 явных project href ФСК получены (11 дополнительных обычных GET); точный publisher ID и явное московское адресное поле привязали 1 157 из 1 167 её unit ID к strict Moscow без сопоставления по имени. Оставшиеся 10 не получили такого основания в unit projection.

Отдельный private табличный экспорт из уже проверенной поставки воспроизводится без сети; исходные JSONL не меняются:

```bash
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_delivery_csv \
  --delivery-dir .runtime/research/developer-harvest/deliveries/developer-delivery-919ca9ca87c416f4 \
  --output-root .runtime/research/developer-harvest/exports
```

Готовый `exports/developer-csv-31f897e309b5feb2/manifest.json` закрепляет SHA каждого файла: `units.csv` 75 721 строка, `moscow_spatial_candidates.csv` 31 663, `projects_corpora.csv` 5 465, `media_refs.csv` 120 838. Формат — UTF-8 BOM, `;`, CRLF; формулы в исходных строках обезврежены для открытия в Excel. Экспорт остаётся частным и не подтверждает права на показ изображений.

В частном `media_refs.jsonl` 120 838 типизированных ссылочных записей планов и проектных изображений, соответствующих 78 682 различным строкам URL, с исходной страницей и `public_display_allowed=false`. Большинство ссылок не проверялись запросом и ни одна не получает права публикации от факта HTTP 200. Ровно 12 воспроизводимо выбранных образцов разных типов и пяти источников были проверены отдельно, не обходя весь список:

```bash
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_media_probe \
  --delivery-dir .runtime/research/developer-harvest/deliveries/developer-delivery-2a0774a5967b6311 \
  --output-dir .runtime/research/developer-harvest/media-probe-2a0774a5967b6311
# После просмотра dry plan — тот же вызов с --live, только на машине с обычным HTTPS-доступом.
```

Проверка ограничена 10 МБ на ответ, 25 секундами, паузой 1 с на хост; реальный 401/403/429 блокирует следующие запросы к этому хосту. `manifest.json` probe сохраняет исходный URL, HTTP-статус, Content-Type, размер, SHA и результат проверки сигнатуры файла; подтверждённые bytes остаются только в private `.runtime`. Повтор того же probe использует manifest без повторных GET. Это проверка доступности и формата нескольких образцов, не лицензия на публичное хранение или показ.

Фактический ограниченный live-probe записан в `.runtime/research/developer-harvest/media-probe-2a0774a5967b6311/manifest.json` (SHA `cf3d5f252f63076bbc269ed6f46820e80fb942926eb8bec4408f3fc9b3deb232`): 12 прямых попыток, девять HTTP 200 с совпавшими Content-Type и сигнатурой изображения/плана (A101, ФСК, Гранель, Брусника); три прямых `cdn-dataout.trendagent.ru` URL завершились `curl_exit_28`, 0 байт, без HTTP-статуса. Изображения были выбраны из первой immutable delivery; `media_refs.jsonl` в итоговой delivery побайтно совпадает (SHA `cc6338d753aad3a8ade314c829ef9f4ddfb44870579be98d20cb4c4174081830`), поэтому повторный GET тех же URL не нужен. Тайм-аут не доказывает отсутствия файлов или запрета доступа; остальные десятки тысяч URL не проверялись. Повтор того же прямого CDN URL в доступном окружении также дал тайм-аут и не повторяется.

Отдельно проверены **два буквальных URL изображений проекта** на том же `proplanirovki.ru`, которые присутствовали в сохранённом публичном HTML; декодированные URL исходного CDN совпали с `media_refs.jsonl`. Оба обычных GET вернули HTTP 200 и JPEG: `project_cover` — 35 924 байта, SHA `3c0c8e5038852ada95790e4deee61fc776954215d9ddee5f23539f87e1da1c99`; `project_rendering` — 6 500 байт, SHA `c9db59212f002d5590028dd2fe68ed0bc84830932e46fc0d5d5d43a1deedc061`. URL, исходный HTML SHA, заголовки, время, локальные bytes и права `unknown` закреплены в `.runtime/research/developer-harvest/media-probe-2a0774a5967b6311/proplan-literal-proxy-proof.json` (SHA `42050194cbf2ca736d18e735d7659d09f80002d97d3941b3e03fb571695f2d7d`). Это два дополнительных проверенных **проектных** изображения, а не доказательство доступности прямого CDN, планов квартир или остальных медиа. Публикация и производные остаются запрещены до отдельной проверки прав.
