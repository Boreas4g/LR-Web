#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=load-platform-env.sh
source "$ROOT_DIR/scripts/load-platform-env.sh"
load_lr_platform_environment
COMPOSE_FILES=(-f "$ROOT_DIR/infra/docker-compose/docker-compose.yml")
if [ "$LR_PLATFORM_MODE" = production ]; then
  COMPOSE_FILES+=(-f "$ROOT_DIR/infra/docker-compose.production.yml")
fi
docker compose "${COMPOSE_FILES[@]}" down

# Compose 只余基础设施；宿主 control-api 是平台应用层，随平台一并停止。
if systemctl cat lr-control-api.service >/dev/null 2>&1; then
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl stop lr-control-api.service
  else
    sudo -n systemctl stop lr-control-api.service
  fi
fi
