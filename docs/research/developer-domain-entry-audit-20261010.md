# Частная проверка входных страниц официальных доменов, 2026-10-10

Это отдельная проверка **входных страниц**, не новый полный сбор квартир. Входы — 323 обработанные записи `domain-review-luna_coverage/batch-*.jsonl` и 10 ранних `official_domain_evidence.jsonl`, всего 333 directory labels. Среди них 237 строк с подтверждённым официальным доменом сводятся к 203 хостам. Четыре хоста A101, ФСК, Гранель и Брусника уже получили отдельные каталожные run; ПИК и «Самолёт» пропущены из-за ранее наблюдавшегося HTTP 401. Оставшиеся 197 хостов составляют ограниченный план GET. Не найденный или неоднозначный официальный домен не превращается в вымышленный URL. Ни directory label, ни число проектов в агрегаторе не являются проверенным юридическим идентификатором застройщика или охватом всей Москвы.

Читаемый локальный индекс — [план официальных хостов](../../.runtime/research/developer-harvest/domain-entry-audit-20261010/plan.json) и [таблица доменов](../../.runtime/research/developer-harvest/domain-entry-audit-20261010/domains.csv); фактические ответы — в `requests.jsonl`, SHA-адресованных `raw/*.body` и `summary.json` в той же приватной папке. Таблица сохраняет различие между уже собранным каталогом, прежним 401, HTTP-ответом, сетевой ошибкой и отсутствием подтверждённого домена. Входные страницы исследуются не более двух URL на хост, оба URL должны присутствовать в доказательстве реестра; иных маршрутов сканер не угадывает. На хост действует пауза не менее секунды. HTTP 401/403/429 и подтверждённый challenge останавливают этот хост. Незавершённый сетевой запуск возобновляется по журналу без повторного GET для уже записанных URL; четыре workers независимы по хосту, а один writer фиксирует каждый ответ до следующей страницы того же хоста.

Фактический запуск завершён: **271 попытка на 197 хостах**, 235 принятых HTML-ответов HTTP 200, 24 транспортные ошибки, четыре остановленных хоста с HTTP 403; непроверенных URL из ограниченного плана не осталось. В сыром журнале 236 кодов 200, но один ответ был PDF с `unexpected_content_type` и не учитывается как HTML. Также записаны три HTTP 301 и четыре HTTP 404. Повторных пар `(хост, URL)` нет, максимум два запроса на хост. SHA `requests.jsonl` — `e95e9704edd528bdbf526eadf0a348d3f4da18745858d0dc69f2eea1cb4887aa`. Из [235 проверенных HTML-страниц](../../.runtime/research/developer-harvest/domain-entry-audit-20261010/entry-evidence-manifest.json) выделены 402 ограниченные JSON-LD сущности, 17 JSON-LD ссылок на изображения и 61 страница с текстовым ценовым сигналом. Это признаки входных страниц, а не 402 проекта, 61 предложение или полное наличие каталога. Права на показ остаются неизвестными.

Воспроизведение после завершения текущего процесса, из корня проекта:

```bash
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_domain_audit --max-domains 0
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_domain_audit_parallel \
  --output .runtime/research/developer-harvest/domain-entry-audit-20261010 \
  --workers 4 --max-hosts 500 --delay-seconds 1
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_domain_extract \
  .runtime/research/developer-harvest/domain-entry-audit-20261010
```

Последняя команда работает **только с уже сохранёнными bytes**, повторно сверяет SHA и выделяет ограниченные JSON-LD типы/имена, явные `Offer.price`, URL изображений и текстовый признак цены в `entry-evidence.jsonl`. Обнаруженная цена остаётся утверждением страницы с неизвестными условиями и датой обновления; изображение остаётся ссылкой с неизвестными правами. Отсутствие сигнала в generic parser не доказывает отсутствие цены или картинки на сайте, особенно у SPA. Ссылки на страницы каталога — кандидаты для дальнейшей адресной проверки, а не доказательство выгруженного unit inventory.

На официальных входных страницах проверены разные уровни данных. Два ответа Донстроя HTTP 200 содержали шесть отдельных **проектных** карточек с подписью цены «от», диапазонами площади и этажности там, где они видны, и буквальными ссылками на изображения. [Частный выход Донстроя](../../.runtime/research/developer-harvest/supplemental-donstroy/observations.jsonl) отделяет карточки от навигации; DOM id не равен unit ID.

Эталон: два ответа входной страницы дали [семь проектно-категорийных записей](../../.runtime/research/developer-harvest/supplemental-etalon/projects.jsonl) с ценой квартир «от», минимальной площадью, промо и буквальными ссылками на `/flat/`. Затем проверены все семь ссылок и source-linked API-продолжения двух частичных страниц. [Финальный частный Etalon manifest](../../.runtime/research/developer-harvest/supplemental-etalon-api-final-20261010/manifest.json) сверяет `unit-results.jsonl` и `unit-results.csv`: **677 различных source unit ID**, ноль конфликтов ID; проект 124 завершён 28/28, проект 125 — 44/44, у обоих пагинация исчерпана. Среди 677 строк 674 имеют метку источника «Москва» без выявленного противоречия, две указывают Раменское и одна — Красногорск вопреки московской метке; эти три строки нельзя считать подтверждённо московскими. Цены и `isBooked` относятся к моменту захвата; текущая доступность и права на изображения не подтверждены.

[Отдельная карточная выборка AFI](../../.runtime/research/developer-harvest/domain-entry-audit-20261010/supplemental-afi/manifest.json) из официальной homepage имеет 11 ссылок `/flat/<id>` и три `/apartment/<id>` с видимыми ценой, площадью и скидкой. Статус жилой квартиры для `/apartment/`, корпус, этаж, адрес и актуальная доступность из этой страницы не доказаны.

Первый [SSR-образец Level](../../.runtime/research/developer-harvest/domain-entry-audit-20261010/supplemental-level/manifest.json) содержал восемь publisher unit ID; [source-linked API](../../.runtime/research/developer-harvest/domain-entry-audit-20261010/supplemental-level-discovery/endpoint-discovery.json) позволил затем пройти весь каталог. [Полный отдельный Level run](../../.runtime/research/developer-harvest/developer-harvest-22ba44b125df5339/manifest.json) завершился: 162 сохранённые страницы, 1 291 source result slots и 1 278 разных publisher unit ID, что совпадает с полями `count` и `count_without_alterplans`; оставшихся страниц и заблокированных хостов нет. 13 повторных слотов не прибавлены как новые квартиры. [Typed экспорт планов](../../.runtime/research/developer-harvest/developer-harvest-22ba44b125df5339/level_plan_variants.manifest.json) сохранил 3 047 URL и 11 unit ID с несколькими source versions; [Excel-совместимый экспорт](../../.runtime/research/developer-harvest/developer-harvest-22ba44b125df5339/supplemental-export/manifest.json) содержит `units.csv` и `plan_references.csv`. API-фильтр `city=moscow` сам по себе не подтверждает нахождение каждого корпуса в границах Москвы, поэтому `location_scope=unknown`. Эти частные supplemental записи не прибавлены к 75 721 provider-scoped unit ID основной поставки и не опубликованы на карте.

Проверка или возобновление отдельного Level run использует два сохранённых доказательных ответа и только следующий URL, выданный JSON источника:

```bash
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_harvest \
  --seed-file .runtime/research/developer-harvest/level/seeds.json run \
  --capture-manifest .runtime/research/developer-harvest/level/captures.json \
  --live --transport curl --max-pages 300 --delay-seconds 1
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_level_variants \
  .runtime/research/developer-harvest/developer-harvest-22ba44b125df5339
rtk proxy .runtime/dev-env/bin/python -m sources.property.developer_level_csv \
  .runtime/research/developer-harvest/developer-harvest-22ba44b125df5339
```

Повтор при текущем полном checkpoint не делает новых GET. При сетевом сбое возобновление не повторяет уже полученные страницы, а реальный отказ 401/403/429 блокирует хост. `status_modified` сохраняется как сырое поле издателя, но не выдаётся за дату обновления цены.

Продуктовое использование изображений и ценового каталога требует отдельной проверки прав и актуальности. Все raw, табличные и normalized результаты здесь private research only; тест входной страницы не равен разрешению на публичное копирование или производную модель фасада.
