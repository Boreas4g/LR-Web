#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 sudo 执行此脚本。" >&2
  exit 1
fi

systemctl disable --now lr-station-discovery.service 2>/dev/null || true
rm -f /etc/systemd/system/lr-station-discovery.service
rm -f /usr/local/libexec/lr-station-discovery.py
rm -f /etc/lr-platform/discovery.json
systemctl daemon-reload
echo "已停止并移除局域网发现服务；lr-platform.service 和平台数据保持不变。"
