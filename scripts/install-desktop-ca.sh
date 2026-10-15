#!/usr/bin/env bash
set -euo pipefail

CA_CERTIFICATE="${1:-}"
NSS_CERT_NICKNAME='LR Platform Control Station CA'

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 sudo 执行此脚本。" >&2
  exit 1
fi

if [[ -z "$CA_CERTIFICATE" || ! -r "$CA_CERTIFICATE" ]]; then
  echo "用法：sudo $0 /path/to/client-ca.crt" >&2
  exit 1
fi

command -v openssl >/dev/null 2>&1 || {
  echo "缺少 openssl。" >&2
  exit 1
}

openssl x509 -in "$CA_CERTIFICATE" -noout >/dev/null 2>&1 || {
  echo "指定文件不是有效的 X.509 PEM 证书。" >&2
  exit 1
}
openssl x509 -in "$CA_CERTIFICATE" -text -noout | grep -Fq 'CA:TRUE' || {
  echo "指定证书不是 CA 证书，拒绝安装。" >&2
  exit 1
}

if command -v update-ca-certificates >/dev/null 2>&1; then
  TARGET='/usr/local/share/ca-certificates/lr-platform-control-station.crt'
  install -m 0644 "$CA_CERTIFICATE" "$TARGET"
  update-ca-certificates
elif command -v update-ca-trust >/dev/null 2>&1; then
  TARGET='/etc/pki/ca-trust/source/anchors/lr-platform-control-station.crt'
  install -m 0644 "$CA_CERTIFICATE" "$TARGET"
  update-ca-trust extract
else
  echo "未识别系统 CA 信任库。请由系统管理员安装该 CA：$CA_CERTIFICATE" >&2
  exit 1
fi

echo "已安装控制站 CA：$TARGET"
openssl x509 -in "$CA_CERTIFICATE" -noout -subject -fingerprint -sha256

# Chromium/Electron on Linux uses the invoking operator's NSS database. The
# system CA store alone is not sufficient on all supported distributions.
DESKTOP_USER="${SUDO_USER:-}"
if [[ -z "$DESKTOP_USER" || "$DESKTOP_USER" == root ]]; then
  echo "未识别发起 sudo 的操作员账号，无法写入 Chromium/Electron 信任库。" >&2
  echo "请以普通操作员账号执行：sudo $0 $CA_CERTIFICATE" >&2
  exit 1
fi

if ! command -v certutil >/dev/null 2>&1; then
  echo "缺少 Chromium/Electron 证书工具 certutil。请先安装 libnss3-tools 后重新执行本脚本。" >&2
  exit 1
fi

DESKTOP_HOME="$(getent passwd "$DESKTOP_USER" | cut -d: -f6)"
DESKTOP_GROUP="$(id -gn "$DESKTOP_USER")"
if [[ -z "$DESKTOP_HOME" || ! -d "$DESKTOP_HOME" ]]; then
  echo "无法确定操作员 $DESKTOP_USER 的主目录。" >&2
  exit 1
fi

NSS_DATABASE="$DESKTOP_HOME/.pki/nssdb"
install -d -o "$DESKTOP_USER" -g "$DESKTOP_GROUP" -m 0700 "$NSS_DATABASE"
if [[ ! -f "$NSS_DATABASE/cert9.db" ]]; then
  runuser -u "$DESKTOP_USER" -- certutil -d "sql:$NSS_DATABASE" -N --empty-password
fi

# The control-station deployment directory is root-only. certutil runs as the
# desktop user, so give it a short-lived, user-readable copy of the public CA.
TEMP_CA="$(mktemp "$NSS_DATABASE/.lr-platform-ca.XXXXXX")"
trap 'rm -f "$TEMP_CA"' EXIT
install -o "$DESKTOP_USER" -g "$DESKTOP_GROUP" -m 0600 "$CA_CERTIFICATE" "$TEMP_CA"

# Replace this product's fixed nickname so a future CA rotation cannot leave
# Chromium trusting an obsolete root alongside the current one.
runuser -u "$DESKTOP_USER" -- certutil -d "sql:$NSS_DATABASE" -D -n "$NSS_CERT_NICKNAME" >/dev/null 2>&1 || true
runuser -u "$DESKTOP_USER" -- certutil -d "sql:$NSS_DATABASE" -A \
  -n "$NSS_CERT_NICKNAME" -t 'C,,' -i "$TEMP_CA"
runuser -u "$DESKTOP_USER" -- certutil -d "sql:$NSS_DATABASE" -L -n "$NSS_CERT_NICKNAME" >/dev/null

echo "已为操作员 $DESKTOP_USER 安装 Chromium/Electron 控制站 CA。"
echo "请完全退出并重新打开“智巡机器人调度”后再连接控制站。"
