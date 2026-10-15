#!/usr/bin/env bash
set -euo pipefail

# Install one robot's mTLS delivery package without exposing its MQTT password
# in the workspace, shell history, or a manually edited YAML file.
umask 077

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROBOT_CONFIG_DIR='/etc/lr-robot'
MQTT_DIR="$ROBOT_CONFIG_DIR/mqtt"
GATEWAY_CONFIG="$ROBOT_CONFIG_DIR/edge-gateway.yaml"
AUTO_LOCALIZATION_CONFIG="$ROBOT_CONFIG_DIR/auto-localization.yaml"
MAP_REGISTRY="$ROBOT_CONFIG_DIR/map-registry.yaml"
MAP_STORAGE_DIR='/var/lib/lr-robot/maps'
BOOK_SPINE_STORAGE_DIR='/var/lib/lr-robot/book-spines'
RUNTIME_ENV="$ROBOT_CONFIG_DIR/runtime.env"

robot_id=''
station_host=''
delivery_package=''
map_id=''
map_min_x=''
map_max_x=''
map_min_y=''
map_max_y=''
bookarm_backend='legacy'
station_https_origin=''
restart_runtime=false
preflight_only=false

usage() {
  cat <<'EOF'
用法：
  sudo install-control-station-gateway.sh \
    --robot-id LR-01 \
    --station-host control-station.example \
    --delivery-package /var/lib/lr-platform/pki/exports/robots/LR-01 \
    --map-id MAP-XXXXXXXXXXXX \
    --map-bounds <min-x> <max-x> <min-y> <max-y> \
    [--bookarm-backend legacy|sdk] \
    [--station-https-origin https://control-station.example:8443] [--preflight] \
    [--restart]

该脚本只安装指定机器人的 MQTT 客户端证书和 Gateway 私有配置。
--preflight 仅校验交付包、地图参数和 MQTT/mTLS，不写入配置、不构建、不重启服务。
--restart 会在配置校验通过后重启 lr-robot-runtime.service；不发送任何移动命令。
EOF
}

die() {
  echo "错误：$*" >&2
  exit 1
}

require_value() {
  [[ $# -eq 2 && -n "$2" ]] || die "$1 需要一个值"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --robot-id)
      require_value "$1" "${2:-}"
      robot_id="$2"
      shift 2
      ;;
    --station-host)
      require_value "$1" "${2:-}"
      station_host="$2"
      shift 2
      ;;
    --delivery-package)
      require_value "$1" "${2:-}"
      delivery_package="$2"
      shift 2
      ;;
    --map-id)
      require_value "$1" "${2:-}"
      map_id="$2"
      shift 2
      ;;
    --map-bounds)
      [[ $# -ge 5 ]] || die '--map-bounds 需要 4 个数值'
      map_min_x="$2"
      map_max_x="$3"
      map_min_y="$4"
      map_max_y="$5"
      shift 5
      ;;
    --bookarm-backend)
      require_value "$1" "${2:-}"
      bookarm_backend="$2"
      shift 2
      ;;
    --station-https-origin)
      require_value "$1" "${2:-}"
      station_https_origin="$2"
      shift 2
      ;;
    --restart)
      restart_runtime=true
      shift
      ;;
    --preflight)
      preflight_only=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "未知参数：$1"
      ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || die '请使用 sudo 执行。'
[[ "$preflight_only" == false || "$restart_runtime" == false ]] || \
  die '--preflight 不能与 --restart 同时使用。'
[[ "$robot_id" =~ ^LR-[A-Za-z0-9_-]+$ ]] || die '机器人 ID 必须匹配 LR-xx 格式。'
[[ "$map_id" =~ ^MAP-[A-Za-z0-9_-]+$ ]] || die '地图 ID 必须匹配 MAP-xx 格式。'
[[ "$station_host" != *[[:space:]]* && "$station_host" != */* ]] || die '控制站地址不能包含空白或路径。'
[[ "$bookarm_backend" == legacy || "$bookarm_backend" == sdk ]] || die '--bookarm-backend 必须是 legacy 或 sdk。'
if [[ -z "$station_https_origin" ]]; then
  station_https_origin="https://$station_host"
fi
python3 - "$station_https_origin" <<'PY' || die '--station-https-origin 必须是不含路径或凭据的根 HTTPS 地址。'
import sys
from urllib.parse import urlsplit

parsed = urlsplit(sys.argv[1])
try:
    valid_port = parsed.port is None or 1 <= parsed.port <= 65535
except ValueError:
    valid_port = False
if not (
    parsed.scheme == "https"
    and parsed.hostname
    and parsed.username is None
    and parsed.password is None
    and parsed.path in {"", "/"}
    and not parsed.query
    and not parsed.fragment
    and valid_port
):
    raise SystemExit(1)
PY
[[ -d "$delivery_package" ]] || die "找不到机器人交付包：$delivery_package"

for value in "$map_min_x" "$map_max_x" "$map_min_y" "$map_max_y"; do
  [[ "$value" =~ ^-?[0-9]+([.][0-9]+)?$ ]] || die '地图边界必须是十进制数。'
done
awk -v min_x="$map_min_x" -v max_x="$map_max_x" -v min_y="$map_min_y" -v max_y="$map_max_y" \
  'BEGIN { exit !(min_x < max_x && min_y < max_y) }' || die '地图边界必须满足 min < max。'

ca_file="$delivery_package/mqtt/ca.crt"
cert_file="$delivery_package/mqtt/$robot_id.crt"
key_file="$delivery_package/mqtt/$robot_id.key"
credentials_file="$delivery_package/mqtt-credentials.env"
for file in "$ca_file" "$cert_file" "$key_file" "$credentials_file"; do
  [[ -r "$file" ]] || die "交付包缺少文件：$file"
done

openssl verify -CAfile "$ca_file" "$cert_file" >/dev/null || die '机器人客户端证书不受交付包 CA 信任。'
openssl x509 -in "$cert_file" -checkend 86400 -noout >/dev/null || die '机器人客户端证书将在 24 小时内过期。'
[[ "$(openssl x509 -in "$cert_file" -noout -subject -nameopt RFC2253)" == *"CN=$robot_id"* ]] || \
  die '交付包证书的 CN 与 --robot-id 不一致。'
cert_digest="$(openssl x509 -in "$cert_file" -pubkey -noout | openssl pkey -pubin -outform DER | openssl dgst -sha256)"
key_digest="$(openssl pkey -in "$key_file" -pubout -outform DER | openssl dgst -sha256)"
[[ "$cert_digest" == "$key_digest" ]] || die '机器人客户端证书与私钥不匹配。'

# The delivery file is root-readable. Parse only the two expected assignments;
# never source a credential file as shell code or print the password.
mqtt_username="$(awk -F= '$1 == "mqtt_username" { print $2; found += 1 } END { exit found == 1 ? 0 : 1 }' "$credentials_file")" || \
  die '交付包 MQTT 用户名格式不正确。'
mqtt_password="$(awk -F= '$1 == "mqtt_password" { print $2; found += 1 } END { exit found == 1 ? 0 : 1 }' "$credentials_file")" || \
  die '交付包 MQTT 密码格式不正确。'
[[ "${mqtt_username:-}" == "$robot_id" ]] || die '交付包 MQTT 用户名与机器人 ID 不一致。'
[[ "${mqtt_password:-}" =~ ^[A-Za-z0-9._~!@%+=:-]{16,}$ ]] || die '交付包 MQTT 密码格式不正确。'

# Verify the server certificate and client certificate acceptance before
# replacing local runtime files. Python validates DNS and IP SANs correctly;
# the probe stops after TLS and never publishes an MQTT message.
python3 - "$station_host" "$ca_file" "$cert_file" "$key_file" <<'PY' || \
  die '无法验证控制站 MQTT/mTLS；请检查控制站地址、8883 防火墙、证书 SAN 和交付包。'
import socket
import ssl
import sys

host, ca_file, cert_file, key_file = sys.argv[1:]
context = ssl.create_default_context(cafile=ca_file)
context.load_cert_chain(certfile=cert_file, keyfile=key_file)
with socket.create_connection((host, 8883), timeout=8) as raw_socket:
    with context.wrap_socket(raw_socket, server_hostname=host):
        pass
PY

if [[ "$preflight_only" == true ]]; then
  echo "已通过 $robot_id 的 Gateway 非动作预检。"
  echo '已校验交付包、地图参数和 MQTT/mTLS；未写入配置、未构建、未重启服务。'
  exit 0
fi

install -d -o root -g boreas -m 0750 "$ROBOT_CONFIG_DIR" "$MQTT_DIR"
install -d -o root -g boreas -m 0750 "$MAP_STORAGE_DIR"
install -d -o boreas -g boreas -m 0700 "$BOOK_SPINE_STORAGE_DIR"
if [[ -f "$GATEWAY_CONFIG" ]]; then
  backup="$GATEWAY_CONFIG.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp -p "$GATEWAY_CONFIG" "$backup"
  chmod 0600 "$backup"
fi
if [[ -f "$AUTO_LOCALIZATION_CONFIG" ]]; then
  backup="$AUTO_LOCALIZATION_CONFIG.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp -p "$AUTO_LOCALIZATION_CONFIG" "$backup"
  chmod 0600 "$backup"
fi
if [[ -f "$RUNTIME_ENV" ]]; then
  backup="$RUNTIME_ENV.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp -p "$RUNTIME_ENV" "$backup"
  chmod 0600 "$backup"
fi

install -o root -g boreas -m 0644 "$ca_file" "$MQTT_DIR/ca.crt"
install -o root -g boreas -m 0644 "$cert_file" "$MQTT_DIR/$robot_id.crt"
install -o root -g boreas -m 0640 "$key_file" "$MQTT_DIR/$robot_id.key"

temporary_config="$(mktemp "$ROBOT_CONFIG_DIR/.edge-gateway.yaml.XXXXXX")"
trap 'rm -f "$temporary_config"' EXIT
cat >"$temporary_config" <<EOF
edge_gateway_node:
  ros__parameters:
    robot_id: "$robot_id"
    mqtt_host: "$station_host"
    mqtt_port: 8883
    mqtt_protocol_version: "3.1.1"
    mqtt_tls_enabled: true
    mqtt_ca_file: "$MQTT_DIR/ca.crt"
    mqtt_client_cert_file: "$MQTT_DIR/$robot_id.crt"
    mqtt_client_key_file: "$MQTT_DIR/$robot_id.key"
    mqtt_username: "$mqtt_username"
    mqtt_password: "$mqtt_password"
    allowed_map_ids:
      - "$map_id"
    active_map_id: "$map_id"
    map_registry_file: "$MAP_REGISTRY"
    map_state_file: "/home/boreas/.local/state/lr-robot/active_map.json"
    map_load_service: "/map_server/load_map"
    map_switch_topic: "/localization/map_switch"
    map_min_x: $map_min_x
    map_max_x: $map_max_x
    map_min_y: $map_min_y
    map_max_y: $map_max_y
    max_abs_yaw: 6.284
    command_queue_size: 10
    navigate_action_name: "navigate_to_pose"
    manual_cmd_vel_topic: "/cmd_vel_nav"
    manual_command_timeout_seconds: 1.0
    amcl_pose_topic: "/amcl_pose"
    initial_pose_topic: "/initialpose"
    localization_status_topic: "/localization/status"
    localization_scan_mode_topic: "/localization/scan_mode"
    chassis_status_topic: "/wheel_speed_source"
    chassis_status_timeout_seconds: 2.5
    wheel_speed_topic: "/wheel_speeds"
    wheel_speed_timeout_seconds: 1.0
    base_stationary_speed_threshold_mps: 0.01
    bookarm_backend: "$bookarm_backend"
    bookarm_api_url: "http://127.0.0.1:18080/api/v1"
    bookarm_timeout_seconds: 3.0
    bookarm_poll_period_seconds: 1.0
    bookarm_rod_device: "/dev/rodmotor"
    bookarm_lift_device: "/dev/lift_port"
    bookarm_spine_station_url: "$station_https_origin"
    bookarm_spine_storage_root: "$BOOK_SPINE_STORAGE_DIR"
    bookarm_spine_max_bytes: 8388608
    book_shelf_fine_tune_enabled: true
    book_shelf_fine_tune_parameter_service: "/book_shelf_fine_tune_node/set_parameters"
    book_shelf_fine_tune_start_service: "/book_shelf_fine_tune/start"
    book_shelf_fine_tune_abort_service: "/book_shelf_fine_tune/abort"
    book_shelf_fine_tune_settle_delay_seconds: 1.0
    telemetry_period_seconds: 0.5
    pose_stale_timeout_seconds: 5.0
    max_position_variance: 0.5
    max_yaw_variance: 0.35
EOF
install -o root -g boreas -m 0640 "$temporary_config" "$GATEWAY_CONFIG"

# The localization cache uses the same map identity as telemetry. Keep this
# private per-vehicle so a production map ID cannot drift from the Gateway.
temporary_localization_config="$(mktemp "$ROBOT_CONFIG_DIR/.auto-localization.yaml.XXXXXX")"
trap 'rm -f "$temporary_config" "$temporary_localization_config"' EXIT
cat >"$temporary_localization_config" <<EOF
auto_localization_node:
  ros__parameters:
    active_map_id: "$map_id"
    state_file: "~/.local/state/lr-robot/last_localization.json"
    map_registry_file: "$MAP_REGISTRY"
    map_min_x: $map_min_x
    map_max_x: $map_max_x
    map_min_y: $map_min_y
    map_max_y: $map_max_y
    pose_topic: "/amcl_pose"
    initial_pose_topic: "/initialpose"
    status_topic: "/localization/status"
    map_switch_topic: "/localization/map_switch"
    set_initial_pose_service: "/set_initial_pose"
    global_localization_service: "/reinitialize_global_localization"
    nomotion_update_service: "/request_nomotion_update"
    max_saved_pose_age_seconds: 604800.0
    initialization_timeout_seconds: 30.0
    min_stable_samples: 3
    max_position_variance: 0.5
    max_yaw_variance: 0.35
    pose_stale_timeout_seconds: 5.0
    nomotion_update_period_seconds: 1.0
EOF
install -o root -g boreas -m 0640 "$temporary_localization_config" "$AUTO_LOCALIZATION_CONFIG"

temporary_env="$(mktemp "$ROBOT_CONFIG_DIR/.runtime.env.XXXXXX")"
trap 'rm -f "$temporary_config" "$temporary_localization_config" "$temporary_env"' EXIT
if [[ -f "$RUNTIME_ENV" ]]; then
  awk '
    $0 !~ /^LR_GATEWAY_PARAMS_FILE=/ &&
    $0 !~ /^LR_AUTO_LOCALIZATION_PARAMS_FILE=/ &&
    $0 !~ /^LR_ROS_AUTOMATIC_DISCOVERY_RANGE=/ { print }
  ' "$RUNTIME_ENV" >"$temporary_env"
fi
printf '%s\n' \
  'LR_GATEWAY_PARAMS_FILE=/etc/lr-robot/edge-gateway.yaml' \
  'LR_AUTO_LOCALIZATION_PARAMS_FILE=/etc/lr-robot/auto-localization.yaml' \
  'LR_ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST' >>"$temporary_env"
install -o root -g boreas -m 0640 "$temporary_env" "$RUNTIME_ENV"

# 单机交付已弃用车端地图自动同步（lr-robot-map-sync.timer 不再安装，也不做
# 首次同步）；车端地图走控制站本机直注册，由 register-robot-map.sh 登记。
PYTHONDONTWRITEBYTECODE=1 python3 "$WORKSPACE/scripts/map-registry.py" validate \
  --registry "$MAP_REGISTRY" \
  --fallback-map-id "$map_id" || \
  die '当前地图尚未登记到车端。请使用 register-robot-map.sh 将当前地图登记到本车注册表后重试。'

# Ensure the installed overlay contains the current Gateway implementation
# before systemd restarts it.  The private MQTT configuration remains outside
# the workspace and is not read by this build step.
if [[ "$restart_runtime" == true ]]; then
  [[ -r /opt/ros/jazzy/setup.bash ]] || die '找不到 /opt/ros/jazzy/setup.bash，无法构建车端 Gateway。'
  command -v colcon >/dev/null 2>&1 || die '找不到 colcon，无法构建车端 Gateway。'
  # systemd runs the runtime as boreas.  A root-owned overlay can leave its
  # package metadata unreadable, so repair previous artifacts and build as
  # that same service account.
  for artifact_dir in "$WORKSPACE/build" "$WORKSPACE/install" "$WORKSPACE/log"; do
    [[ -e "$artifact_dir" ]] && chown -R boreas:boreas "$artifact_dir"
  done
  runuser -u boreas -- env HOME=/home/boreas WORKSPACE="$WORKSPACE" bash -c '
    set -eo pipefail
    cd "$WORKSPACE"
    # ROS setup reads optional variables before initializing them.
    set +u
    # shellcheck disable=SC1091
    source /opt/ros/jazzy/setup.bash
    set -u
    colcon build \
      --packages-select mobile_robot_nav_bringup dual_laser_merger robot_decision \
      --symlink-install
  ' || die '车端 Gateway 构建失败；未重启运行时。'
fi

echo "已安装 $robot_id 的 mTLS Gateway 配置。"
echo "配置文件：$GATEWAY_CONFIG（root:boreas 0640）"
echo "自动定位配置：$AUTO_LOCALIZATION_CONFIG（root:boreas 0640）"
echo "地图注册表：$MAP_REGISTRY（请用 register-robot-map.sh 登记当前与后续地图）"
echo '车端地图走本机直注册；新地图登记后出现在控制站可切换列表，不会自动切换当前地图。'
echo "证书目录：$MQTT_DIR（私钥 root:boreas 0640）"

if [[ "$restart_runtime" == true ]]; then
  systemctl restart lr-robot-runtime.service || \
    die '无法重启 lr-robot-runtime.service；新配置已写入但尚未启用。'
  systemctl --no-pager --full status lr-robot-runtime.service
else
  echo '尚未重启车载运行时；确认现场安全后执行：sudo systemctl restart lr-robot-runtime.service'
fi
