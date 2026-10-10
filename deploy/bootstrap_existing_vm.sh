#!/usr/bin/env bash
# Install only the packages needed by a checked Novostroy release on one VM.
# Docker is used through sudo; no user is added to the docker group.
set -euo pipefail

if [[ $# -ne 3 || ! $1 =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]{0,62}$ || ! $2 =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  printf 'Usage: bash bootstrap_existing_vm.sh <verified-hostname> <verified-user> core|edge\n' >&2
  exit 2
fi

expected_host=$1
expected_user=$2
phase=$3
if [[ $phase != core && $phase != edge ]]; then
  printf 'Unknown bootstrap phase.\n' >&2
  exit 2
fi
if [[ $(hostname -s) != "$expected_host" || $(id -un) != "$expected_user" ]]; then
  printf 'Host or user differs from the reviewed VM; no changes made.\n' >&2
  exit 3
fi
if [[ $(uname -m) != x86_64 ]] || ! grep -Eq '^ID=ubuntu$' /etc/os-release \
    || ! grep -Eq '^VERSION_ID="?24\.04"?$' /etc/os-release; then
  printf 'Expected Ubuntu 24.04 x86_64; no changes made.\n' >&2
  exit 3
fi
if ! sudo -n true >/dev/null 2>&1; then
  printf 'Noninteractive sudo is required; no changes made.\n' >&2
  exit 3
fi

if [[ $phase == core ]]; then
  sudo -n apt-get update -qq
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker.io docker-compose-v2 docker-buildx git python3-venv
  sudo -n systemctl enable --now docker
  sudo -n docker version --format '{{.Server.Version}}' >/dev/null
  sudo -n docker compose version --short
  sudo -n docker buildx version >/dev/null
  sudo -n install -d -m 0755 /srv/novostroy /srv/novostroy/releases /srv/novostroy/source
  sudo -n install -d -m 0700 /srv/novostroy/deploy-control
  printf 'Core bootstrap ready: Docker and Compose respond; /srv/novostroy is dedicated.\n'
  exit 0
fi

# Run only after application health. This phase does not activate a proxy or
# request a certificate; the separate edge script owns its site and renewal.
sudo -n apt-get update -qq
if ! command -v nginx >/dev/null 2>&1; then
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
  sudo -n systemctl stop nginx
fi
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
if [[ ! -e /opt/novostroy-certbot ]]; then
  sudo -n python3 -m venv /opt/novostroy-certbot
fi
if [[ ! -x /opt/novostroy-certbot/bin/python ]]; then
  printf 'Certbot venv is incomplete; stop before changing the edge.\n' >&2
  exit 4
fi
sudo -n /opt/novostroy-certbot/bin/python -m pip install 'certbot>=5.4,<6'
sudo -n /opt/novostroy-certbot/bin/python -c \
  'from importlib.metadata import version; import re; m=re.match(r"^(\d+)\.(\d+)", version("certbot")); assert m and tuple(map(int,m.groups())) >= (5, 4)'
sudo -n nginx -t
printf 'Edge dependencies ready; no Novostroy Nginx site or certificate activated.\n'
