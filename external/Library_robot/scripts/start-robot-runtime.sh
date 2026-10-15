#!/usr/bin/env bash
set -eo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROS_SETUP="/opt/ros/jazzy/setup.bash"
OVERLAY_SETUP="$WORKSPACE/install/setup.bash"
IMU_PARAMS_FILE="$WORKSPACE/install/car_ctrl/share/car_ctrl/config/ddsm_hat_diff_drive.yaml"
ROBOT_ENV_FILE="/etc/lr-robot/runtime.env"

# This optional root-managed file only selects the private Gateway parameter
# file. It keeps customer network credentials out of the ROS workspace and Git.
if [ -r "$ROBOT_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ROBOT_ENV_FILE"
fi
GATEWAY_PARAMS_FILE="${LR_GATEWAY_PARAMS_FILE:-$WORKSPACE/src/robot_decision/config/edge_gateway.yaml}"
AUTO_LOCALIZATION_PARAMS_FILE="${LR_AUTO_LOCALIZATION_PARAMS_FILE:-$WORKSPACE/src/robot_decision/config/auto_localization.yaml}"
export ROS_AUTOMATIC_DISCOVERY_RANGE="${LR_ROS_AUTOMATIC_DISCOVERY_RANGE:-LOCALHOST}"

# A previous instance of this unit can leave orphaned ROS children behind
# (drivers in uninterruptible sleep outlive even systemd's SIGKILL pass).
# They stay in this unit's own cgroup and would fight this instance for the
# MQTT client identity, CAN/serial devices and Nav2 action servers.  Reap
# everything the previous instance left before launching anything.
CGROUP_PROCS_FILE="/sys/fs/cgroup/system.slice/lr-robot-runtime.service/cgroup.procs"
if [ -r "$CGROUP_PROCS_FILE" ]; then
  for pid in $(cat "$CGROUP_PROCS_FILE" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    kill -TERM "$pid" 2>/dev/null || true
  done
  for _ in $(seq 1 80); do
    leftover=false
    for pid in $(cat "$CGROUP_PROCS_FILE" 2>/dev/null); do
      [ "$pid" = "$$" ] && continue
      if kill -0 "$pid" 2>/dev/null; then
        leftover=true
        break
      fi
    done
    [ "$leftover" = false ] && break
    sleep 0.1
  done
  for pid in $(cat "$CGROUP_PROCS_FILE" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    kill -KILL "$pid" 2>/dev/null || true
  done
fi

if [ ! -f "$ROS_SETUP" ] || [ ! -f "$OVERLAY_SETUP" ]; then
  echo "ROS 运行环境不完整，请先完成机器人工作空间编译。" >&2
  exit 1
fi

if [ ! -r "$GATEWAY_PARAMS_FILE" ]; then
  echo "无法读取 Edge Gateway 参数文件：$GATEWAY_PARAMS_FILE" >&2
  exit 1
fi
if [ ! -r "$AUTO_LOCALIZATION_PARAMS_FILE" ]; then
  echo "无法读取自动定位参数文件：$AUTO_LOCALIZATION_PARAMS_FILE" >&2
  exit 1
fi

# ROS setup scripts intentionally read optional environment variables that may
# be unset in a clean systemd environment, so do not enable bash nounset here.
source "$ROS_SETUP"
source "$OVERLAY_SETUP"

# The map registry is optional during a staged upgrade. When configured, the
# selection is resolved before Nav2 starts so boot and a previous map switch
# cannot leave Nav2, localization, and Gateway on different map identities.
map_runtime_config="$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$WORKSPACE/src/robot_decision${PYTHONPATH:+:$PYTHONPATH}" python3 - "$GATEWAY_PARAMS_FILE" <<'PY'
import sys
from pathlib import Path

import yaml

from robot_decision.map_registry import active_map_id, load_map_registry

config_path = Path(sys.argv[1])
document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
params = document.get("edge_gateway_node", {}).get("ros__parameters", {})
if not isinstance(params, dict):
    raise SystemExit("Gateway parameters are invalid")
fallback = params.get("active_map_id")
if not isinstance(fallback, str) or not fallback.startswith("MAP-"):
    raise SystemExit("Gateway active_map_id is invalid")
registry_file = params.get("map_registry_file", "")
state_file = params.get("map_state_file", "~/.local/state/lr-robot/active_map.json")
if not isinstance(registry_file, str) or not registry_file.strip():
    print(fallback)
    print("")
    raise SystemExit(0)
if not Path(registry_file).expanduser().is_file():
    raise SystemExit("Gateway map registry is configured but has not been staged")
if not isinstance(state_file, str):
    raise SystemExit("Gateway map_state_file is invalid")
maps = load_map_registry(Path(registry_file).expanduser())
selected = active_map_id(Path(state_file).expanduser(), fallback)
if selected not in maps:
    raise SystemExit("The selected map is not registered locally")
print(selected)
print(maps[selected].yaml_path)
PY
)"
mapfile -t runtime_map <<<"$map_runtime_config"
ACTIVE_MAP_ID="${runtime_map[0]:-}"
ACTIVE_MAP_YAML="${runtime_map[1]:-}"
if [[ -z "$ACTIVE_MAP_ID" ]]; then
  echo '无法确定车端当前地图。' >&2
  exit 1
fi

imu_supervisor() {
  local imu_pid exit_code

  stop_imu_supervisor() {
    if [ -n "${imu_pid:-}" ]; then
      kill -TERM "$imu_pid" 2>/dev/null || true
      wait "$imu_pid" 2>/dev/null || true
    fi
    exit 0
  }
  trap stop_imu_supervisor INT TERM

  while true; do
    echo "Starting IMU driver." >&2
    ros2 run car_ctrl imu_driver --ros-args --params-file "$IMU_PARAMS_FILE" &
    imu_pid=$!
    wait "$imu_pid" || exit_code=$?
    imu_pid=""
    echo "IMU driver exited (${exit_code:-0}); restarting in 1 second." >&2
    unset exit_code
    sleep 1
  done
}

# Nav2 lifecycle_manager 在启动高并发时可能丢失 change_state 的 DDS 响应而
# 静默挂起（实测：map_server configure 成功但响应超时，manager 从此不再
# 推进任何节点，定位栈永远停在 map_server=inactive、amcl=unconfigured 的
# 半成品状态）。看门狗先给 manager 留 75 秒自行完成 bringup；超时仍未就绪
# 则按 configure -> activate 顺序补发转换（幂等，与 manager 并发安全），
# 两个节点都 active 后立即退出，不干预后续任何刻意的状态切换。
localization_lifecycle_watchdog() {
  stop_localization_watchdog() {
    exit 0
  }
  trap stop_localization_watchdog INT TERM

  local node state all_active
  for _ in $(seq 1 75); do
    all_active=true
    for node in /map_server /amcl; do
      state="$(timeout 5 ros2 lifecycle get "$node" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
      if [ "$state" != "active" ]; then
        all_active=false
        break
      fi
    done
    if [ "$all_active" = true ]; then
      exit 0
    fi
    sleep 1
  done

  echo "定位生命周期看门狗：lifecycle_manager 75 秒内未完成定位 bringup，接管推进。" >&2
  for node in /map_server /amcl; do
    for _ in $(seq 1 90); do
      state="$(timeout 5 ros2 lifecycle get "$node" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
      case "$state" in
        active)
          break
          ;;
        inactive)
          timeout 10 ros2 lifecycle set "$node" activate >/dev/null 2>&1 || true
          ;;
        unconfigured)
          timeout 10 ros2 lifecycle set "$node" configure >/dev/null 2>&1 || true
          ;;
      esac
      sleep 2
    done
    if [ "$state" != "active" ]; then
      echo "定位生命周期看门狗：$node 未能进入 active（当前 ${state:-unknown}）。" >&2
    fi
  done
}

# Navigation must own the sensors and AMCL before the gateway reads /amcl_pose.
navigation_args=(
  use_nav_rviz:=false
  use_lidar_rviz:=false
  start_imu_driver:=false
)
if [[ -n "$ACTIVE_MAP_YAML" ]]; then
  navigation_args+=("map:=$ACTIVE_MAP_YAML")
fi
ros2 launch mobile_robot_nav_bringup full_navigation.launch.py "${navigation_args[@]}" &
NAVIGATION_PID=$!

# The IMU driver owns a USB serial device and must survive transient USB I/O
# failures independently of the navigation launch process.
imu_supervisor &
IMU_SUPERVISOR_PID=$!

localization_lifecycle_watchdog &
LOCALIZATION_WATCHDOG_PID=$!

for _ in $(seq 1 45); do
  if ros2 node list 2>/dev/null | grep -qx '/amcl'; then
    break
  fi
  sleep 1
done

if ! ros2 node list 2>/dev/null | grep -qx '/amcl'; then
  echo "导航定位服务未在预期时间内启动。" >&2
  exit 1
fi

ros2 launch robot_decision auto_localization.launch.py \
  params_file:="$AUTO_LOCALIZATION_PARAMS_FILE" \
  active_map_id:="$ACTIVE_MAP_ID" &
AUTO_LOCALIZATION_PID=$!

# Book tasks require a final shelf-pose alignment before any arm workflow. It
# publishes only to /cmd_vel_nav, so Nav2's velocity and collision safeguards
# stay between the controller and the chassis.
ros2 launch robot_decision book_shelf_fine_tune.launch.py &
BOOK_SHELF_FINE_TUNE_PID=$!

cleanup() {
  local pid
  local live
  local children=(
    "$IMU_SUPERVISOR_PID"
    "$LOCALIZATION_WATCHDOG_PID"
    "$AUTO_LOCALIZATION_PID"
    "$BOOK_SHELF_FINE_TUNE_PID"
    "$NAVIGATION_PID"
    "$EDGE_GATEWAY_PID"
  )

  # Give ROS launches a chance to propagate a clean shutdown before systemd
  # needs to escalate.  Signalling all parents first keeps the stop bounded.
  for pid in "${children[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for _ in $(seq 1 50); do
    live=false
    for pid in "${children[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        live=true
        break
      fi
    done
    [ "$live" = false ] && break
    sleep 0.1
  done
  for pid in "${children[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT
# Exit non-zero after a service signal so systemd starts a fresh runtime.
trap 'exit 143' INT TERM

ros2 launch robot_decision edge_gateway.launch.py \
  params_file:="$GATEWAY_PARAMS_FILE" &
EDGE_GATEWAY_PID=$!
wait "$EDGE_GATEWAY_PID"
