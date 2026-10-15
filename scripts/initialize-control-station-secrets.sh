#!/usr/bin/env bash
set -euo pipefail

umask 077

ENV_FILE="/etc/lr-platform/platform.env"
HANDOFF_DIR="/root/lr-platform-maintenance"
HANDOFF_FILE="$HANDOFF_DIR/initial-secrets.txt"

die() {
  echo "错误：$*" >&2
  exit 1
}

set_env_value() {
  local key="$1"
  local value="$2"
  local temporary
  temporary="$(mktemp "${ENV_FILE}.XXXXXX")"
  awk -v key="$key" '$0 !~ "^" key "=" { print }' "$ENV_FILE" >"$temporary"
  printf '%s=%s\n' "$key" "$value" >>"$temporary"
  chown root:root "$temporary"
  chmod 0640 "$temporary"
  mv -f "$temporary" "$ENV_FILE"
}

[[ "$(id -u)" -eq 0 ]] || die "请使用 sudo 执行此脚本。"
[[ -f "$ENV_FILE" ]] || die "缺少 $ENV_FILE；请先复制 infra/control-station.env.example。"
command -v openssl >/dev/null 2>&1 || die "缺少 openssl。"

if [[ -e "$HANDOFF_FILE" ]]; then
  die "$HANDOFF_FILE 已存在；为防止意外轮换秘密，脚本不会覆盖它。"
fi

postgres_password="$(openssl rand -hex 32)"
minio_password="$(openssl rand -hex 32)"
emqx_dashboard_password="$(openssl rand -hex 32)"
emqx_node_cookie="$(openssl rand -hex 32)"

set_env_value LR_POSTGRES_PASSWORD "$postgres_password"
set_env_value LR_MINIO_ROOT_PASSWORD "$minio_password"
set_env_value LR_EMQX_DASHBOARD_USERNAME "lr-admin"
set_env_value LR_EMQX_DASHBOARD_PASSWORD "$emqx_dashboard_password"
set_env_value LR_EMQX_NODE_COOKIE "$emqx_node_cookie"

install -d -o root -g root -m 0700 "$HANDOFF_DIR"
temporary="$(mktemp "$HANDOFF_DIR/initial-secrets.XXXXXX")"
cat >"$temporary" <<EOF
LR 控制站初始维护秘密
生成时间：$(date -u +%Y-%m-%dT%H:%M:%SZ)

LR_POSTGRES_PASSWORD=$postgres_password
LR_MINIO_ROOT_PASSWORD=$minio_password
LR_EMQX_DASHBOARD_USERNAME=lr-admin
LR_EMQX_DASHBOARD_PASSWORD=$emqx_dashboard_password
LR_EMQX_NODE_COOKIE=$emqx_node_cookie
EOF
chown root:root "$temporary"
chmod 0600 "$temporary"
mv -f "$temporary" "$HANDOFF_FILE"

echo "已生成并保存四个独立秘密。"
echo "维护清单：$HANDOFF_FILE（root:root，0600）。"
echo "请立即将其中的 lr-admin 密码保存到受控密码管理器，然后删除或离线加密保存该文件。"
