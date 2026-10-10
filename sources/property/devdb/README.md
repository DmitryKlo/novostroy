# Isolated local PostgreSQL + PostGIS

This optional local development database is installed entirely inside ignored
`.runtime/`: PostgreSQL 17.11 and PostGIS 3.6.0, a fresh cluster, one database
named `novostroy`, a local password file, and a libpq service definition. The
server binds only to `127.0.0.1:55439`; Unix sockets live under
`.runtime/postgis-socket/`. No system packages, sudo, existing database, or
project secrets are involved. The generated password is stored only in
`.runtime/postgis.pgpass` with mode `0600`; it is not part of the manifest or
commands below.

## Recreate on a clean checkout

Run from the project root. These commands assume `rtk` and `/opt/anaconda/bin/conda`
are available. Do not run `initdb` against an existing cluster. If
`.runtime/postgis-data/` already exists, inspect it and reuse only after
confirming it is this project's cluster; this setup guide never removes or
reinitializes data.

```bash
rtk mkdir -p .runtime/conda-pkgs
rtk proxy env CONDA_PKGS_DIRS="$PWD/.runtime/conda-pkgs" /opt/anaconda/bin/conda create --yes --prefix "$PWD/.runtime/postgis-env" --override-channels --channel conda-forge postgresql=17.11 postgis=3.6.0
rtk mkdir -p .runtime/postgis-data .runtime/postgis-socket
rtk proxy .runtime/postgis-env/bin/initdb -D "$PWD/.runtime/postgis-data" --username="$(rtk id -un)" --auth-local=peer --auth-host=scram-sha-256 --no-instructions
rtk proxy .runtime/postgis-env/bin/pg_ctl -D "$PWD/.runtime/postgis-data" -l "$PWD/.runtime/postgis-server.log" -o "-c listen_addresses=127.0.0.1 -c port=55439 -c unix_socket_directories=$PWD/.runtime/postgis-socket" start
```

The current runtime's database role is `pirojban`. For another OS account,
use its name consistently as the `initdb --username`, `createdb -U`, and role
name. Set a random role password through local peer-authenticated `psql` stdin
and write it only to a mode-`0600` `.runtime/postgis.pgpass`; do not put it in a
shell argument, environment, terminal transcript, or tracked file. Then create
database `novostroy` through the Unix socket and use the following service file
(which has no password):

```ini
[novostroy]
host=127.0.0.1
port=55439
dbname=novostroy
user=pirojban
connect_timeout=3
passfile=/home/pirojban/novostroy/.runtime/postgis.pgpass
```

The cluster is started with an explicit loopback-only `listen_addresses`; do
not expose this port on a LAN. Verify the listener before use:

```bash
rtk ss -ltn
rtk proxy .runtime/postgis-env/bin/pg_isready -h 127.0.0.1 -p 55439
```

## Extension and spatial smoke test

`CREATE EXTENSION` is done as the local cluster owner. The SQL uses only a
temporary table and a clearly synthetic coordinate; it leaves no rows behind.

```bash
rtk proxy env PGSERVICEFILE="$PWD/.runtime/pg_service.conf" .runtime/postgis-env/bin/psql 'service=novostroy' -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION postgis;'
rtk proxy env PGSERVICEFILE="$PWD/.runtime/pg_service.conf" .runtime/postgis-env/bin/psql 'service=novostroy' -v ON_ERROR_STOP=1 -f sources/property/devdb/smoke.sql
```

The successful run must show `PostGIS_Full_Version()`, SRID `4326`, the
synthetic point, a passing bounding-box assertion, and a positive transformed
EPSG:3857 x coordinate. `PGSERVICEFILE` and the service name are the backend
connection mechanism; the password remains in the ignored passfile.

## Evidence and permissions

Install command, tested versions, local endpoint, and smoke-test commands are
recorded in [`manifest.json`](manifest.json). Primary references:

- [PostgreSQL `initdb`](https://www.postgresql.org/docs/current/app-initdb.html):
  initialize a new cluster as its owner, not as root.
- [PostgreSQL connection settings](https://www.postgresql.org/docs/current/runtime-config-connection.html):
  `listen_addresses` and port are server-start settings.
- [PostgreSQL service files](https://www.postgresql.org/docs/current/libpq-pgservice.html)
  and [libpq environment variables](https://www.postgresql.org/docs/current/libpq-envars.html):
  `PGSERVICEFILE` and `PGPASSFILE` are supported; avoid `PGPASSWORD`.
- [PostGIS getting started](https://postgis.net/documentation/getting_started/):
  install/enable the extension with `CREATE EXTENSION postgis`.

This local runtime is disposable development infrastructure. It is not a shared
service or a production backup. `.runtime/` is gitignored.
