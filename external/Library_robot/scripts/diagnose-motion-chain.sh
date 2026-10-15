#!/usr/bin/env bash
# Read-only ROS 2 topology diagnostic for the protected chassis velocity path.
set -euo pipefail

watch_mode=false
if [[ "${1:-}" == "--watch" ]]; then
  watch_mode=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--watch]" >&2
  exit 64
fi

if [[ ! -r /opt/ros/jazzy/setup.bash ]]; then
  echo "ROS 2 Jazzy is not installed at /opt/ros/jazzy; cannot inspect the motion chain." >&2
  exit 1
fi

workspace="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
overlay_setup="$workspace/install/setup.bash"
if [[ ! -r "$overlay_setup" ]]; then
  echo "ROS workspace overlay is unavailable at $overlay_setup; cannot inspect the motion chain." >&2
  exit 1
fi

# Do not source private runtime configuration or publish any ROS messages.
# ROS setup scripts intentionally reference optional variables that are unset
# in a clean shell, so briefly relax nounset while they are sourced.
set +u
source /opt/ros/jazzy/setup.bash
source "$overlay_setup"
set -u
# Keep this diagnostic local to the vehicle; it must not discover LAN ROS nodes.
export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-LOCALHOST}"

show_topic() {
  local topic="$1"
  echo
  echo "=== ${topic} ==="
  timeout 5 ros2 topic info -v "$topic" || true
}

show_lifecycle() {
  local node="$1"
  echo
  echo "=== lifecycle ${node} ==="
  timeout 5 ros2 lifecycle get "$node" || true
}

echo "LR motion-chain diagnostic (read-only; no velocity command is published)"
echo "Expected path: /cmd_vel_nav -> velocity_smoother -> /cmd_vel_smoothed -> collision_monitor -> /cmd_vel -> chassis driver"
echo
echo "=== matching ROS nodes ==="
timeout 5 ros2 node list | rg '(^|/)(edge_gateway|velocity_smoother|collision_monitor|ddsm_hat_diff_drive)' || true

show_lifecycle /velocity_smoother
show_lifecycle /collision_monitor
show_lifecycle /lifecycle_manager_navigation
show_topic /cmd_vel_nav
show_topic /cmd_vel_smoothed
show_topic /cmd_vel
show_topic /collision_monitor_state
show_topic /wheel_speed_source
show_topic /wheel_speeds

if [[ "$watch_mode" == true ]]; then
  echo
  echo "=== passive velocity observation ==="
  echo "现场维护人员现在可从控制站发起一次已授权的短促低速点动。"
  echo "脚本会并行观察三个话题最多 12 秒，绝不发送 ROS 控制命令。"
  for topic in /cmd_vel_nav /cmd_vel_smoothed /cmd_vel; do
    timeout 12 ros2 topic echo "$topic" geometry_msgs/msg/Twist --once &
  done
  wait || true
fi

echo
echo "Diagnostic complete. Do not redirect the Gateway to /cmd_vel; that would bypass collision_monitor."
