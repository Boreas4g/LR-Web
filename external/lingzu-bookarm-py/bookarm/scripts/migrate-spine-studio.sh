#!/usr/bin/env bash
# One-shot migration of the spine-studio store out of the code repository.
#
#   sudo scripts/migrate-spine-studio.sh [OLD_DIR] [NEW_DIR]
#
# Copies OLD_DIR (default recordings/spine_studio) into NEW_DIR (default
# /var/lib/lr-bookarm/spine_studio) with rsync, then verifies that every file
# arrived. The old directory is left untouched; rename it to
# recordings/spine_studio.migrated-bak yourself once the copy checks out, so
# the service (now defaulting to NEW_DIR) cannot keep writing into the repo.

set -euo pipefail

BOOKARM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OLD_DIR="${1:-$BOOKARM_ROOT/recordings/spine_studio}"
NEW_DIR="${2:-/var/lib/lr-bookarm/spine_studio}"
RUN_AS_USER="${LR_BOOKARM_USER:-${SUDO_USER:-$(id -un)}}"

[[ -d "$OLD_DIR" ]] || {
  echo "旧目录不存在：$OLD_DIR" >&2
  exit 1
}
id "$RUN_AS_USER" >/dev/null 2>&1 || {
  echo "运行用户不存在：$RUN_AS_USER" >&2
  exit 1
}

if [[ "$(id -u)" -eq 0 ]]; then
  install -d -o "$RUN_AS_USER" -g "$RUN_AS_USER" -m 0750 "$NEW_DIR"
else
  mkdir -p "$NEW_DIR"
fi

rsync -a "$OLD_DIR/" "$NEW_DIR/"
if [[ "$(id -u)" -eq 0 ]]; then
  chown -R "$RUN_AS_USER":"$RUN_AS_USER" "$NEW_DIR"
fi

old_count="$(cd "$OLD_DIR" && find . -type f | wc -l)"
new_count="$(cd "$NEW_DIR" && find . -type f | wc -l)"
if [[ "$old_count" != "$new_count" ]]; then
  echo "文件数不一致：旧 $old_count，新 $new_count；请排查后重跑。" >&2
  exit 1
fi
if ! diff <(cd "$OLD_DIR" && find . -type f | sort) <(cd "$NEW_DIR" && find . -type f | sort) >/dev/null; then
  echo "文件清单不一致：旧目录与新目录存在不同文件；请排查后重跑。" >&2
  exit 1
fi

echo "迁移完成：$OLD_DIR -> $NEW_DIR（$new_count 个文件，清单一致）。"
echo "确认服务读取新目录无误后，可执行：mv \"$OLD_DIR\" \"$OLD_DIR.migrated-bak\""
