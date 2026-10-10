# Ново — поиск новостроек на 3D-карте

Развёртывание на Spirit через SHA-образы и отдельный проверенный выпуск данных:
[deploy/README.md](deploy/README.md). Локальный `scripts/dev.py` не является
production-деплоем.

> **Source-only checkout.** Публикуемый GitLab-снимок содержит код, контракты,
> тесты, документацию и синтетические fixtures. Локальные `data/`, `assets/`,
> `.runtime/`, секреты, импорт EstateLine, фактические CIAN review-записи,
> GeoParquet/PBF и готовые GLB в него не входят. Поэтому приведённые ниже
> ссылки на локальные манифесты и числа реального импорта описывают проверенный
> рабочий checkout, а не содержимое чистого клона. Чистый клон запускается в
> отдельном **синтетическом demo** режиме; действующий московский каталог,
> меню квартир, модели и городские слои появятся только после отдельного разрешённого
> получения данных и сборки. Исходный код сам по себе их не восстанавливает.

Для чистого клона с Python 3.12+/Node/npm/`rtk` и `uv` из корня проекта:

```bash
rtk uv venv --python python3.13 .runtime/dev-env
rtk uv pip install --python .runtime/dev-env/bin/python -r requirements-dev.txt
rtk npm --prefix web ci
rtk proxy .runtime/dev-env/bin/python scripts/bootstrap_source_demo.py
rtk python scripts/dev.py start --preview
```

`bootstrap_source_demo.py` предназначен только для свежего клона и откажется
перезаписывать уже существующий `assets/manifest.json`. Он создаёт два явно
синтетических контура и демонстрационный каталог; никакие реальные московские
данные или модели не подгружает. Для остановки используйте
`rtk python scripts/dev.py stop`. Браузерный запуск требует локальных TCP-сокетов.

**Текущий проверенный рабочий снимок (11 октября 2026 года).** Главный веб-вид
работает в `catalog_view=developer_buildings`: 1 163 записи о корпусах, из них
1 027 с подтверждённой точкой и 136 без точки. Прежние 756 отдельных
OSM-кандидатов не входят в основную карту. Один атомарный указатель
`data/property/developer_experience/active.json` связывает карту с меню 45 466
наблюдаемых квартир, 12 982 строк этажей, 62 553 строк планов и выбранными
856 схематическими 3D-моделями. Файлы планировок подготовлены для четырёх
записей; число строк планов не означает столько скачанных изображений. Цена и
доступность показываются только с указанным основанием и датой; живой поток
предложений отсутствует. Эти модели не являются подтверждёнными проектными
фасадами. Фильтры квартир по цене, цене за м², комнатам, площади и этажу
действуют одинаково в списке, на карте и внутри меню дома; неизвестные
значения не заполняются догадкой. Модель выбранного дома загружается по
запросу. Приватный перенос данных, SHA-проверки и запуск описаны в
[инструкции по выпуску](deploy/README.md).

Клиент соединяет React/TypeScript/CesiumJS и FastAPI; исходный проект
Unity/Cesium for Unity для Android/iOS хранится отдельно. Вкладка «Данные»
открывает текущий каталог, а имена поставщиков по умолчанию скрыты в
публичном UI; обязательная атрибуция картографических слоёв видима. Веб-карту
можно открыть в браузере iPhone через локальную сеть при доступных серверах,
но реальный iPhone и FPS текущей версии не проверены. Нативная iOS-сборка
требует macOS, Unity с модулем iOS, Xcode и подписи.

Отдельный каталог `demo` содержит 100 вымышленных ЖК и 1000 иллюстративных предложений. Фасады демо-ЖК процедурные. Реальные контуры зданий из Overture/OSM дают только объёмную подложку; они не подтверждают материалы фасадов, входы или планировки. На снимке 2026-10-09 [городской манифест](assets/citywide/manifest.json) фиксирует 564 031 объём из выбранной области, 3396 детальных и 212 региональных тайлов. Активный внутренний [citywide-v12](assets/citywide-v12/manifest.json) строит обзорные объёмы по отдельным упрощённым контурам зданий без сплошных ячеек, повторно использует 3396 детальных GLB v8 и согласует lit-материал обзорного уровня с деталями. [Консервативная оценка высоты](data/city/overture_moscow_render_height_imputation_v1_1.manifest.json) применяется только к рендеру; сохранены **800** проверенных ступенчатых OSM/Overture частей у **166** зданий. Это ограниченный пример, а не полный набор секций города: исходный атрибут высоты есть у 6595 родительских зданий, 117 132 используют 3 м на этаж, 59 получают оценку по локально схожему контуру, остальные 440 245 — отображаемую высоту 15 м. Ещё 36 контуров получили геометрический fallback. Parts не добавляются к счётчику 564 031 зданий и не раскрывают этажи/квартиры. Уровень земли принят приблизительно по эллипсоиду WGS84. Отдельные потоковые слои [дорог](assets/roads-v2/manifest.json), [парков](assets/parks-v1/manifest.json) и [воды](assets/water-v1/manifest.json) отрисовывают открытые картографические данные с атрибуцией; ширины дорог и положение над землёй условны. Публично выдаются только очищенные GLB без идентификаторов источников; внутренние манифесты и индексы остаются на диске для аудита. Это не доказательство полного правового или картографического охвата Москвы. Проверяйте числа реестра в `/v1/registry/buildings`, состояние API в `/v1/health` и активный набор тайлов в локальном `assets/manifest.json`. Цель следующего этапа — лицензированные модели минимум трёх ЖК и разрешённый поток актуальных цен; эти источники пока не получены. `real` режим отделён от демо и не использует вымышленные цены.

## Исторический внутренний реестр EstateLine

Следующие сведения описывают прежний отдельный реестр и оценки OSM-кандидатов;
они не являются счётчиками текущей главной карты `developer_buildings`.

Локальный пользовательский набор `/home/pirojban/Загрузки/1_xls_estateline` обрабатывается в [снимок реестра](data/property/estateline/source_manifest.json). Отдельные `projects.jsonl` и `review_required.jsonl` сохраняют прошедшие проверку записи и спорные совпадения. Зафиксированы 1473 московские проектные записи: 1347 многоквартирных, 114 апартаментных и 12 малоэтажных; ещё 59 спорных записей отправлены на review. Только 10 адресов имеют подтверждённую точную OSM-привязку, остальные 1463 остаются без координат. Снимок датирован 2026-09-06; SHA-256 `projects.jsonl` — `6edc6b68b8b29dbb892d1080dbe8e616db5462876aa2c8e152925080aa606e3e`. Даты, этажность и статусы отражают исторические отчёты, а не актуальную продажу квартир. Права набора — `user_supplied_internal_use`; открытая лицензия и разрешение на публичное перераспространение не заявляются. Публичный `registry_id` не раскрывает внутренние ID/URL источника.

При наличии манифеста, прошедшего проверку SHA-256, числа записей и прав, API выбирает `catalog_view=building_registry`. Веб-клиент использует `/v1/registry/buildings` для списка, `/v1/registry/map` для слоёв и `/v1/map/points` для маркеров; фильтры списка и карты общие. Демо-каталог остаётся отдельным fixture и не заполняет поля реестра. Для явного запуска старого демонстрационного вида задайте `NOVOSTROY_REGISTRY_MODE=off` перед стартом. Чтобы пересобрать реестр из разрешённой локальной папки, включая строгую адресную привязку:

Для основной карты добавлен компактный `/v1/map/points`: он отдаёт **10 подтверждённых точек реестра и 756 отдельных OSM-кандидатов** на 765 координатах, независимо от 48 записей текущей страницы списка. Одно точное совпадение координат остаётся двумя выбираемыми записями без доказанной связи между ними. Запрос ищет имена обеих групп; фильтры `status` и `kind` относятся только к реестру и явно исключают OSM-записи с отдельным счётчиком. Клик по OSM-точке открывает лёгкую карточку точки; переход к оценке модели — отдельная ссылка, без автоматической загрузки индивидуального 3D. Это 766 исходных записей, **не 766 уникальных ЖК** и не подтверждение сегодняшней стадии стройки. Права реестра остаются `user_supplied_internal_use` в уже разрешённом локальном режиме; право открытого перераспространения из нового маршрута не следует. Подробности и проверки — в [отчёте по точкам](reports/point-map.md).

Точки сохраняют доступный адрес и прямую метку ЖК с отдельным основанием поля; OSM-метка не связывает объект с реестром по близости или сходству имени. Для выбранной точки `/v1/map/nearby` выбирает максимум по пять нанесённых станций метро, образовательных и медицинских объектов, затем пытается построить пешие маршруты локальным графом. Слои POI на карте по умолчанию скрыты. Выбор идёт по расстоянию по прямой, а не по кратчайшему пешему пути; медицинская пятёрка при необходимости включает ближайшую из **шести подтверждённых на 2026 год** площадок ОМС вместо пятой обычной кандидатуры, с явной пометкой и фактическим расстоянием. Точки на контурах не являются проверенными входами, неуспешный маршрут не получает выдуманного времени. [Контракт](contracts/IMPLEMENTATION-1.4.md) и [проверка](reports/nearby-amenities.md) фиксируют ограничения. Снимок OSM даёт 7 126 записей по четырём категориям (276 метро, 2 121 школа, 2 881 детский сад, 1 848 клиника) после удаления дублей внутри каждой категории; полнота городского охвата и актуальность работы учреждений не подтверждены.

```bash
rtk proxy .runtime/geodata-env/bin/python -m sources.property.estateline --source-dir '/home/pirojban/Загрузки/1_xls_estateline' --output-dir data/property/estateline --address-index data/city/local_address_index
```

## Проверка кандидатов моделей

[Активный offline-run](data/models/models-v1-92dc659b5e75fa52/manifest.json) оценил все **2317 входных записей**: 1473 принятых и 59 спорных EstateLine, 756 отдельных OSM-контуров строящихся жилых зданий и 29 неподходящих событий Минстроя. Это не счётчик уникальных ЖК. Результат: **756** пригодных к показу контуров, **60** на review, **29** отклонённых модельных кандидатур и **1472** без индивидуальной геометрии. Из 756 OSM-контуров 273 получили явно помеченный схематический объём (32 по нанесённой высоте, 241 по этажности × 3 м), 483 остались плоскими. Эти OSM-модели не привязаны автоматически к записям ЖК и не являются проектными фасадами или подтверждением текущей стадии стройки. Для каждой записи сохранён приватный журнал проверок; публичный экран показывает понятную причину и безопасные результаты проверок без исходных ID/URL.

После применения нового backend через управляемый запуск откройте `http://localhost:5173/models?decision=rejected` (или тот же путь на LAN-адресе компьютера). Экран загружает оценки постранично из `/v1/models/candidates`; выбор готового контура открывает Polygon/MultiPolygon с внутренними дворами, а тяжёлый схематический объём допускается только при подтверждённой render-height и свободном бюджете кадров. До перезапуска API новый URL может показать «Проверка моделей пока недоступна»; это не ноль найденных кандидатов. Чтобы повторить полный проход по неизменным исходным снимкам:

```bash
rtk proxy .runtime/dev-env/bin/python -m sources.models.build
rtk proxy .runtime/dev-env/bin/python -m sources.models.audit data/models/models-v1-92dc659b5e75fa52
```

Повтор `build` валидирует неизменяемый run и использует content-addressed `data/models/cache/geometry`; отдельного incremental-флага нет. Получение лицензированных IFC/GLB или масштабных планов, точных фасадов и актуальной стадии котлована этим импортом пока не реализовано. Числа, права, тесты и ограничения — в [отчёте](reports/model-reconstruction.md).

Постоянный плоский слой [public-ground-v3](assets/public-ground-v3/manifest.json) построен из тех же 564 031 исходных контуров Overture/OSM, что и объёмы: сначала единый дальний обзор, затем 212 региональных векторных тайлов (упрощение до 2 м), затем 3396 точных ячеек без упрощения. В ячейках сохранены 995 внутренних колец и 702 MultiPolygon; поверхность расположена на +0,45 м над эллипсоидом WGS84. Дальний обзор имеет 46,748 м/пиксель и опускает 504 501 субпиксельный контур, но при приближении доступны исходные векторные очертания. Дороги, парки и вода остаются отдельными постоянными картографическими слоями. Объёмы [public-citywide-v10](assets/public-citywide-v10/manifest.json) загружаются после точного видимого плоского контура и здоровой ограниченной пробы кадров только при высоте камеры ≤12 км; скрываются при ≥16 км без очистки кэша. Региональный обзор — отдельные footprint-призмы с упрощением до 2 м вместо сплошных ячеек v8; 3396 точных детальных GLB побайтно сохранены. Web начинает с coarse SSE 128, а после новых здоровых окон допускает SSE 64 и 24; при просадке понижает детализацию или приостанавливает 3D. Для ground включён skip-LOD без соседей, чтобы общий дальний raster не вызывал загрузку всех 212 регионов; плоский слой остаётся видимым при смене LOD объёмов. Внешние региональные JSON сохраняют ленивую загрузку metadata.

[Независимый offline-аудит](integration/audit_ground_city.py) подтвердил структуру, ссылки, приватность, +0,45 м для вершин точных ячеек и положительную высоту дальнего растра между вершинами (минимум +0,169 м по центрам и серединам рёбер треугольников). [Выборочная сверка WKB](integration/audit_ground_source_samples.py) подтвердила отверстие и MultiPolygon по двум ячейкам. Цвет активного ground-v3 после linear→sRGB совпадает с обзорным PNG **#AAAFB2**; все 3608 бинарных частей векторных GLB побайтно совпадают с v2. [Аудит v9](integration/audit_footprint_city.py) проверил 212 регионов, высоты, приватность и отсутствие сплошных ячеек; [аудит v10](integration/audit_city_lighting.py) сверил все индексированные треугольники с v9, нормали, lit-материал и неизменность 3396 деталей. [Аудиты roads-v2](integration/audit_road_seams.py) и [bounds](integration/audit_road_bounds.py) проверили устранение 12 измеренных стыков и границы всех 3683 дорожных GLB. [Предпросмотр](.runtime/ground-preview/ground-map.png) показывает плоские GLB с картографией в offline top-down рендере; это не скриншот приложения. Скорость браузера и FPS после поздних исправлений не измерялись; Chromium здесь завершился до страницы (`sandbox_host_linux.cc` / `EPERM`), локальный TCP запрещён. Очищенные citywide-v4–v9 и roads-v1 сохранены для открытых сессий.

## Локальный запуск

Нужны Python 3.12+, Node/npm и `rtk`. Из корня проекта:

```bash
rtk uv venv --python python3.13 .runtime/dev-env
rtk uv pip install --python .runtime/dev-env/bin/python -r requirements-dev.txt
rtk npm --prefix web ci
rtk proxy .runtime/dev-env/bin/python -m pipeline.generate_demo
rtk python scripts/dev.py start --lan --preview
```

Откройте `http://localhost:5173` на компьютере или `http://<LAN-IP-компьютера>:5173` на iPhone в той же сети. `--preview` показывает собранный production bundle; скрипт пересобирает его, если исходники новее. Vite слушает `0.0.0.0:5173` и проксирует `/v1` и `/assets` на API. `--lan` открывает API на `0.0.0.0:8000` для прямого подключения Unity-приложения; без этого флага API слушает только `127.0.0.1:8000`, а мобильный клиент может использовать прокси по `http://<LAN-IP>:5173`. Для разработки с Vite/HMR запускайте `rtk python scripts/dev.py start` вместо preview. Адрес компьютера может измениться, не используйте зашитый пример IP. Локальный HTTP предназначен для разработки.

При отсутствии 3D Tiles `start` вызывает сборщик один раз. Существующие тайлы он не перестраивает: это сохраняет результат большого городского импорта. Перед запуском скрипт проверяет доступ к локальному TCP; если среда запрещает сокеты, он завершится понятной ошибкой без создания процессов. После старта он сверяет `/v1/health`, режим каталога, активный `/v1/map` или `/v1/registry/map`, URL ground/city с локальным манифестом и несколько внешних region JSON через веб-прокси. Корневой tileset сам по себе не доказывает, что дочерние тайлы доступны; внутренний `/assets/manifest.json` по HTTP закрыт. `rtk python scripts/dev.py verify` выполняет ту же проверку у уже запущенной пары без изменений процессов. `rtk python scripts/dev.py status` показывает процессы, запущенные этим скриптом. `rtk python scripts/dev.py restart-api` перечитывает backend-код без остановки управляемого веб-preview и повторяет проверку. `rtk python scripts/dev.py stop` останавливает только процессы этого скрипта. Логи находятся в `.runtime/api.log` и `.runtime/web.log`.

Для уже восстановленной управляемой пары после публикации нового backend запустите из обычного терминала того же компьютера и checkout одну команду:

```bash
cd /home/pirojban/novostroy
rtk python scripts/dev.py restart-api
```

Команда сама сверяет активный набор city/ground, опубликованный run моделей и новый маршрут точек `/v1/map/points` через веб-прокси. Затем обновите основную карту; экран отклонённых модельных кандидатур — `/models?decision=rejected`. Следующий сценарий нужен только если снова обнаружен прежний запуск **без managed state**: старый демо-каталог 100/1000 и ошибка дочерних тайлов.

```bash
cd /home/pirojban/novostroy
rtk python scripts/dev.py recover --lan --preview
```

`recover` предназначен для прежней пары, запущенной агентом без managed state: он сопоставляет слушателей 8000/5173 с точным `cwd`, командой Uvicorn/Vite, портом и PID в `/proc`, затем останавливает только подтверждённые процессы и запускает управляемую пару. При чужом слушателе или недоступном PID он завершится **без сигналов** с объяснением; `stop` не следует применять к чужому процессу. В доступной host-среде прежняя пара уже успешно восстановлена этой командой. Из ограниченной среды Codex новое применение к работающему host API не выполнялось: локальные сокеты здесь запрещены (`EPERM`).

Крупная городская сборка требует локального GeoParquet, указанного в [манифесте источника](data/city/overture_moscow_cityclipped_2026-09-23.1.manifest.json), и около 1.3 ГБ под базовые тайлы. Без этого файла обычный демо-запуск создаёт ограниченную подложку по локальной выборке. Исходный GeoParquet и GLB не включаются в репозиторий. После получения всех зафиксированных исходных снимков слои воспроизводятся последовательно:

```bash
rtk .runtime/dev-env/bin/python -m pipeline.build_citywide
rtk .runtime/dev-env/bin/python -m pipeline.recolor_citywide
rtk .runtime/dev-env/bin/python -m pipeline.build_roads
rtk .runtime/dev-env/bin/python -m pipeline.build_roads --revision 2 --output assets/roads-v2
rtk .runtime/dev-env/bin/python -m pipeline.build_surfaces parks
rtk .runtime/dev-env/bin/python -m pipeline.build_surfaces water
rtk .runtime/dev-env/bin/python -m pipeline.apply_render_heights
rtk .runtime/dev-env/bin/python -m pipeline.apply_building_parts --base assets/citywide-v3 --output assets/citywide-v5 --sample data/city/osm_overture_clean_multilevel_parts_full_261009.geojson --sample-manifest data/city/osm_overture_clean_multilevel_parts_full_261009.manifest.json
rtk .runtime/dev-env/bin/python -m pipeline.build_progressive_city --base assets/citywide-v5 --output assets/citywide-v6
rtk .runtime/dev-env/bin/python -m pipeline.build_lazy_city --base assets/citywide-v6 --output assets/citywide-v7
rtk .runtime/dev-env/bin/python -m pipeline.build_ground --base-city assets/citywide-v5 --output assets/ground-v2
rtk .runtime/dev-env/bin/python -m pipeline.build_ground_color_revision --base assets/ground-v2 --output assets/ground-v3
rtk .runtime/dev-env/bin/python -m pipeline.build_ground_first_city --base assets/citywide-v7 --output assets/citywide-v8
rtk .runtime/dev-env/bin/python -m pipeline.build_footprint_city --base assets/citywide-v8 --output assets/citywide-v9
rtk .runtime/dev-env/bin/python -m pipeline.build_city_material_revision --base assets/citywide-v9 --output assets/citywide-v10
rtk .runtime/dev-env/bin/python -m pipeline.publish_public_assets
```

Сборщики проверяют закреплённые манифесты, публикуют результат после завершения и повторно используют готовые артефакты. `apply_render_heights` принимает только [утверждённый overlay](data/city/overture_moscow_render_height_imputation_v1_1.manifest.json), не изменяет исходный GeoParquet и не выдаёт оценку за наблюдённую высоту. `apply_building_parts` требует [строгий allowlist](data/city/osm_overture_clean_multilevel_parts_full_261009.manifest.json): точный source ID и версия, родитель, высота, ≥99% покрытия и отсутствие пересечения 3D-объёмов в выбранной группе. Исходно имеются 29 714 частей у 6263 родителей, но отрисованы лишь прошедшие проверку 800/166; прочие source-записи сохранены. Для выбранных exact-version OSM частей `height` — верх от уровня земли, `min_height` — нижний Z, а `roof:height` уже включён в общую высоту. Это [проверенное правило источника](docs/briefs/city-height-parts.md), не общее правило для любого поставщика. `build_progressive_city` добавляет плоский первый уровень и повторно использует v5 volume/detail без изменения. `build_lazy_city` выносит metadata объёмов и деталей в 212 внешних региональных JSON; `build_ground` создаёт отдельный плоский floor, `build_ground_color_revision` исправляет linear/sRGB glTF-материал, `build_ground_first_city` удаляет силуэты v7, `build_footprint_city` заменяет сплошные coarse-ячейки v8 индивидуальными призмами v9, а `build_city_material_revision` приводит lit-материал и нормали v10 к деталям без изменения геометрии треугольников. Roads-v2 вычисляет общие граничные точки в WGS84/ECEF и расширяет bounds на ширину ленты. [Публикация](pipeline/publish_public_assets.py) создаёт очищенные тайлы; при работающем managed API примените `rtk python scripts/dev.py restart-api`: команда сама проверяет публичный стек. Актуальный указатель — в локальном `assets/manifest.json`; повтор старого `build_citywide` не должен откатывать v11. Картографические слои требуют отдельного закреплённого [OSM/Geofabrik снимка](docs/briefs/social.md) и сохраняют его обязательную атрибуцию.

## Сбор и обновление данных

`pipeline.collection` проверяет источники по явному списку задач, создаёт неизменяемые snapshot-артефакты, SHA-256, контрольные точки и отчёты покрытия в `data/runtime/collection/`. Повтор с тем же входом становится `no_op`; ошибка не заменяет опубликованный снимок. Без флагов команда выполняет только сухую проверку локальных файлов, без сети:

```bash
rtk proxy .runtime/dev-env/bin/python -m pipeline.collection
rtk proxy .runtime/dev-env/bin/python -m pipeline.collection --apply --offline
```

Отдельный [список разрешённых публичных HTTP-задач](pipeline/collection/jobs.public.json) содержит только официальные открытые реестры Минстроя. Для ручного сетевого обновления требуется явный `--allow-network`; скачивание ограничено HTTPS-хостом, размером и проверенной ссылкой, использует ETag/Last-Modified и не следует редиректам:

```bash
rtk proxy .runtime/dev-env/bin/python -m pipeline.collection --config pipeline/collection/jobs.public.json --apply --allow-network
```

Шаблоны расписания находятся в `pipeline/collection/systemd/`, но таймеры не включены. В двух полученных реестрах жилые события, выделенные строгим классификатором, относятся к Сочи; московских жилых записей нет. Поля цены, квартиры и текущего наличия в этих наборах отсутствуют, поэтому они не попадают в каталог предложений. Разрешённый feed застройщика требует отдельного подтверждения прав и остаётся выключенным. Документация источников — в [property matrix](docs/criteria/property.json) и [source manifest](data/property/minstroy/source_manifest.json).

## Проверки

```bash
rtk proxy .runtime/dev-env/bin/python -m unittest discover -s recommendations -p 'test_*.py'
rtk proxy .runtime/dev-env/bin/python contracts/build_registry.py --check
rtk proxy .runtime/dev-env/bin/python integration/run_acceptance.py
rtk proxy .runtime/dev-env/bin/python -m pytest -q backend/tests integration/test_shared_eligibility.py integration/test_criteria_producer.py integration/test_dev_startup.py
rtk proxy .runtime/dev-env/bin/python integration/check_offline_routes.py
rtk proxy .runtime/dev-env/bin/python integration/audit_ground_city.py --require-active
rtk proxy .runtime/dev-env/bin/python integration/audit_ground_color.py --require-active
rtk proxy .runtime/dev-env/bin/python integration/audit_road_bounds.py --require-active
rtk proxy .runtime/dev-env/bin/python integration/audit_road_seams.py --require-active
rtk proxy .runtime/dev-env/bin/python integration/audit_city_lighting.py --require-active
rtk proxy .runtime/geodata-env/bin/python integration/audit_ground_source_samples.py
rtk proxy .runtime/dev-env/bin/python integration/check_live.py http://127.0.0.1:8000
rtk npm --prefix web run build
rtk npm --prefix web run test:unit
rtk npm --prefix web run test:e2e
rtk dotnet run --project mobile/Tests/Novostroy.Domain.Tests.csproj
```

Браузерные E2E и `integration/check_live.py` требуют работающих API и Vite. `check_offline_routes.py` проверяет прямые ответы backend, JSON Schema, разделение реестра и предложений без сокета; `audit_ground_city.py` проверяет геометрический baseline ground-v2/city-v8; `audit_ground_color.py` проверяет активный ground-v3; `audit_footprint_city.py` проверяет baseline city-v9; `audit_city_lighting.py` проверяет active v10 exact triangle geometry, нормали, материалы, детали и privacy; дорожные аудиты проверяют границы и стыки roads-v2. `audit_ground_source_samples.py` сравнивает два GLB baseline с исходными WKB. Они не проверяют браузерный кэш или FPS. `integration/run_acceptance.py` исполняет все 100 случаев: 80 проверяют ответы поиска/рекомендаций, 20 — адаптер OSM и покрытие. Live-проверки API и Unity описаны в `integration/` и [mobile/README.md](mobile/README.md). Текущий sandbox запрещает локальный TCP (`socket EPERM`); здесь прошли офлайн-проверки и сборка, а новый live/browser прогон после последних исправлений остаётся невыполненным. Отчёт — в `reports/validation.md`.

## Структура и контракт

- `contracts/` — исходный v1 и текущий аддитивный v1.3 OpenAPI/JSON Schema, реестр 55 критериев, примеры, правила фильтрации и покрытия данных. [Архитектурная карта](docs/architecture.md) связывает модули и границы источников.
- `backend/` — FastAPI; `recommendations/` — независимый движок строгого отбора и рекомендаций.
- `web/` — веб-каталог и CesiumJS; `mobile/UnityProject/` — исходники нативного рендера Unity/Cesium.
- `pipeline/` — генератор демо и сборщик 3D Tiles; `sources/` и `data/` — адаптеры, манифесты и локальные исходные данные.
- `quality/` и `integration/` — независимые сценарии и проверки; `reports/` — измеренные результаты и открытые ограничения.

Поиск сначала применяет включительные бюджет/площадь и остальные жёсткие фильтры, затем ограничивает число строк. Дополнительные условия задаются как `filters.predicates` по машинному реестру `/v1/criteria`; неизвестные, просроченные, оценочные и неполно покрытые факты не проходят строгий фильтр. Цена за м² сравнивается точной дробью от полной запрашиваемой цены и исходной площади, без подмены условной ценой. `count` — полное число подходящих предложений до `limit`. Неоднозначный русский запрос возвращает `clarification`, а не расширенный поиск. В `real` режиме цена старше 24 часов от текущего UTC не выводится как актуальное предложение. Явно недоступное предложение исключается из всех активных маршрутов; неизвестная доступность обозначается как неподтверждённая. Backend сравнения хранит `offer_id` отдельно от `unit_id`, но экран сравнения сейчас скрыт. Локальный пешеходный граф прошёл семь пробных маршрутов между точками доступа POI, но маршруты от входов ЖК и полное покрытие не проверены; поэтому индекс инфраструктуры не вычисляется.

Для целевого PostGIS описана схема в `pipeline/postgis_schema.sql`. Локальный fixture backend позволяет проверить интерфейс без PostgreSQL, но эта проверка сама по себе не подтверждает работу PostGIS. Подробности источников, прав и границ находятся в `docs/briefs/` и манифестах рядом с данными. Запуск реального режима без разрешённых прайсов оставляет предложения пустыми.

Для CIAN добавлен ограниченный **локальный review-парсер**: он принимает уже наблюдённые факты либо отдельно разрешённый сохранённый HTML, хранит индивидуальную цену в RUB отдельно от проектного «от», рынка переуступки и пер‑м², а изображения лишь классифицирует по явно связанным URL/подписям без скачивания. Текущий [пилот](reports/cian-review.md) содержит 2 частных датированных наблюдения, 1 пропущенную страницу ЖК, 0 автоматических запросов к CIAN, 0 опубликованных квартир и 0 медиафайлов. Разрешённого API/экспорта CIAN сейчас нет; существующий публичный каталог и web-интерфейс этим импортом не меняются. Повторный локальный импорт идемпотентен: `rtk python -m sources.property.cian import-observations data/property/cian_public_sample_20261010.json`.
