#!/usr/bin/env bash
# Read-only inventory before placing Novostroy on a verified Spirit VM.
# Run on the VM through an authenticated SSH/Teleport session.
set -u

if [[ $# -ne 2 || ! $1 =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]{0,62}$ || ! $2 =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  printf 'Usage: bash preflight_existing_vm.sh <verified-short-hostname> <verified-user>\n' >&2
  exit 2
fi

actual_host=$(hostname -s 2>/dev/null || true)
actual_user=$(id -un 2>/dev/null || true)
printf 'host=%s user=%s\n' "$actual_host" "$actual_user"
if [[ $actual_host != "$1" || $actual_user != "$2" ]]; then
  printf 'Target identity mismatch; no further host inspection was performed.\n' >&2
  exit 3
fi

printf 'os=' 
if [[ -r /etc/os-release ]]; then
  sed -n 's/^PRETTY_NAME=//p' /etc/os-release | head -n 1
else
  printf 'unknown\n'
fi
printf 'arch=%s cpu_cores=%s\n' "$(uname -m)" "$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf unknown)"
printf 'memory_kib=' 
awk '/^MemTotal:/ { print $2; found=1; exit } END { if (!found) print "unknown" }' /proc/meminfo

target_dir=/
if [[ -d /srv/novostroy ]]; then
  target_dir=/srv/novostroy
elif [[ -d /srv ]]; then
  target_dir=/srv
fi
printf 'filesystem_for_release=%s\n' "$target_dir"
df -B1 -P "$target_dir" 2>/dev/null | awk 'NR==2 { printf "disk_bytes_total=%s disk_bytes_available=%s disk_used_percent=%s\n", $2, $4, $5 }'
df -i -P "$target_dir" 2>/dev/null | awk 'NR==2 { printf "inodes_total=%s inodes_available=%s inodes_used_percent=%s\n", $2, $4, $5 }'

for path in /srv /srv/novostroy /srv/novostroy/releases /srv/novostroy/deploy-control; do
  if [[ -e $path ]]; then
    printf 'path=%s type=%s owner_mode=' "$path" "$(stat -c %F "$path" 2>/dev/null || printf unknown)"
    stat -c '%U:%G %a' "$path" 2>/dev/null || printf 'unknown\n'
  else
    printf 'path=%s absent\n' "$path"
  fi
done

for tool in docker git python3 rsync curl tar zstd; do
  if command -v "$tool" >/dev/null 2>&1; then
    version=$(timeout 5 "$tool" --version 2>/dev/null | head -n 1 || true)
    printf 'tool=%s version=%s\n' "$tool" "${version:-unknown}"
  else
    printf 'tool=%s missing\n' "$tool"
  fi
done

if command -v docker >/dev/null 2>&1; then
  compose_version=$(timeout 5 docker compose version --short 2>/dev/null | head -n 1 || true)
  printf 'docker_compose=%s\n' "${compose_version:-unavailable}"
  docker_root=$(timeout 5 docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
  if [[ $docker_root == /* && -d $docker_root ]]; then
    printf 'docker_root=%s\n' "$docker_root"
    df -B1 -P "$docker_root" 2>/dev/null | awk 'NR==2 { printf "docker_disk_bytes_available=%s\n", $4 }'
  fi
  printf 'docker_containers_name_status_ports:\n'
  if ! timeout 8 docker ps -a --format '{{.Names}} | {{.Status}} | {{.Ports}}' 2>/dev/null; then
    printf 'docker_ps=unavailable_to_current_user\n'
  fi
fi

printf 'tcp_listeners_local_address:\n'
if command -v ss >/dev/null 2>&1; then
  timeout 5 ss -ltnH 2>/dev/null | awk 'NR <= 120 { print $4 } END { if (NR > 120) print "truncated_after_120" }'
else
  printf 'ss=missing\n'
fi

if command -v sudo >/dev/null 2>&1; then
  if timeout 5 sudo -n true >/dev/null 2>&1; then
    printf 'sudo_noninteractive=available\n'
  else
    printf 'sudo_noninteractive=unavailable\n'
  fi
else
  printf 'sudo=missing\n'
fi

printf 'read_only_preflight_complete=true\n'
