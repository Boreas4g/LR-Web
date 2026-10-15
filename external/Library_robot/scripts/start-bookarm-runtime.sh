#!/usr/bin/env bash
set -eo pipefail

# The MotorStudio WebUI remains loopback-only.  ROS calls it locally through
# Edge Gateway; neither the browser nor the MQTT broker can reach CAN/serial.
WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOOKARM_ROOT="${LR_BOOKARM_ROOT:-$WORKSPACE_ROOT/lingzu-bookarm-py}"
BOOKARM_ENV='/etc/lr-robot/bookarm.env'

if [[ -r "$BOOKARM_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$BOOKARM_ENV"
fi

PYTHON_BIN="${LR_BOOKARM_PYTHON:-/home/boreas/miniconda3/bin/python3}"
PORT="${LR_BOOKARM_PORT:-18080}"
CAN_INTERFACE="${LR_BOOKARM_CAN_INTERFACE:-can0}"
BACKEND="${LR_BOOKARM_BACKEND:-legacy}"
ROBOT_WORKSPACE="${LR_ROBOT_WORKSPACE:-/home/boreas/LR/Library_robot}"

[[ -d "$BOOKARM_ROOT" ]] || {
  echo "找不到机械臂项目：$BOOKARM_ROOT" >&2
  exit 1
}
[[ -x "$PYTHON_BIN" ]] || {
  echo "找不到机械臂 Python：$PYTHON_BIN" >&2
  exit 1
}
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] && ((PORT >= 1024 && PORT <= 65535)) || {
  echo "LR_BOOKARM_PORT 必须是 1024-65535 之间的端口" >&2
  exit 1
}
[[ "$CAN_INTERFACE" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$ ]] || {
  echo "LR_BOOKARM_CAN_INTERFACE 不是有效的 CAN 接口名" >&2
  exit 1
}

[[ "$BACKEND" == "legacy" || "$BACKEND" == "sdk" ]] || {
  echo "LR_BOOKARM_BACKEND 必须是 legacy 或 sdk" >&2
  exit 1
}

if [[ "$BACKEND" == "sdk" ]]; then
  [[ -d "$ROBOT_WORKSPACE/src/robot_decision" ]] || {
    echo "找不到车端 robot_decision 工作区：$ROBOT_WORKSPACE" >&2
    exit 1
  }
  [[ -f "${LR_BOOKARM_SDK_SLOTS:-}" && -f "${LR_BOOKARM_SDK_CAMERA_CALIBRATION:-}" ]] || {
    echo 'SDK 后端缺少槽位或相机外参配置；不会启动硬件运行时。' >&2
    exit 1
  }
  cd "$ROBOT_WORKSPACE"
  export PYTHONPATH="$ROBOT_WORKSPACE/src/robot_decision${PYTHONPATH:+:$PYTHONPATH}"
  sdk_args=(
    --port "$PORT"
    --sdk-root "$BOOKARM_ROOT"
    --book-stand-slots "$LR_BOOKARM_SDK_SLOTS"
    --camera-calibration "$LR_BOOKARM_SDK_CAMERA_CALIBRATION"
    --spine-storage-root "${LR_BOOKARM_SPINE_STORAGE:-/var/lib/lr-robot/book-spines}"
    --rod-device "${LR_BOOKARM_ROD_DEVICE:-/dev/rodmotor}"
    --lift-device "${LR_BOOKARM_LIFT_DEVICE:-/dev/lift_port}"
  )
  if [[ -n "${LR_BOOKARM_SDK_PICKUP_SLOTS:-}" ]]; then
    sdk_args+=(--book-stand-pickup-slots "$LR_BOOKARM_SDK_PICKUP_SLOTS")
  fi
  if [[ -n "${LR_BOOKARM_SDK_PROFILE:-}" ]]; then
    sdk_args+=(--take-book-profile "$LR_BOOKARM_SDK_PROFILE")
  fi
  if [[ -n "${LR_BOOKARM_SDK_RETURN_PROFILE:-}" ]]; then
    sdk_args+=(--return-book-profile "$LR_BOOKARM_SDK_RETURN_PROFILE")
  fi
  if [[ -n "${LR_BOOKARM_SDK_CAMERA_CONFIG:-}" ]]; then
    sdk_args+=(--camera-config "$LR_BOOKARM_SDK_CAMERA_CONFIG")
  fi
  exec "$PYTHON_BIN" -m robot_decision.bookarm_sdk_service "${sdk_args[@]}"
fi

cd "$BOOKARM_ROOT"
exec "$PYTHON_BIN" -m webui.backend.api \
  --host 127.0.0.1 \
  --port "$PORT" \
  --can "$CAN_INTERFACE" \
  --backend socketcan \
  --no-frontend \
  --log-level info
