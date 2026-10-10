# Дома по наблюдениям квартир — 10 октября 2026

Активен приватный снимок `developer-map-v1-7092646e252c8681` (manifest SHA-256
`330994e0574233aca34e896ad2563bbf82a5ea83601a3624c841ad03c66446df`).
Список и карта читают один `developer-map-snapshot-3d1f1f851d73a443` через
`data/property/developer_map/active.json`. Прежний EstateLine/OSM набор сохранён
в своих снимках для аудита и отката, но не смешивается с новым видимым набором.

В списке **1 163 записи корпусов**, к которым источники привязали **45 469 provider-scoped
идентификаторов квартир**. На карте **1 027 точек конкретных домов**: 1 026
репрезентативных точек исходных контуров корпусов Proplanirovki внутри закреплённой
границы Москвы и одна точка FSK The Lake (официальный адрес Снежная, 20А,
единственный корпус; уникальный OSM way/1317814900 v2 того же адреса). Ещё
**136 записей корпусов** остаются в списке без координаты: 107 A101 по точной паре
`project_slug + building.corpus`, 28 FSK по собственному ID корпуса и один
Proplanirovki с непригодной геометрией. Центр ЖК не перенесён на его корпуса.

| Источник | Дома в списке | Точки | ID квартир в списке | ID квартир вне списка |
| --- | ---: | ---: | ---: | ---: |
| Proplanirovki | 1 027 | 1 026 | 40 195 (включая 3 ID объединённого The Lake) | 26 854 |
| A101 | 107 | 0 | 4 117 | 676 |
| FSK | 29 | 1 | 1 157 | 10 |
| Brusnika | 0 | 0 | 0 | 424 |
| Granel | 0 | 0 | 0 | 2 288 |
| Level | 0 | 0 | 0 | 1 278 |
| Etalon | 0 | 0 | 0 | 677 |
| AFI | 0 | 0 | 0 | 14 |

Эти числа относятся к идентификаторам каждого издателя, а не к уникальным
физическим квартирам между сайтами. Исключённые 32 221 ID остаются в приватном
`rejections.jsonl`: источник указывает МО/иной регион, местоположение Москвы
не подтверждено, нет точной связи квартиры с корпусом либо адреса/геометрии
корпуса. Причины и счётчики по источникам закреплены в manifest; разные причины
могут относиться к одной группе, поэтому их нельзя складывать как отдельные ID.
Отдельные геокодинговые проверки без точной точки остаются review, не «не найдено».
После фиксации активного снимка получены дополнительные приватные результаты для
всех остальных 28 FSK корпусов: 11 задач у address_tail_a
(`.runtime/research/developer-harvest/geocode-evidence/fsk_address_tail_a-results.jsonl`, результат SHA-256
`d4b41013a6c13f963383f345d1b642aea85fa58aef0b496933d6a2584d4c2782`)
и 17 у luna_clinics
(`.runtime/research/developer-harvest/geocode-evidence/luna_clinics-remaining-results.jsonl`, SHA-256
`6eef03218b2aa1545f2d34414f3e28264bc33c9e56c399e4703aacda6b6071ba`).
Все остались `review` с `coordinates=null`: встречены неоднозначные строения
или проектный адрес без точного корпуса (например, Sydney City,
Rotterdam, Amber City и Simonovsky). Эти два поздних файла не включены в
хеш активного run и не дают оснований добавлять к 1 027 точкам.

Отдельно после активации найден один доказанный межисточниковый дубль The Lake:
FSK `636ce8f7f459625c9efe72ad/639b34e2b176d8bf3ad3d6b1` и
Proplanirovki `63a05817dca7d172f90dd3a0/63a05a5fdca7d16ff10dd557`.
Официальная страница FSK даёт ровно один корпус по адресу Снежная, 20А;
OSM way/1317814900 v2 имеет тот же адрес. Исходный контур корпуса «Лэйк»
агрегатора на улице Снежной покрыт OSM-контуром на 65,6% своей площади
(Jaccard 29,0%); OSM-контур пересекает ровно один из 4 469 валидных
корпусных контуров Proplanirovki — этот. Совпадение названия и номера корпуса
служит дополнительным доказательством, а не самостоятельным правилом по близости.
Проверенное соответствие и исходные хеши сохранены в
`.runtime/research/developer-map-crosswalk-20261010/manifest.json`
(SHA-256 `6488ec8092a06ac1fc94ad01861c9c0a642f0149325ea8627de70a6c7fd8bfa5`).
Активный снимок `developer-map-v1-7092646e252c8681`
(manifest SHA-256 `330994e0574233aca34e896ad2563bbf82a5ea83601a3624c841ad03c66446df`)
содержит 1 163 записи корпусов, 1 027 точек и 136 записей без координаты.
Он оставляет публичную FSK-запись с 3 наблюдёнными ID этого источника, а
ещё 3 ID Proplanirovki и обе исходные идентичности хранит раздельно в
приватном provenance. Число разных физических квартир по двум источникам
не установлено. Остальные межисточниковые пары не считались автоматически
одним домом; общая физическая уникальность 1 163 записей не доказана.

Снимок публикует только фактические названия, адрес/корпус, число наблюдённых
ID квартир и координату корпуса с основанием. Число квартир не означает текущую
доступность. Цены, изображения, ссылки на первичные страницы и сырые HTML не
копируются в публичный ответ; исходные права остаются `unknown`. Флаг сбора
`public_display_allowed=false` сохранён в исходных наблюдениях как консервативное
значение для медиа/сырых данных; он не менялся и не трактуется как разрешение на
их распространение. Контуры Proplanirovki — геометрия агрегатора, не
геодезически подтверждённый план. OSM-тег The Lake `building=construction`
датирован апрелем 2026 года и не используется как текущий статус дома.

Воспроизводимый локальный запуск без сети (все входы закреплены хешами):

```bash
cd /home/pirojban/novostroy
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_map build \
  --delivery-manifest .runtime/research/developer-harvest/deliveries/developer-delivery-919ca9ca87c416f4/manifest.json \
  --level-manifest .runtime/research/developer-harvest/developer-harvest-22ba44b125df5339/manifest.json \
  --etalon-manifest .runtime/research/developer-harvest/supplemental-etalon-api-final-20261010/manifest.json \
  --afi-manifest .runtime/research/developer-harvest/domain-entry-audit-20261010/supplemental-afi/manifest.json \
  --geocode-manifest .runtime/research/developer-harvest/geocode-evidence/address_tail_a-manifest.json \
  --geocode-manifest .runtime/research/developer-harvest/geocode-evidence/luna_clinics-lake-verified.manifest.json \
  --crosswalk-manifest .runtime/research/developer-map-crosswalk-20261010/manifest.json
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_map validate developer-map-v1-7092646e252c8681
rtk proxy .runtime/dev-env/bin/python integration/check_developer_map.py --active
```

Повторный `build` с теми же входами проверяет существующий неизменяемый run
и не перезаписывает его. Активация одного проверенного run — атомарная запись
`active.json` командой `publish RUN_ID --expected-active-sha SHA256_ТЕКУЩЕГО_УКАЗАТЕЛЯ`.
Для отката к прежним EstateLine/OSM маршрутам предусмотрен
`rollback-legacy --expected-active-sha SHA256_ТЕКУЩЕГО_УКАЗАТЕЛЯ`: команда
требует точный CAS-хеш, сохраняет копию указателя в `pointer_history/` и не
удаляет run. Возврат к снимку домов выполняется той же командой `publish` с
SHA-256 пустого указателя `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
Откат не запускался на рабочем наборе.

Проверки: тесты publisher 6/6, `validate` прошёл; staged backend direct/ASGI,
JSON Schema, отсутствие старых точек и редактирование приватных полей прошли.
Активный API и браузер также проверены после переключения.
