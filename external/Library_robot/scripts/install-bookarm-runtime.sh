#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="$(cd "$ROOT_DIR/.." && pwd)"
BOOKARM_ROOT="${LR_BOOKARM_ROOT:-$WORKSPACE_ROOT/lingzu-bookarm-py}"
SERVICE_FILE='/etc/systemd/system/lr-bookarm-runtime.service'
ENV_DIR='/etc/lr-robot'
ENV_FILE="$ENV_DIR/bookarm.env"
RUNTIME_USER='boreas'

usage() {
  cat <<'EOF'
安装机器人本机的机械臂运行服务。默认是旧 MotorStudio；显式传入
`--backend sdk` 才切换为独立 BookArm SDK loopback 服务。

用法：
  sudo ./scripts/install-bookarm-runtime.sh [--backend legacy|sdk] [--bookarm-root /path/to/bookarm] [--book-stand-slots /path/to/take-slots.json] [--book-stand-pickup-slots /path/to/return-slots.json] [--camera-calibration /path/to/extrinsic.json] [--camera-config /path/to/d435.json] [--return-book-profile /path/to/return-profile.json] [--python /path/to/python] [--port 18080] [--can can0] [--restart]

服务只监听 127.0.0.1。车端 ROS Edge Gateway 是唯一调用方，网页与控制站不能直接访问机械臂 API。
安装不会连接、使能或移动机械臂。
EOF
}

python_bin='/home/boreas/miniconda3/bin/python3'
port='18080'
can_interface='can0'
backend='legacy'
backend_explicit=false
bookarm_root="${LR_BOOKARM_ROOT:-$BOOKARM_ROOT}"
sdk_slots=''
sdk_pickup_slots=''
sdk_camera_calibration=''
sdk_camera_config=''
sdk_profile=''
sdk_return_profile=''
spine_storage='/var/lib/lr-robot/book-spines'
rod_device='/dev/rodmotor'
lift_device='/dev/lift_port'
restart=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend) backend="${2:?--backend 需要 legacy 或 sdk}"; backend_explicit=true; shift 2 ;;
    --bookarm-root) bookarm_root="${2:?--bookarm-root 需要路径}"; shift 2 ;;
    --book-stand-slots) sdk_slots="${2:?--book-stand-slots 需要路径}"; shift 2 ;;
    --book-stand-pickup-slots) sdk_pickup_slots="${2:?--book-stand-pickup-slots 需要路径}"; shift 2 ;;
    --camera-calibration) sdk_camera_calibration="${2:?--camera-calibration 需要路径}"; shift 2 ;;
    --camera-config) sdk_camera_config="${2:?--camera-config 需要路径}"; shift 2 ;;
    --take-book-profile) sdk_profile="${2:?--take-book-profile 需要路径}"; shift 2 ;;
    --return-book-profile) sdk_return_profile="${2:?--return-book-profile 需要路径}"; shift 2 ;;
    --spine-storage) spine_storage="${2:?--spine-storage 需要路径}"; shift 2 ;;
    --rod-device) rod_device="${2:?--rod-device 需要路径}"; shift 2 ;;
    --lift-device) lift_device="${2:?--lift-device 需要路径}"; shift 2 ;;
    --python) python_bin="${2:?--python 需要路径}"; shift 2 ;;
    --port) port="${2:?--port 需要端口}"; shift 2 ;;
    --can) can_interface="${2:?--can 需要接口名}"; shift 2 ;;
    --restart) restart=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "错误：未知参数：$1" >&2; exit 1 ;;
  esac
done

BOOKARM_ROOT="$bookarm_root"

[[ "$(id -u)" -eq 0 ]] || { echo '错误：请使用 sudo 执行。' >&2; exit 1; }
[[ -x "$python_bin" ]] || { echo "错误：找不到 Python：$python_bin" >&2; exit 1; }
[[ -d "$BOOKARM_ROOT" ]] || { echo "错误：找不到机械臂项目：$BOOKARM_ROOT" >&2; exit 1; }
[[ "$backend" == "legacy" || "$backend" == "sdk" ]] || { echo '错误：--backend 必须是 legacy 或 sdk。' >&2; exit 1; }
[[ "$port" =~ ^[0-9]{2,5}$ ]] && ((port >= 1024 && port <= 65535)) || {
  echo '错误：端口必须为 1024-65535。' >&2; exit 1;
}
[[ "$can_interface" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$ ]] || {
  echo '错误：CAN 接口名格式不正确。' >&2; exit 1;
}

if [[ "$backend" == "legacy" ]]; then
  # The project is intentionally run from its checkout rather than globally
  # installed, so validate imports from the same working directory as systemd.
  runuser -u "$RUNTIME_USER" -- bash -c '
    cd "$1"
    exec "$2" -c "import PyQt6, fastapi, uvicorn, webui.backend.api"
  ' bash "$BOOKARM_ROOT" "$python_bin" || {
    echo '错误：机械臂运行环境缺少 PyQt6、FastAPI、Uvicorn 或 WebUI 模块。' >&2
    exit 1
  }
else
  [[ -f "$BOOKARM_ROOT/src/bookarm/api/sdk.py" ]] || {
    echo '错误：SDK 后端的 --bookarm-root 必须指向包含 src/bookarm/api/sdk.py 的项目目录。' >&2
    exit 1
  }
  [[ -f "$sdk_slots" ]] || { echo '错误：SDK 后端需要 --book-stand-slots。' >&2; exit 1; }
  [[ -f "$sdk_pickup_slots" ]] || { echo '错误：SDK 后端需要 --book-stand-pickup-slots。' >&2; exit 1; }
  [[ -f "$sdk_camera_calibration" ]] || { echo '错误：SDK 后端需要 --camera-calibration。' >&2; exit 1; }
  [[ -z "$sdk_camera_config" || -f "$sdk_camera_config" ]] || { echo '错误：--camera-config 文件不存在。' >&2; exit 1; }
  [[ -z "$sdk_profile" || -f "$sdk_profile" ]] || { echo '错误：--take-book-profile 文件不存在。' >&2; exit 1; }
  [[ -z "$sdk_return_profile" || -f "$sdk_return_profile" ]] || { echo '错误：--return-book-profile 文件不存在。' >&2; exit 1; }
fi

install -d -o root -g "$RUNTIME_USER" -m 0750 "$ENV_DIR"
if [[ ! -f "$ENV_FILE" || "$backend_explicit" == true ]]; then
  if [[ -f "$ENV_FILE" ]]; then
    backup="$ENV_FILE.$(date -u +%Y%m%dT%H%M%SZ).bak"
    cp -p "$ENV_FILE" "$backup"
    chmod 0600 "$backup"
  fi
  umask 077
  cat >"$ENV_FILE" <<EOF
LR_BOOKARM_ROOT=$BOOKARM_ROOT
LR_BOOKARM_PYTHON=$python_bin
LR_BOOKARM_PORT=$port
LR_BOOKARM_CAN_INTERFACE=$can_interface
LR_BOOKARM_BACKEND=$backend
LR_ROBOT_WORKSPACE=$ROOT_DIR
LR_BOOKARM_SDK_SLOTS=$sdk_slots
LR_BOOKARM_SDK_PICKUP_SLOTS=$sdk_pickup_slots
LR_BOOKARM_SDK_CAMERA_CALIBRATION=$sdk_camera_calibration
LR_BOOKARM_SDK_CAMERA_CONFIG=$sdk_camera_config
LR_BOOKARM_SDK_PROFILE=$sdk_profile
LR_BOOKARM_SDK_RETURN_PROFILE=$sdk_return_profile
LR_BOOKARM_SPINE_STORAGE=$spine_storage
LR_BOOKARM_ROD_DEVICE=$rod_device
LR_BOOKARM_LIFT_DEVICE=$lift_device
EOF
  chown root:"$RUNTIME_USER" "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
fi

install -o root -g root -m 0755 "$ROOT_DIR/scripts/start-bookarm-runtime.sh" /usr/local/libexec/lr-start-bookarm-runtime.sh
cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=LR 机器人本机 BookArm 图书机械臂服务
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUNTIME_USER
WorkingDirectory=$BOOKARM_ROOT
Environment=HOME=/home/boreas
Environment=LR_BOOKARM_ROOT=$BOOKARM_ROOT
Environment=LR_ROBOT_WORKSPACE=$ROOT_DIR
EnvironmentFile=-$ENV_FILE
ExecStart=/usr/local/libexec/lr-start-bookarm-runtime.sh
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lr-bookarm-runtime.service
if [[ "$restart" == true ]]; then
  systemctl restart lr-bookarm-runtime.service
  sleep 2
  systemctl --no-pager --full status lr-bookarm-runtime.service
else
  echo '已安装 lr-bookarm-runtime.service。确认机械臂周围安全后启动：'
  echo '  sudo systemctl start lr-bookarm-runtime.service'
fi
