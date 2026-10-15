#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RULE_SRC="${REPO_DIR}/config/99-robot-serial.rules"
RULE_DST="/etc/udev/rules.d/99-robot-serial.rules"

sudo install -m 0644 "${RULE_SRC}" "${RULE_DST}"
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty

echo "Installed ${RULE_DST}"
echo "Check with: ls -l /dev/chassis_serial_port /dev/laser_port /dev/laser_back /dev/imu_port"
