#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ELECTRON_BIN="$ROOT_DIR/node_modules/electron/dist/electron"
SANDBOX_BIN="$ROOT_DIR/node_modules/electron/dist/chrome-sandbox"

if [[ ! -x "$ELECTRON_BIN" ]]; then
  echo "Electron 尚未安装。请先使用 Node.js 22 执行 npm install。" >&2
  exit 1
fi

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if (( NODE_MAJOR < 22 )); then
  echo "桌面开发需要 Node.js 22 或更高版本；当前版本：$(node --version)" >&2
  exit 1
fi

if [[ "$(stat -c '%U:%G:%a' "$SANDBOX_BIN")" == 'root:root:4755' ]]; then
  exec "$ELECTRON_BIN" "$ROOT_DIR/desktop/main.cjs" "$@"
fi

APPARMOR_RESTRICTS_USERNS=false
if [[ -r /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]] && \
  [[ "$(< /proc/sys/kernel/apparmor_restrict_unprivileged_userns)" == 1 ]]; then
  APPARMOR_RESTRICTS_USERNS=true
fi

if [[ "$APPARMOR_RESTRICTS_USERNS" == false ]] && \
  command -v unshare >/dev/null 2>&1 && unshare --user true 2>/dev/null; then
  exec "$ELECTRON_BIN" --disable-setuid-sandbox "$ROOT_DIR/desktop/main.cjs" "$@"
fi

cat >&2 <<EOF
当前系统禁用了普通用户 namespace，Electron 开发环境需要配置 SUID 沙箱：

  sudo chown root:root "$SANDBOX_BIN"
  sudo chmod 4755 "$SANDBOX_BIN"

完成后重新执行 npm run desktop:dev。脚本不会使用 --no-sandbox。
EOF
exit 1
