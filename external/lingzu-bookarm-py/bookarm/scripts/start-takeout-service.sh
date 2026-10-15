#!/usr/bin/env bash
# Start the BookArm HTTP daemon for systemd or supervised local deployment.
# It deliberately binds a Unix socket: the Web container proxies that socket,
# while CAN/serial/camera controls remain unavailable on the LAN.

set -euo pipefail

BOOKARM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOKARM_ENV_FILE="${LR_BOOKARM_ENV_FILE:-/etc/lr-bookarm/bookarm.env}"
if [[ -r "$BOOKARM_ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$BOOKARM_ENV_FILE"
fi

PYTHON_BIN="${LR_BOOKARM_PYTHON:-/home/boreas/miniconda3/envs/bookarm/bin/python}"
SOCKET_PATH="${LR_BOOKARM_SOCKET_PATH:-/run/lr-bookarm/bookarm.sock}"
LEGACY_HOST="${LR_BOOKARM_LEGACY_TCP_HOST:-127.0.0.1}"
LEGACY_PORT="${LR_BOOKARM_LEGACY_TCP_PORT:-8090}"

[[ -x "$PYTHON_BIN" ]] || {
  echo "找不到 BookArm Python：$PYTHON_BIN" >&2
  exit 1
}

# The old hand-operated daemon owns the same CAN, camera and serial devices.
# Refuse before creating any hardware-backed object; do not silently replace
# or compete with it just because a systemd service was started.
if curl -fsS --max-time 1 \
  "http://${LEGACY_HOST}:${LEGACY_PORT}/api/v1/takeout/health" >/dev/null 2>&1; then
  echo "检测到旧 BookArm TCP 服务（${LEGACY_HOST}:${LEGACY_PORT}）；请在现场确认机械臂安全后停止旧服务，再启动 Unix socket 服务。" >&2
  exit 1
fi

cd "$BOOKARM_ROOT"
# 编译版 wheel 安装后，bookarm 包内的路径默认解析不再指向本仓库，
# 显式导出仓库根与同级推理目录（LR_BOOKARM_ROOT / LR_BOOKARM_INFERENCE_ROOT）
export LR_BOOKARM_ROOT="$BOOKARM_ROOT"
export LR_BOOKARM_INFERENCE_ROOT="${LR_BOOKARM_INFERENCE_ROOT:-$BOOKARM_ROOT/../book_spine_inference_light}"

TOKEN_ARGS=()
if [[ -n "${LR_BOOKARM_TOKEN:-}" ]]; then
  TOKEN_ARGS=(--token "$LR_BOOKARM_TOKEN")
else
  echo "警告：LR_BOOKARM_TOKEN 未设置（$BOOKARM_ENV_FILE 缺失、不可读或为空）；BookArm 接口将以无鉴权模式运行。" >&2
fi

# 书脊库是生产数据，默认存到代码仓库之外；YOLO 权重显式传入（默认与 bookarm
# 仓库同级的 book_spine_inference_light 检出；相对路径由 resolve_path 按仓库根解析）。
STUDIO_DIR="${LR_BOOKARM_STUDIO_DIR:-/var/lib/lr-bookarm/spine_studio}"
YOLO_WEIGHTS="${LR_BOOKARM_YOLO_WEIGHTS:-../book_spine_inference_light/weights/best.pt}"

exec "$PYTHON_BIN" -m bookarm.service \
  --unix-socket "$SOCKET_PATH" \
  --task-store-path "${LR_BOOKARM_TASK_STORE_PATH:-recordings/runtime/bookarm-tasks.json}" \
  --hardware-lock-path "${LR_BOOKARM_HARDWARE_LOCK_PATH:-recordings/runtime/bookarm-hardware.lock}" \
  --shutdown-timeout-s "${LR_BOOKARM_SHUTDOWN_TIMEOUT_S:-10}" \
  --studio-dir "$STUDIO_DIR" \
  --yolo-weights "$YOLO_WEIGHTS" \
  ${TOKEN_ARGS[@]+"${TOKEN_ARGS[@]}"} \
  "$@"
