#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=load-platform-env.sh
source "$ROOT_DIR/scripts/load-platform-env.sh"
load_lr_platform_environment
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose/docker-compose.yml"
COMPOSE_FILES=(-f "$COMPOSE_FILE")
if [ "$LR_PLATFORM_MODE" = production ]; then
  COMPOSE_FILES+=(-f "$ROOT_DIR/infra/docker-compose.production.yml")
fi

if ! docker compose "${COMPOSE_FILES[@]}" build; then
  echo "平台镜像构建失败；现有平台服务保持运行。请检查网络后重新执行更新。" >&2
  exit 1
fi
# Application updates must not be discarded merely because Docker Hub is
# unreachable.  Broker upgrades are a separate, explicitly requested action:
# they can require a reviewed image and a maintenance window of their own.
if [ "${LR_PLATFORM_PULL_BROKER:-false}" = true ]; then
  docker compose "${COMPOSE_FILES[@]}" pull emqx
fi
docker compose "${COMPOSE_FILES[@]}" up -d --pull never
echo "平台已更新并重启。"
