"""Create a local DB role password, database, and libpq service without exposing secrets."""

from __future__ import annotations

import getpass
import os
import secrets
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = PROJECT_ROOT / ".runtime"
ROLE = getpass.getuser()
DATABASE = "novostroy"
PORT = 55439
SOCKET_DIR = RUNTIME / "postgis-socket"
BIN_DIR = RUNTIME / "postgis-env" / "bin"
PASSFILE = RUNTIME / "postgis.pgpass"
SERVICE_FILE = RUNTIME / "pg_service.conf"


def _psql(query: str, *, database: str = "postgres") -> str:
    result = subprocess.run(
        [
            str(BIN_DIR / "psql"),
            "-X",
            "-qAt",
            "-h",
            str(SOCKET_DIR),
            "-p",
            str(PORT),
            "-U",
            ROLE,
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input=query,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_new_private_file(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


def main() -> int:
    if not (RUNTIME / "postgis-data" / "PG_VERSION").is_file():
        raise SystemExit("No initialized project-local PostgreSQL cluster found; refusing to continue.")
    if PASSFILE.exists() or SERVICE_FILE.exists():
        raise SystemExit("Service or password file already exists; refusing to overwrite it.")

    role_is_superuser = _psql(
        "SELECT COALESCE((SELECT rolsuper FROM pg_roles WHERE rolname = current_user), false);"
    )
    if role_is_superuser != "t":
        raise SystemExit("The peer-authenticated cluster owner is not a superuser; refusing to continue.")
    database_exists = _psql(
        "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'novostroy');"
    )
    if database_exists != "f":
        raise SystemExit("Database novostroy already exists; refusing to alter or reuse it.")

    subprocess.run(
        [
            str(BIN_DIR / "createdb"),
            "-h",
            str(SOCKET_DIR),
            "-p",
            str(PORT),
            "-U",
            ROLE,
            DATABASE,
        ],
        check=True,
    )
    password = secrets.token_urlsafe(36)
    _psql(f"ALTER ROLE \"{ROLE}\" WITH PASSWORD '{password}';")

    _write_new_private_file(
        PASSFILE,
        f"127.0.0.1:{PORT}:{DATABASE}:{ROLE}:{password}\n",
    )
    _write_new_private_file(
        SERVICE_FILE,
        "[novostroy]\n"
        "host=127.0.0.1\n"
        f"port={PORT}\n"
        f"dbname={DATABASE}\n"
        f"user={ROLE}\n"
        "connect_timeout=3\n"
        f"passfile={PASSFILE}\n",
    )
    print("Local database service and private password file created; no credential was printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
