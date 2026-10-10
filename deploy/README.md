# Развёртывание Novostroy на выделенной Spirit VM

**Статус на 11 октября 2026 года.** Прежняя пара Docker-образов была собрана
локально и прошла изолированный rootless Compose smoke с тогдашним
`current-only` выпуском данных. После этого активный каталог сменился на
проверенный `developer_buildings` (1 163 записи, 1 027 точек, 136 без точки).
Для него нужны **новый** проверенный выпуск данных и образы из соответствующего
нового source-коммита; прежний smoke не подтверждает их работу. Публикация
исходников в GitLab фиксируется отдельным receipt; наличие образов в Registry
само по себе её не подтверждает.
Оба образа загружены в Spirit Registry с существующим сервисным ключом под
промежуточным тегом `ef1d1992e729eb1aba33cb984d07846e7fd33404`:
удалённые digest проверены (`api` `sha256:572aaa53d2fe6081627deaaf0ae7777cd640e3813e2d1110c862f0f151be1214`,
`web` `sha256:b0bc4b967814af239763e56da725291debea41ffc188a422ab80ea2586f4d334`).
Этот старый тег **не** является образом нового developer-map кода.
CI jobs не запускались; для применения нужен один и тот же проверенный тег
полного source SHA у **обоих** образов. Три прежних запроса VM — 40 ГБ, 20 ГБ и
отдельная воссоздаваемая `ttt` на 20 ГБ — были отклонены квотой network SSD в
`ru-central1-d`; они удалены. Для нового выпуска используется другая,
уже работающая VM `hack-l2sh-command-2` с Ubuntu 24.04, 4 CPU и 8 ГБ RAM.
Рабочий облачный сайт ещё нужно подтвердить после фактического деплоя. Проверка
существующего сервисного аккаунта по точному tenant key вернула HTTP 200;
браузерный OAuth-вход обычного пользователя прошёл. Создание нового ключа
вернуло HTTP 403 как для сервисного аккаунта, так и для обычного пользователя;
новый ключ не создан, прежний не отозван. Это не подтверждает права CI.
Секреты в репозиторий не помещаются.

GitLab хранит **только исходники**. Реальные `data/` и `assets/` в образ не
попадают: их экспортируют отдельно из проверенного локального checkout и
монтируют на VM как неизменяемый выпуск. Синтетический bootstrap из корневого
README не является московским реестром и в этой Compose-конфигурации запрещён.
Текущая целевая схема — одна VM, два образа с тегом полного commit SHA, web
Compose на `127.0.0.1:8080` и FastAPI внутри Compose. Host Nginx может передать
публичный HTTPS на этот порт только после проверки TCP 80/443 и доверенного
сертификата для точного IP. Доступность приложения не расширяет автоматически
права на распространение исходных данных.

## Прямой выпуск на существующую VM без GitLab и Registry

Нужны проверенный source-only `branch1.bundle`, полный commit SHA и отдельный
приватный runtime transport archive с `release-manifest.json`. Bundle не
содержит `data/`, `assets/` или секреты. Два образа строятся **на VM** из
чистого checkout с этим SHA; GitLab PAT и Spirit service-account key не нужны.
Локальный рабочий сайт разработчика этот процесс не затрагивает.

Аутентифицированный Teleport/SSH должен привести на проверенные host
`hack-l2sh-command-2`, user `dk9267812737`. Сначала read-only preflight, затем
core bootstrap:

```bash
bash preflight_existing_vm.sh hack-l2sh-command-2 dk9267812737
bash bootstrap_existing_vm.sh hack-l2sh-command-2 dk9267812737 core
```

Bootstrap устанавливает штатные Ubuntu `docker.io`, `docker-compose-v2`,
`docker-buildx`, `git` и `python3-venv`, включает Docker и создаёт только
`/srv/novostroy/{releases,deploy-control}`. Пользователь не добавляется в
Docker-группу. Проверьте свободные байты и inode для одного распакованного
выпуска, сжатого transport archive, двух образов и запаса для отката. Ничьи
другие контейнеры или сайты не удаляются.

Передайте bundle и transport archive в домашний каталог по тому же доверенному
каналу, сверив SHA256 с локальными проверенными receipt. В командах ниже
`SOURCE_SHA` — **точный** 40-символьный commit SHA из source receipt:

```bash
SOURCE_SHA=<full-source-commit-sha>
sudo -n git clone --branch branch1 --single-branch \
  "$PWD/branch1.bundle" "/srv/novostroy/source/$SOURCE_SHA"
cd "/srv/novostroy/source/$SOURCE_SHA"
sudo -n python3 deploy/build_local_images.py check --source-sha "$SOURCE_SHA"
sudo -n python3 deploy/build_local_images.py build --source-sha "$SOURCE_SHA"
```

`check` требует clean `branch1`, HEAD равный SHA и проверяет все отслеживаемые
файлы source guard. `build` отмечает оба локальных образа тем же SHA и OCI
revision label. Существующий checkout не перезаписывайте; повторно сверьте
его или создайте отдельный каталог для нового SHA. Запуск через `sudo -n`
избавляет от доступа обычного пользователя к Docker socket; глобальный Git
config и credential store не меняются.

Проверенный `scripts/runtime_transport.py` извлекает archive в staging на
том же `/srv` filesystem, сверяет все SHA/пути и внутренний full-compat выпуск,
затем атомарно переименовывает новый direct child `releases/<bundle-id>`.
Вторая распакованная копия не создаётся. Импорт не активирует контейнеры;
evidence archive для этой команды не подходит. После установки проверьте,
что родительские каталоги и файлы читаемы API UID 10001. Постоянный
`data/runtime` — отдельный именованный Docker volume.

```bash
sudo -n python3 scripts/runtime_transport.py import \
  --archive "$HOME/<runtime-archive>.tar.zst" \
  --manifest "$HOME/<runtime-transport-manifest>.json" \
  --release-root /srv/novostroy/releases
sudo -n python3 deploy/apply_release.py apply \
  --release-dir /srv/novostroy/releases/<bundle-id> \
  --state-dir /srv/novostroy/deploy-control \
  --image-tag "$SOURCE_SHA" --local-images
```

`apply` сверяет выпуск, оба local image labels, Compose health и общий
map/menu/model snapshot через `127.0.0.1:8080`. На ошибке после переключения
пытается вернуть предыдущий успешный выпуск; первый неудачный деплой явно
остаётся неуспешным. Ручной откат:

```bash
sudo -n python3 deploy/apply_release.py rollback \
  --state-dir /srv/novostroy/deploy-control
```

До host Nginx сайт доступен лишь локально на VM. Установка edge-зависимостей
может идти параллельно сборке образов командой
`bash bootstrap_existing_vm.sh hack-l2sh-command-2 dk9267812737 edge`: она
устанавливает Nginx и отдельный `/opt/novostroy-certbot` с Certbot >=5.4, но
не активирует Novostroy site и не выдаёт сертификат. HTTP challenge,
production IP certificate, renewal и публичную проверку проводите только
после локального app health и проверки входящих TCP 80/443.

## CI/CD для исходников и заранее подготовленной VM

Нужны два существующих репозитория в `registry.spirit.culab.ru/hack-l2sh-2`:
`novostroy-api` и `novostroy-web`. Защитите `branch1` и задайте в GitLab
**protected File** variable `SPIRIT_DOCKER_SA_KEY` с environment scope
`registry-build` и действующим ключом
сервисного аккаунта Spirit, которому разрешён push в оба репозитория. Не
помещайте ключ в Git, обычную текстовую переменную, командную строку или
артефакты job. GitLab PAT для исходного репозитория не является ключом Registry.

Перед загрузкой нового кода `scripts/publish_gitlab.py prepare` создаёт
отдельный source-only Git-коммит, bundle и receipt в `.runtime/gitlab-publish/`.
Ему передают SHA последнего **проверенного** удалённого `branch1` через
`--base-remote-sha`; это основание для будущей трёхсторонней сверки, а не
утверждение, что удалённая ветка до сих пор на том же SHA. Локальный bundle
сохраняется даже при отсутствии GitLab-auth. Публикация выполняется отдельно
из пользовательского терминала командой `publish --token`: скрытый ввод PAT
попадает только в краткоживущий memory credential-cache. Перед обычным push
помощник считывает свежий remote tip, требует происхождения от записанной
базы, сохраняет посторонние удалённые файлы и останавливается при
пересекающемся изменении исходника. `push --force` не используется. Успех
устанавливается только по сверке удалённого SHA и `source-receipt.json` со
статусом `pushed`; локальный commit/bundle сами по себе не означают публикацию.

`.gitlab-ci.yml` запускает тесты для MR с целью `branch1` и для самой
`branch1`. `source_boundary` проверяет **фактически отслеживаемое Git-дерево**:
разрешённые пути, отсутствие raw/generated файлов, ссылок и признаков секретов,
а также исключения Docker-контекста. Он не печатает значения ключей. Отдельные
jobs без ключей выполняют source-only Python checks с отдельными таймаутами,
web unit tests и сборку Vite. Python suite использует синтетические фикстуры;
локальные CIAN observations пропускаются явно, если их файла нет. Тесты,
требующие московских `data/`/`assets/`, запущенный API или браузер, в CI не
маскируются под пройденные: это отдельная проверка реального выпуска.

После успешных тестов **только на защищённой `branch1`** два Kaniko jobs
собирают API и web с одним полным `$CI_COMMIT_SHA`. Только эти jobs видят
protected File `SPIRIT_DOCKER_SA_KEY`: они скачивают pinned `dp 14.1.0`,
проверяют SHA256, настраивают Kaniko и удаляют временную конфигурацию. Образы
не являются артефактами GitLab с реальными данными. Оба build jobs должны
успешно завершиться до появления ручного `deploy_existing_vm`.

Для CD заранее нужен **существующий** dedicated protected runner на той самой
VM с тегом `novostroy-trusted-deploy`, закреплённый за этим проектом и с
`run_untagged=false`. Обычные/MR test jobs не должны исполняться на VM с
Docker/Registry credentials. Нужны Python 3.12, Docker Compose, настроенный доступ к
Registry, проверенным выпуском в `/srv/novostroy/releases/<release-id>` и
приватной директорией `/srv/novostroy/deploy-control` режима `0700`.
Защитите GitLab environment `production`; задайте protected переменные
`NOVOSTROY_DEPLOY_TARGET` (точное короткое hostname runner) и
`NOVOSTROY_RELEASE_DIR` (абсолютный путь к **прямому потомку** каталога
releases). Job сверяет host/ref/full SHA/path, сериализован
`resource_group: novostroy-production` и вызывает существующий
`deploy/apply_release.py apply`. Он проверяет хеши выпуска, запускает обе
SHA-версии образов, healthchecks и API smoke; при ошибке после переключения
пытается вернуть последний успешный выпуск. Ручной откат остаётся командой
ниже. CI не создаёт VM, не загружает данные на неё и не запускает deploy сам.
В GitLab включите **Prevent outdated deployment jobs** для production, но
проверьте выбранный полный SHA перед ручным запуском: `resource_group` лишь
сериализует jobs, а настройка GitLab не является независимой проверкой
актуальности source SHA при любом порядке запуска старых manual pipelines.
Первый GitLab pipeline, доступ runner к Registry и целевая VM пока не
подтверждены реальным запуском.

## Выпуск данных вне GitLab

На машине с подтверждённым исходным импортом сначала просмотрите сухой план
нового `full-compat` выпуска:

```bash
rtk proxy .runtime/dev-env/bin/python scripts/package_deploy_data.py --profile full-compat
```

`full-compat` сохраняет старые versioned URL для открытых вкладок и кэша.
Обновление только кода с тем же release-dir допустимо без нового экспорта.
Экспорт создаётся только по явному `--apply` в новой директории вне рабочего
`data/`:

```bash
rtk proxy .runtime/dev-env/bin/python scripts/package_deploy_data.py \
  --profile full-compat --apply --output /private/releases/<release-id>
```

Передавайте выпуск только приватным каналом, сверяя `release-manifest.json` и
каждый файл. Он содержит внутренний реестр, социальные данные, атрибуцию и
один атомарный указатель `developer_experience/active.json`, который закрепляет
каталог зданий, меню секций/этажей/квартир и выбранные модели с исходными
SHA-256. Публичные API продолжают проверять права и уровень доказательств;
наличие медиа в приватном выпуске не означает разрешения на показ. У выпуска
есть `data/`, `assets/` и манифест с SHA-256 каждого файла. Для обновления
создавайте новый ID и не удаляйте прежний выпуск.
Папка `data/runtime` в пакете пустая; SQLite/журналы хранятся в отдельном
постоянном Docker volume `novostroy_runtime` (никогда не запускайте `down -v`).

Проверьте копию той же командой с `--verify --output <путь-к-выпуску>`;
VM-скрипт применения повторит сверку до изменения контейнеров. Локальный
`full-compat` выпуск от 11 октября прошёл обе проверки: 56 297 файлов,
12 395 403 862 байта, SHA-256 `release-manifest.json`
`f5c2d814b627ff36c6f1fb3689d52f57de91d5a91719a7fd59e08eca7eec39bd`.
Это проверка данных на исходной машине, не установка на VM. Прежний
`current-only` выпуск с 17 542 файлами и ~1,97 ГБ относился к старому
каталогу. Для каждого следующего выпуска берите размер из его манифеста.

## Частный архив данных и свидетельств

`scripts/agent_data_bundle.py` формирует внешний архив вне Git. У него два
назначения: runtime-поддерево включает только прошедший проверку `full-compat`
выпуск, который можно импортировать на VM; evidence-поддерево сохраняет
локальные каталоги и единицы, геокодировочные ledgers, FR map capture,
исходные медиа, модели/указатели и активные с историческими `assets/`. Архив
не публикуется и не монтируется в приложение. Повторяющееся содержимое
дедуплицируется по SHA256 в ограниченных по размеру `tar.zst`-частях; coverage
index показывает логические и уникальные объёмы, исключения и категории. Из
каждого источника сохраняются исходные манифесты и даты; exporter не меняет
provenance или неизвестный статус прав. Заголовки ответов (могут содержать
cookies), ключи, секретные файлы, process state и rebuildable caches в архив
не попадают.

Сначала можно построить hash-only inventory; он атомарно появляется в новой
versioned-папке и не копирует payload:

```bash
rtk proxy .runtime/dev-env/bin/python scripts/agent_data_bundle.py inventory
```

Перед замораживанием выберите конкретный **проверенный** `full-compat` выпуск.
Не запускайте эти команды параллельно с созданием моделей или медиа: сначала
зафиксируйте active pointers и дождитесь, чтобы данные перестали меняться.
Далее создайте новый runtime release и объединённый частный bundle с новым ID;
старые каталоги экспорта не перезаписываются:

```bash
rtk proxy .runtime/dev-env/bin/python scripts/package_deploy_data.py \
  --profile full-compat --apply --output /private/releases/<release-id>
rtk proxy .runtime/dev-env/bin/python scripts/agent_data_bundle.py pack \
  --release-dir /private/releases/<release-id> \
  --bundle-id <unique-bundle-id>
rtk proxy .runtime/dev-env/bin/python scripts/agent_data_bundle.py verify \
  --bundle-dir .runtime/exports/<unique-bundle-id>
```

`verify` сверяет SHA outer manifest/coverage/chunks и каждого content blob,
точный список файлов, безопасные пути и лимиты decompression. Перед импортом
скопируйте каталог bundle целиком по доверенному приватному каналу. На VM
понадобятся `python3` и системная утилита `zstd`. Импортёр проверит весь
evidence archive, извлечёт только `deployment/` во
временный staging, повторно вызовет `deploy.apply_release.verify_release`, затем
атомарно поставит неизменяемый прямой дочерний каталог в `releases/`. Он не
меняет активный указатель и не запускает контейнеры:

```bash
python3 scripts/agent_data_bundle.py verify \
  --bundle-dir /path/to/<unique-bundle-id>
python3 scripts/agent_data_bundle.py import \
  --bundle-dir /path/to/<unique-bundle-id> \
  --release-root /srv/novostroy/releases
python3 deploy/apply_release.py apply \
  --release-dir /srv/novostroy/releases/<unique-bundle-id> \
  --state-dir /srv/novostroy/deploy-control \
  --image-tag <full-40-character-source-commit-sha>
```

Для повторной проверки установленного data release используйте ту же bundle
команду `verify`, а затем runtime verifier:

```bash
python3 -c 'from deploy.apply_release import verify_release; from pathlib import Path; print(verify_release(Path("/srv/novostroy/releases/<unique-bundle-id>"))["file_count"])'
```

Evidence-only snapshot создаётся тем же `pack --bundle-id <new-id>` без
`--release-dir`; его можно проверить, но импортёр намеренно откажет ему в
runtime-установке. Существующий начальный snapshot сохраняется как
`.runtime/exports/snapshot-20261010T205655Z`; он покрывает тогдашнее состояние,
а не заменяет финальную frozen delivery. Отдельный roundtrip отчёт должен
фиксировать IDs, outer manifest SHA, `verify` результат, установленный path,
inner release manifest SHA и результат runtime verifier.

## VM: применение и откат

Нужны Ubuntu 22/24, Docker Engine с Compose v2 (`up --wait`), доступ к
Registry через приватный `dp auth service-acc` + `dp auth configure-docker`,
права на чтение выпуска. API работает как UID 10001; отдельный именованный
Docker volume монтируется для `/app/data/runtime`, поэтому права записи не
зависят от UID/GID host bind mount. Startup preflight API проверяет реальную
возможность записи перед healthcheck.
Для альтернативного Registry/CI-пути на любой заранее проверенной VM сначала
перенесите `deploy/preflight_existing_vm.sh` доверенным SSH/Teleport-каналом
и передайте **фактические** короткое имя хоста и пользователя. Скрипт читает ОС, ресурсы,
место и inode, версии Docker/Compose, существующие контейнеры и занятые порты;
он не устанавливает и не останавливает службы. Несовпадение имени/пользователя
останавливает проверку. Сверьте свободное место с **новым** манифестом выпуска,
двумя образами, их распакованным размером и запасом под откат. Не считайте
номинальный размер диска свободным местом. Если порт 8080 занят, используйте
другой свободный порт через `--http-port`; чужие службы не перенастраивайте.
Создайте также отдельную `deploy-control` директорию владельца запуска с
правами `0700`: она **не** монтируется в API и хранит только состояние отката.
Для Registry-пути используйте полный source SHA, для которого **оба** образа
уже опубликованы и их удалённые digest проверены. Промежуточный тег выше пригоден только
для соответствующей проверенной пары образов; после публикации другого source
commit ему потребуется отдельный тег и сверка. Сначала подтвердите, что
Teleport HTTP Access указывает на выбранный VM-порт и не заменяет чужое
приложение; по умолчанию Compose слушает только `127.0.0.1:8080`.

```bash
python3 deploy/apply_release.py apply \
  --release-dir /srv/novostroy/releases/<release-id> \
  --state-dir /srv/novostroy/deploy-control \
  --image-tag <full-40-character-source-commit-sha>
```

Скрипт сверяет хеши и состав выпуска, проверяет Compose, скачивает оба
SHA-образа **до** замены контейнеров, ждёт healthchecks, а затем через тот же
Nginx сверяет один активный developer-map snapshot/run между каталогом и
картой, закреплённые тем же указателем меню квартир и выбранные модели,
а также активный run прежних оценок моделей. Отдельный локальный Compose smoke ниже
проверяет также реальную возможность построить Nearby-маршрут.
Он пишет только не секретный `deploy-state.json` в приватную `deploy-control`.
При неудаче после начала обновления он пытается вернуть предыдущие образы и выпуск;
данные и старые образы не удаляет. При первом неудачном деплое предыдущего
состояния нет — ошибка остаётся явной. Если уже есть Compose-контейнеры без
записанного состояния, первый `apply` откажется их заменять. Ручной откат:

```bash
python3 deploy/apply_release.py rollback --state-dir /srv/novostroy/deploy-control
```

Nginx оставляет `/v1/` и **весь** `/assets/` за API с его приватностью,
ETag/Range и allowlist, а `/bundled/`, `/cesium/` и SPA `/models` отдаёт из
веб-образа. API работает в `real`/`estateline` с нулём выдуманных квартир:
отсутствие текущего offer feed не превращает demo-предложения в реальные.
Маршруты Nearby требуют включённый в выпуск пешеходный граф и дополнительные
данные; быстрый healthcheck не является браузерным, iPhone или FPS-тестом.

## Локальная проверка образов до Registry и VM

Для локально собранных `novostroy-api:smoke` и `novostroy-web:smoke` есть
`deploy/smoke_local.py`. Он проверяет SHA-инвентарь явно выбранного настоящего
выпуска, запускает отдельный Compose-проект `novostroy-smoke` только на
`127.0.0.1:18080` без Registry pull и проверяет через Nginx реестр, карту,
модели, Nearby, приватные/публичные ассеты и `/models`. Параметры:

```bash
rtk proxy python deploy/smoke_local.py \
  --docker-bin /absolute/path/to/docker \
  --docker-host unix:///absolute/path/to/rootless/docker.sock \
  --docker-config /absolute/path/to/private/docker-config \
  --release-dir /absolute/path/to/checked/developer-release
```

Без `--keep` он остановит только собственный Compose-проект, сохранив volume;
`--keep` оставляет контейнеры для ручной проверки. Прежний smoke **прошёл**
10 октября 2026 года для старых образов и старого набора 1473 записей/
766 точек. Это историческое доказательство механики Compose, а не проверка
нынешнего каталога зданий, новых образов, внешнего импорта, CI, VM, iPhone,
частоты кадров или полной производительности 3D. Новый smoke сверяет
фактические counts из одного snapshot и требует хотя бы один построенный
пешеходный маршрут среди ограниченного набора реальных контрольных точек.
