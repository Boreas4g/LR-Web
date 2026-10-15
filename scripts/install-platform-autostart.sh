#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=load-platform-env.sh
source "$ROOT_DIR/scripts/load-platform-env.sh"
SERVICE_FILE="/etc/systemd/system/lr-platform.service"
ENV_DIR="/etc/lr-platform"
ENV_FILE="$ENV_DIR/platform.env"
PLATFORM_MODE="${LR_PLATFORM_MODE:-local}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 sudo 执行此安装脚本。" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 Docker。请先安装 Docker Engine 与 Docker Compose。" >&2
  exit 1
fi

install -d -o root -g root -m 0750 "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  cat >"$ENV_FILE" <<EOF
# 由部署人员维护；production 模式需要补充证书目录、强密码和 CORS 来源。
LR_PLATFORM_MODE=$PLATFORM_MODE
EOF
  chmod 0640 "$ENV_FILE"
fi

# Use the same private configuration for the initial image build and the
# eventual systemd service. PKI export values are loaded as a second private
# environment file so certificate rotation never requires editing this script.
load_lr_platform_environment
PLATFORM_MODE="$LR_PLATFORM_MODE"
case "$PLATFORM_MODE" in
  local|production) ;;
  *)
    echo "LR_PLATFORM_MODE 只能是 local 或 production。" >&2
    exit 1
    ;;
esac

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=LR 图书馆机器人管理平台
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT_DIR
EnvironmentFile=-$ENV_FILE
ExecStart=$ROOT_DIR/scripts/start-platform.sh
ExecStop=$ROOT_DIR/scripts/stop-platform.sh
RemainAfterExit=yes
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
if [ "$PLATFORM_MODE" = production ]; then
  docker compose -f "$ROOT_DIR/infra/docker-compose/docker-compose.yml" -f "$ROOT_DIR/infra/docker-compose.production.yml" build
else
  docker compose -f "$ROOT_DIR/infra/docker-compose/docker-compose.yml" build
fi
systemctl enable --now lr-platform.service
echo "已安装并启动 lr-platform.service。运行配置：$ENV_FILE"
