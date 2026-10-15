#!/usr/bin/env bash
set -euo pipefail

# 【已弃用】单机交付已弃用本脚本，地图导入走控制站本机直注册
# （register-robot-map.sh / 建图代理 /mapping/save 自动登记）。本文件仅保留作
# 历史参考，install-control-station-gateway.sh 已不再调用它；不要在新交付中
# 执行，现网 lr-robot-map-sync.timer 已禁用。
#
# Install a root-owned, mTLS-only synchronization timer. It never starts ROS,
# changes the active map, or sends a chassis command.
if [[ "$(id -u)" -ne 0 ]]; then
  echo '请使用 sudo 执行。' >&2
  exit 1
fi

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR='/etc/lr-robot'
CONFIG_FILE="$CONFIG_DIR/map-sync.yaml"
SERVICE_FILE='/etc/systemd/system/lr-robot-map-sync.service'
TIMER_FILE='/etc/systemd/system/lr-robot-map-sync.timer'

robot_id=''
station_url=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --robot-id) robot_id="${2:-}"; shift 2 ;;
    --station-url) station_url="${2:-}"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[[ "$robot_id" =~ ^LR-[A-Za-z0-9_-]{1,80}$ ]] || { echo '机器人 ID 格式不正确。' >&2; exit 2; }
[[ "$station_url" =~ ^https://[^/?#[:space:]]+/?$ ]] || { echo '控制站地址必须是根 HTTPS 地址。' >&2; exit 2; }
for file in \
  "$CONFIG_DIR/mqtt/ca.crt" \
  "$CONFIG_DIR/mqtt/$robot_id.crt" \
  "$CONFIG_DIR/mqtt/$robot_id.key" \
  "$WORKSPACE/scripts/sync-robot-maps.py" \
  "$WORKSPACE/scripts/map-registry.py"; do
  [[ -r "$file" ]] || { echo "缺少所需文件：$file" >&2; exit 1; }
done

install -d -o root -g boreas -m 0750 "$CONFIG_DIR"
temporary="$(mktemp "$CONFIG_DIR/.map-sync.yaml.XXXXXX")"
trap 'rm -f "$temporary"' EXIT
cat >"$temporary" <<EOF
version: 1
stationUrl: "$station_url"
robotId: "$robot_id"
caFile: "$CONFIG_DIR/mqtt/ca.crt"
certificateFile: "$CONFIG_DIR/mqtt/$robot_id.crt"
keyFile: "$CONFIG_DIR/mqtt/$robot_id.key"
registryFile: "$CONFIG_DIR/map-registry.yaml"
storageRoot: "/var/lib/lr-robot/maps"
stateFile: "/var/lib/lr-robot/map-sync-state.json"
EOF
install -o root -g boreas -m 0640 "$temporary" "$CONFIG_FILE"

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=LR robot map synchronization (mTLS only)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=$WORKSPACE
ExecStart=$WORKSPACE/scripts/sync-robot-maps.py --config $CONFIG_FILE
EOF

cat >"$TIMER_FILE" <<'EOF'
[Unit]
Description=Periodic LR robot map synchronization

[Timer]
OnBootSec=30
OnUnitActiveSec=60
RandomizedDelaySec=15
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now lr-robot-map-sync.timer
systemctl start lr-robot-map-sync.service
echo "已启用车端地图自动同步：$CONFIG_FILE"
echo '已完成首次地图同步；后续同步仅下载、校验并登记新地图，不会切换当前地图或操作 ROS/底盘。'
