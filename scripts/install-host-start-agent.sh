#!/usr/bin/env bash
# Install the fixed-command host bridge used by the dashboard start button.

set -euo pipefail

[[ "$(id -u)" -eq 0 ]] || {
  echo "请使用 sudo 执行此安装脚本。" >&2
  exit 1
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_SOURCE="$ROOT_DIR/scripts/lr-host-start-agent.py"
AGENT_TARGET="/usr/local/libexec/lr-host-start-agent.py"
START_SOURCE="$ROOT_DIR/scripts/start-all.sh"
START_TARGET="/usr/local/libexec/lr-platform-start-all.sh"
RESTART_SOURCE="$ROOT_DIR/scripts/restart-all.sh"
RESTART_TARGET="/usr/local/libexec/lr-platform-restart-all.sh"
SERVICE_PATH="/etc/systemd/system/lr-host-start-agent.service"
BOOKARM_ALLOW_START="${LR_BOOKARM_ALLOW_START:-false}"
SOCKET_DIR="/var/lib/lr-platform/host-start-agent"
SOCKET_PATH="$SOCKET_DIR/start-all.sock"

[[ -r "$AGENT_SOURCE" && -r "$START_SOURCE" && -r "$RESTART_SOURCE" ]] || {
  echo "找不到宿主启动代理、start-all 或 restart-all 脚本。" >&2
  exit 1
}

install -d -o root -g root -m 0755 /usr/local/libexec
install -d -o root -g root -m 0755 "$SOCKET_DIR"
install -o root -g root -m 0755 "$AGENT_SOURCE" "$AGENT_TARGET"
install -o root -g root -m 0755 "$START_SOURCE" "$START_TARGET"
install -o root -g root -m 0755 "$RESTART_SOURCE" "$RESTART_TARGET"

cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=LR 固定命令宿主启动代理
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
Environment=LR_WEB_ROOT=$ROOT_DIR
Environment=LR_BOOKARM_ROOT=$ROOT_DIR/../../lingzu_arm/lingzu-bookarm-py/bookarm
Environment=LR_BOOKARM_ALLOW_START=$BOOKARM_ALLOW_START
ExecStart=/usr/bin/python3 $AGENT_TARGET --socket $SOCKET_PATH --script $START_TARGET --restart-script $RESTART_TARGET
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=read-only
ProtectSystem=strict
ReadWritePaths=$SOCKET_DIR

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lr-host-start-agent.service
# enable --now 不会替换已在运行的旧进程；显式 restart 确保新脚本即刻生效。
systemctl restart lr-host-start-agent.service
echo "已安装并启动 lr-host-start-agent.service。"
