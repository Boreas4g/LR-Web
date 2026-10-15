#!/usr/bin/env bash
# Install, but do not start, the supervised BookArm HTTP service.

set -euo pipefail

BOOKARM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="lr-bookarm-takeout.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
CAN_SERVICE_NAME="lr-bookarm-can.service"
CAN_SERVICE_PATH="/etc/systemd/system/$CAN_SERVICE_NAME"
RUN_AS_USER="${LR_BOOKARM_USER:-${SUDO_USER:-$(id -un)}}"

[[ "$(id -u)" -eq 0 ]] || {
  echo "请使用 sudo 执行此安装脚本。" >&2
  exit 1
}
id "$RUN_AS_USER" >/dev/null 2>&1 || {
  echo "运行用户不存在：$RUN_AS_USER" >&2
  exit 1
}

install -d -o root -g root -m 0755 /etc/lr-bookarm
if [[ ! -e /etc/lr-bookarm/bookarm.env ]]; then
  install -o root -g root -m 0640 /dev/null /etc/lr-bookarm/bookarm.env
fi

cat >"$CAN_SERVICE_PATH" <<EOF
[Unit]
Description=BookArm CAN interface (can0 @ 1Mbps)
After=network-online.target
Wants=network-online.target
Before=$SERVICE_NAME

[Service]
Type=oneshot
RemainAfterExit=yes
# A manual CAN recovery may already have brought the interface up before this
# unit is installed.  Never flap a live bus merely to make systemd report the
# dependency active; configure it only when it is actually offline.
ExecStart=/usr/bin/bash -c '/usr/sbin/ip link show can0 2>/dev/null | /usr/bin/grep -q "state UP" || exec $BOOKARM_ROOT/scripts/arm/setup_can.sh can0 1000000'

[Install]
WantedBy=multi-user.target
EOF

cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=BookArm take/return HTTP service (private Unix socket)
After=network-online.target
Wants=network-online.target
Requires=$CAN_SERVICE_NAME
After=$CAN_SERVICE_NAME
# The retired Gateway BookArm runtime owns the same CAN, camera and serial
# devices.  Do not use Conflicts= here: systemd would stop the existing
# runtime as a side effect of starting this one.  A mechanical-arm handover
# must always be an explicit, on-site action.

[Service]
Type=simple
User=$RUN_AS_USER
WorkingDirectory=$BOOKARM_ROOT
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-/etc/lr-bookarm/bookarm.env
RuntimeDirectory=lr-bookarm
RuntimeDirectoryMode=0755
ExecStartPre=/usr/bin/bash -c '! /usr/bin/systemctl is-active --quiet lr-bookarm-runtime.service'
ExecStart=$BOOKARM_ROOT/scripts/start-takeout-service.sh
Restart=on-failure
RestartSec=3
# The task runner still waits for its bounded device operation to finish
# rather than closing CAN/serial/camera handles in the middle of a physical
# action, but it gives up on its own after --shutdown-timeout-s (default 10s).
# A stuck device call must never suspend `systemctl stop` forever; systemd's
# budget stays a safe margin above the runner's own.
TimeoutStopSec=30s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$CAN_SERVICE_NAME" >/dev/null
echo "已安装 $CAN_SERVICE_NAME 和 $SERVICE_NAME，尚未启动 BookArm。"
echo "确认现场硬件安全后执行：sudo systemctl enable --now $SERVICE_NAME"
