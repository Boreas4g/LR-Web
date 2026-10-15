#!/usr/bin/env bash
set -euo pipefail

# Repair the two stateful transitions encountered when an existing local
# Compose deployment is promoted to the production appliance deployment.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose/docker-compose.yml"
# shellcheck source=load-platform-env.sh
source "$ROOT_DIR/scripts/load-platform-env.sh"

die() {
  echo "错误：$*" >&2
  exit 1
}

require_file() {
  [[ -r "$1" ]] || die "缺少或无法读取：$1"
}

emqx_runtime_gid() {
  local gid
  gid="$(docker run --rm --entrypoint sh "${LR_EMQX_IMAGE:-emqx/emqx:5}" -c 'id -g')"
  [[ "$gid" =~ ^[0-9]+$ ]] || die "无法识别 EMQX 运行组 ID"
  printf '%s' "$gid"
}

prepare_emqx_files() {
  local emqx_gid cert_dir users_file
  cert_dir="$LR_MQTT_CERTS_DIR"
  users_file="$LR_EMQX_BOOTSTRAP_USERS_FILE"
  require_file "$cert_dir/ca.crt"
  require_file "$cert_dir/server.crt"
  require_file "$cert_dir/server.key"
  require_file "$cert_dir/control-api.crt"
  require_file "$cert_dir/control-api.key"
  require_file "$users_file"
  emqx_gid="$(emqx_runtime_gid)"

  # EMQX runs as an unprivileged container UID. It receives only traverse
  # access to the certificate directory, read access to its server key and
  # read access to its bootstrap credentials. The Control API key stays root-only.
  chown root:"$emqx_gid" "$cert_dir"
  chmod 0710 "$cert_dir"
  chown root:root "$cert_dir/ca.crt" "$cert_dir/server.crt" "$cert_dir/control-api.crt"
  chmod 0644 "$cert_dir/ca.crt" "$cert_dir/server.crt" "$cert_dir/control-api.crt"
  chown root:"$emqx_gid" "$cert_dir/server.key"
  chmod 0640 "$cert_dir/server.key"
  chown root:root "$cert_dir/control-api.key"
  chmod 0600 "$cert_dir/control-api.key"
  chown root:"$emqx_gid" "$users_file"
  chmod 0640 "$users_file"
  echo "已准备 EMQX mTLS 运行时文件权限。"
}

migrate_existing_postgres_password() {
  local postgres_container
  postgres_container="$(docker compose -f "$COMPOSE_FILE" ps -q postgres 2>/dev/null)"
  [[ -n "$postgres_container" ]] || {
    echo "未发现现有 PostgreSQL 容器；首次启动将使用当前配置初始化数据库。"
    return
  }
  [[ "$(docker inspect -f '{{.State.Running}}' "$postgres_container")" == true ]] || {
    echo "现有 PostgreSQL 容器未运行；首次启动后会使用当前配置。"
    return
  }

  local attempt
  for attempt in $(seq 1 30); do
    if docker exec -u postgres "$postgres_container" pg_isready -q -U library_robot; then
      docker exec -i -u postgres -e LR_NEW_DB_PASSWORD="$LR_POSTGRES_PASSWORD" "$postgres_container" \
        sh -ceu 'psql -v ON_ERROR_STOP=1 -U library_robot -d postgres --set=password="$LR_NEW_DB_PASSWORD"' <<'SQL'
ALTER ROLE library_robot WITH PASSWORD :'password';
SQL
      echo "已将现有 PostgreSQL 账号密码迁移到当前控制站私有配置。"
      return
    fi
    sleep 1
  done
  die "现有 PostgreSQL 容器 30 秒内未就绪，未修改任何数据库密码。"
}

[[ "$(id -u)" -eq 0 ]] || die "请使用 sudo 执行此脚本。"
command -v docker >/dev/null 2>&1 || die "未检测到 Docker。"
docker info >/dev/null 2>&1 || die "Docker 服务未运行。"
load_lr_platform_environment
[[ "$LR_PLATFORM_MODE" == production ]] || die "此修复仅适用于 production 控制站。"
[[ -n "${LR_MQTT_CERTS_DIR:-}" && -n "${LR_EMQX_BOOTSTRAP_USERS_FILE:-}" ]] || \
  die "缺少控制站 PKI 配置，请先执行 export-station /etc/lr-platform。"
[[ -n "${LR_POSTGRES_PASSWORD:-}" && "$LR_POSTGRES_PASSWORD" != replace-* ]] || \
  die "缺少有效的 LR_POSTGRES_PASSWORD。"

prepare_emqx_files
migrate_existing_postgres_password
