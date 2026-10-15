#!/usr/bin/env bash
set -euo pipefail

umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_DIR="/etc/lr-platform"
ENV_FILE="$ENV_DIR/platform.env"
PKI_ENV_FILE="$ENV_DIR/platform-pki.env"
DISCOVERY_FILE="$ENV_DIR/discovery.json"
DISCOVERY_SERVICE="/etc/systemd/system/lr-station-discovery.service"
PLATFORM_MODE="${LR_PLATFORM_MODE:-production}"
STATION_NAME="${LR_STATION_NAME:-LR 控制站}"
STATION_URL="${LR_STATION_URL:-}"

usage() {
  cat <<'EOF'
安装 LR 控制站一体机

用法：
sudo ./scripts/install-control-station-appliance.sh \
    --station-url https://control-station.example [--station-name 名称]

此脚本会：
  1. 写入控制站设备身份与标准生产配置；
  2. 安装 lr-platform.service，使平台随开机自动启动；
  3. 安装仅在本局域网广播地址的发现服务；
  4. 输出操作员桌面端应连接的 HTTPS 地址。

执行前必须已完成 `export-station /etc/lr-platform`，并填写了数据库、MinIO、EMQX 管理员密码。
证书 SAN 必须包含 --station-url 中的域名或 IP。脚本不会生成、替换或导出私钥。
EOF
}

die() {
  echo "错误：$*" >&2
  exit 1
}

set_env_file_value() {
  local key="$1"
  local value="$2"
  local escaped temporary
  printf -v escaped '%q' "$value"
  temporary="$(mktemp "$ENV_DIR/platform.env.XXXXXX")"
  if [[ -f "$ENV_FILE" ]]; then
    awk -v key="$key" '$0 !~ "^" key "=" { print }' "$ENV_FILE" >"$temporary"
  fi
  printf '%s=%s\n' "$key" "$escaped" >>"$temporary"
  chown root:root "$temporary"
  chmod 0640 "$temporary"
  mv -f "$temporary" "$ENV_FILE"
}

require_file() {
  [[ -r "$1" ]] || die "缺少或无法读取：$1"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --station-url)
      STATION_URL="${2:-}"
      shift 2
      ;;
    --station-name)
      STATION_NAME="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "未知参数：$1"
      ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || die "请使用 sudo 执行此脚本。"
[[ "$PLATFORM_MODE" == production ]] || die "一体机交付仅支持 production 模式。"
[[ -n "$STATION_URL" ]] || die "必须提供 --station-url。"
[[ -n "$STATION_NAME" && ${#STATION_NAME} -le 120 ]] || die "控制站名称不能为空且最长 120 个字符。"
command -v docker >/dev/null 2>&1 || die "未检测到 Docker Engine 与 Docker Compose。"
command -v openssl >/dev/null 2>&1 || die "未检测到 openssl。"
docker info >/dev/null 2>&1 || die "Docker 服务未运行，或当前用户没有 Docker 使用权限。"

python3 - "$STATION_URL" <<'PY'
import sys
from urllib.parse import urlparse

url = urlparse(sys.argv[1])
if url.scheme != "https" or not url.netloc or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
    raise SystemExit("控制站地址必须是形如 https://control-station.example 的根 HTTPS 地址")
PY

install -d -o root -g root -m 0750 "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  die "缺少 $ENV_FILE；请先从 infra/control-station.env.example 创建并填写生产环境配置。"
fi

[[ -r "$PKI_ENV_FILE" ]] || die "缺少 $PKI_ENV_FILE；请先完成证书签发和 export-station。"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
# shellcheck disable=SC1090
source "$PKI_ENV_FILE"
set +a
for variable in \
  LR_POSTGRES_PASSWORD \
  LR_MINIO_ROOT_PASSWORD \
  LR_EMQX_DASHBOARD_USERNAME \
  LR_EMQX_DASHBOARD_PASSWORD \
  LR_EMQX_NODE_COOKIE \
  LR_MQTT_PASSWORD; do
  value="${!variable:-}"
  [[ -n "$value" && "$value" != replace-* && "$value" != change-me-before-production ]] || \
    die "请先在 $ENV_FILE 设置 $variable。"
done
require_file "${LR_WEB_TLS_CERTS_DIR:-}/web.crt"
require_file "${LR_WEB_TLS_CERTS_DIR:-}/web.key"
require_file "${LR_MQTT_CERTS_DIR:-}/ca.crt"
require_file "${LR_MQTT_CERTS_DIR:-}/server.crt"
require_file "${LR_MQTT_CERTS_DIR:-}/server.key"
require_file "${LR_EMQX_BOOTSTRAP_USERS_FILE:-}"

python3 - "$STATION_URL" "${LR_WEB_TLS_CERTS_DIR}/web.crt" <<'PY'
import subprocess
import sys
from urllib.parse import urlparse

url = urlparse(sys.argv[1])
certificate = sys.argv[2]
san = subprocess.run(
    ["openssl", "x509", "-in", certificate, "-noout", "-ext", "subjectAltName"],
    check=True,
    capture_output=True,
    text=True,
).stdout
marker = f"IP Address:{url.hostname}" if ":" not in url.hostname and url.hostname.replace(".", "").isdigit() else f"DNS:{url.hostname}"
if marker not in san:
    raise SystemExit(f"网页证书 SAN 不包含 {marker}，不能使用 {url.geturl()} 作为控制站地址")
PY

if [[ -r "$DISCOVERY_FILE" ]]; then
  STATION_ID="$(python3 - "$DISCOVERY_FILE" <<'PY'
import json
import sys

try:
    station_id = str(json.load(open(sys.argv[1], encoding="utf-8")).get("stationId", "")).strip()
except (OSError, ValueError, TypeError):
    station_id = ""
print(station_id)
PY
)"
fi
if [[ -z "${STATION_ID:-}" || ! "$STATION_ID" =~ ^lr-station-[a-f0-9-]{36}$ ]]; then
  STATION_ID="lr-station-$(cat /proc/sys/kernel/random/uuid)"
fi

# Keep a single canonical origin. Existing values are intentionally updated so
# re-running the installer after an address change cannot leave stale CORS.
set_env_file_value LR_PLATFORM_MODE production
set_env_file_value LR_STATION_NAME "$STATION_NAME"
set_env_file_value LR_STATION_ID "$STATION_ID"
set_env_file_value LR_STATION_URL "$STATION_URL"
set_env_file_value LR_CORS_ORIGINS "$STATION_URL"

python3 - "$DISCOVERY_FILE" "$STATION_ID" "$STATION_NAME" "$STATION_URL" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {"stationId": sys.argv[2], "stationName": sys.argv[3], "stationUrl": sys.argv[4]}
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")
os.chmod(temporary, 0o640)
temporary.replace(path)
PY

# Existing local Compose volumes may have been initialized with an old
# database password. Prepare runtime certificate access and migrate that role
# before the production service starts.
"$ROOT_DIR/scripts/repair-control-station-runtime.sh"
"$ROOT_DIR/scripts/install-host-start-agent.sh"

install -d -o root -g root -m 0755 /usr/local/libexec
install -m 0755 "$ROOT_DIR/scripts/lr-station-discovery.py" /usr/local/libexec/lr-station-discovery.py
cat >"$DISCOVERY_SERVICE" <<EOF
[Unit]
Description=LR 控制站局域网发现广播
After=network-online.target lr-platform.service
Wants=network-online.target
Requires=lr-platform.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/libexec/lr-station-discovery.py --config $DISCOVERY_FILE
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadOnlyPaths=$DISCOVERY_FILE
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
EOF

"$ROOT_DIR/scripts/install-platform-autostart.sh"
systemctl daemon-reload
systemctl enable --now lr-station-discovery.service
curl --fail --silent --show-error --cacert "$ENV_DIR/client-ca.crt" "$STATION_URL/health" >/dev/null || \
  die "控制站服务已启动但 HTTPS 健康检查失败；请检查证书、地址和 lr-platform.service 日志。"
echo "控制站一体机已就绪。操作员桌面端会自动发现：$STATION_NAME"
echo "维护连接地址：$STATION_URL"
