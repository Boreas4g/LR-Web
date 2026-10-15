#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose/docker-compose.yml"
# shellcheck source=load-platform-env.sh
source "$ROOT_DIR/scripts/load-platform-env.sh"
load_lr_platform_environment
PLATFORM_MODE="$LR_PLATFORM_MODE"
COMPOSE_FILES=(-f "$COMPOSE_FILE")

case "$PLATFORM_MODE" in
  local)
    ;;
  production)
    COMPOSE_FILES+=(-f "$ROOT_DIR/infra/docker-compose.production.yml")
    ;;
  *)
    echo "LR_PLATFORM_MODE 只能是 local 或 production。" >&2
    exit 1
    ;;
esac

# The production BookArm service owns /run/lr-bookarm through systemd's
# RuntimeDirectory.  Before that service is installed (or while hardware is
# deliberately left offline), the Web platform must still be able to start:
# mount a project-local empty directory so BookArm requests return Nginx 502
# instead of preventing maps, dispatch and all other station functions.
#
# An explicit LR_BOOKARM_SOCKET_DIR always wins; this permits an administrator
# to use a non-default managed runtime directory without a hidden fallback.
if [[ -n "${LR_BOOKARM_SOCKET_DIR:-}" ]]; then
  BOOKARM_SOCKET_DIR="$LR_BOOKARM_SOCKET_DIR"
elif [[ -d /run/lr-bookarm && -r /run/lr-bookarm && -x /run/lr-bookarm ]]; then
  BOOKARM_SOCKET_DIR=/run/lr-bookarm
else
  BOOKARM_SOCKET_DIR="$ROOT_DIR/data/runtime/lr-bookarm"
  echo "BookArm 受管 socket 目录尚不可用；平台将以离线代理模式启动：$BOOKARM_SOCKET_DIR"
fi
if ! install -d -m 0755 "$BOOKARM_SOCKET_DIR" 2>/dev/null; then
  echo "无法创建 BookArm socket 目录：$BOOKARM_SOCKET_DIR" >&2
  echo "请由管理员创建该目录，或设置 LR_BOOKARM_SOCKET_DIR 为可访问目录。" >&2
  exit 1
fi
export LR_BOOKARM_SOCKET_DIR="$BOOKARM_SOCKET_DIR"

compose() {
  docker compose "${COMPOSE_FILES[@]}" "$@"
}

if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 Docker。请先安装 Docker Engine 与 Docker Compose。" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker 服务未运行，或当前用户没有 Docker 使用权限。" >&2
  exit 1
fi

# A control station must come back after a network outage.  Daily/systemd
# startup therefore reconciles only locally available images and never turns a
# registry timeout into an outage of the whole platform.  The explicit update
# workflow builds and deploys new images in a maintenance window; developers
# can opt into the old behaviour with LR_PLATFORM_BUILD_ON_START=true.
if [ "${LR_PLATFORM_BUILD_ON_START:-false}" = true ]; then
  compose up -d --build
else
  compose up -d --no-build --pull never
fi

# 平台应用层已移出 Docker：Compose 只负责基础设施（postgres/redis/minio/emqx），
# API 由宿主 lr-control-api.service 提供。启动它（幂等）并以其 /health 作为就绪判据。
if systemctl cat lr-control-api.service >/dev/null 2>&1; then
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl start lr-control-api.service
  else
    sudo -n systemctl start lr-control-api.service
  fi
else
  echo "未安装 lr-control-api.service；宿主 control-api 未由 systemd 管理。" >&2
  exit 1
fi

echo "正在等待平台服务就绪..."
# A first start can include PostgreSQL schema initialization and an MQTT/mTLS
# connection retry. Give the service enough time before systemd declares the
# one-shot bootstrap failed.
for _ in $(seq 1 60); do
  if curl --fail --silent --max-time 2 "http://127.0.0.1:8080/health" >/dev/null; then
    HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "平台已启动："
    echo "  本机访问：http://localhost:8000（serve-static → 127.0.0.1:8080）"
    if [ -n "${HOST_IP:-}" ]; then
      echo "  局域网访问：http://${HOST_IP}:8000"
    fi
    if [ -n "${LR_STATION_URL:-}" ]; then
      echo "  操作员地址：$LR_STATION_URL"
    fi
    exit 0
  fi
  sleep 2
done

echo "宿主 control-api 尚未就绪，请执行以下命令查看日志：" >&2
echo "journalctl -u lr-control-api.service -n 100" >&2
exit 1
