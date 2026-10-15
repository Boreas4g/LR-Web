#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$WORKSPACE/scripts/start-robot-runtime.sh"
SERVICE_FILE="/etc/systemd/system/lr-robot-runtime.service"
ENV_DIR="/etc/lr-robot"
ENV_FILE="$ENV_DIR/runtime.env"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 sudo 执行此安装脚本。" >&2
  exit 1
fi

if [ ! -x "$RUNNER" ]; then
  echo "缺少可执行启动脚本：$RUNNER" >&2
  exit 1
fi

install -d -o root -g boreas -m 0750 "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  cat >"$ENV_FILE" <<EOF
# Optional production overrides. Leave both parameter paths unset for the
# workspace's local single-machine configuration.
# LR_GATEWAY_PARAMS_FILE=/etc/lr-robot/edge-gateway.yaml
# LR_AUTO_LOCALIZATION_PARAMS_FILE=/etc/lr-robot/auto-localization.yaml
LR_ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
EOF
  chown root:boreas "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
fi

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=LR 机器人导航与 Edge Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=boreas
WorkingDirectory=$WORKSPACE
EnvironmentFile=-$ENV_FILE
ExecStart=$RUNNER
Restart=on-failure
RestartSec=10
# Terminate the launcher first so its cleanup trap can safely stop ROS child
# processes.  systemd force-cleans any unresponsive child after this timeout.
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now lr-robot-runtime.service
echo "已安装并启动 lr-robot-runtime.service。运行配置：$ENV_FILE"
