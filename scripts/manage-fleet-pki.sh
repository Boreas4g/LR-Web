#!/usr/bin/env bash
set -euo pipefail

umask 077

SCRIPT_NAME="$(basename "$0")"
PKI_DIR="${LR_PKI_DIR:-/var/lib/lr-platform/pki}"
CA_DAYS="${LR_PKI_CA_DAYS:-3650}"
CERT_DAYS="${LR_PKI_CERT_DAYS:-825}"

usage() {
  cat <<EOF
LR 车队证书管理

用法：
  sudo $SCRIPT_NAME [--pki-dir DIR] init-ca
  sudo $SCRIPT_NAME [--pki-dir DIR] issue-station <控制站域名> [控制站IP]
  sudo $SCRIPT_NAME [--pki-dir DIR] rotate-station <控制站域名> [控制站IP]
  sudo $SCRIPT_NAME [--pki-dir DIR] issue-robot <LR-机器人ID> [显示名称]
  sudo $SCRIPT_NAME [--pki-dir DIR] batch <robots.csv>
  sudo $SCRIPT_NAME [--pki-dir DIR] rotate-robot <LR-机器人ID>
  sudo $SCRIPT_NAME [--pki-dir DIR] verify
  sudo $SCRIPT_NAME [--pki-dir DIR] export-station [目标目录]
  sudo $SCRIPT_NAME [--pki-dir DIR] export-robot <LR-机器人ID> [目标目录]
  sudo $SCRIPT_NAME [--pki-dir DIR] sync-emqx [LR-机器人ID|all]

默认 PKI 目录：$PKI_DIR

robots.csv 格式：
  robot_id,name
  LR-01,智巡一号
  LR-02,智巡二号

说明：
  init-ca 只创建维护根 CA。ca.key 永远不会进入导出包。
  issue-station 为 MQTT Broker、Control API 和网页分别签发证书。
  rotate-station 归档并重签控制站叶子证书，不会更换根 CA 或机器人凭据。
  export-station 默认导出到 PKI 目录的 exports/control-station。
  export-robot 默认导出到 PKI 目录的 exports/robots/<机器人ID>。
EOF
}

die() {
  echo "错误：$*" >&2
  exit 1
}

info() {
  echo "[LR-PKI] $*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

validate_robot_id() {
  [[ "$1" =~ ^LR-[A-Za-z0-9_-]+$ ]] || die "机器人 ID '$1' 不合法，必须匹配 ^LR-[A-Za-z0-9_-]+$"
}

validate_dns_name() {
  local value="$1"
  [[ ${#value} -le 253 ]] || die "控制站域名过长"
  [[ "$value" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "控制站域名 '$value' 不合法"
  [[ "$value" != *..* ]] || die "控制站域名 '$value' 不合法"
}

validate_ip_address() {
  local value="$1"
  [[ "$value" =~ ^[0-9A-Fa-f:.]+$ ]] || die "控制站 IP '$value' 不合法"
  [[ "$value" == *.* || "$value" == *:* ]] || die "控制站 IP '$value' 不合法"
}

init_layout() {
  CA_DIR="$PKI_DIR/ca"
  STATE_DIR="$PKI_DIR/state"
  STATION_DIR="$PKI_DIR/station"
  ROBOTS_DIR="$PKI_DIR/robots"
  ARCHIVE_DIR="$PKI_DIR/archive"
  EXPORT_DIR="$PKI_DIR/exports"
  USERS_FILE="$STATE_DIR/users.tsv"
  ROBOTS_FILE="$STATE_DIR/robots.tsv"
  STATION_FILE="$STATE_DIR/station.tsv"
  EMQX_USERS_FILE="$PKI_DIR/emqx-users.csv"

  install -d -m 0700 "$PKI_DIR" "$CA_DIR" "$STATE_DIR" "$STATION_DIR" \
    "$ROBOTS_DIR" "$ARCHIVE_DIR" "$EXPORT_DIR"
  touch "$PKI_DIR/.lock"
  chmod 0600 "$PKI_DIR/.lock"
}

require_ca() {
  [[ -s "$CA_DIR/ca.crt" && -s "$CA_DIR/ca.key" ]] || \
    die "尚未初始化根 CA，请先执行 init-ca"
}

random_password() {
  openssl rand -hex 24
}

user_exists() {
  local username="$1"
  [[ -f "$USERS_FILE" ]] && awk -F '\t' -v username="$username" '$1 == username { found=1 } END { exit !found }' "$USERS_FILE"
}

get_password() {
  local username="$1"
  awk -F '\t' -v username="$username" '$1 == username { print $2; found=1; exit } END { exit !found }' "$USERS_FILE"
}

set_user_password() {
  local username="$1"
  local password="$2"
  local temp_file
  temp_file="$(mktemp "$STATE_DIR/users.XXXXXX")"
  if [[ -f "$USERS_FILE" ]]; then
    awk -F '\t' -v username="$username" '$1 != username' "$USERS_FILE" >"$temp_file"
  fi
  printf '%s\t%s\n' "$username" "$password" >>"$temp_file"
  chmod 0600 "$temp_file"
  mv -f "$temp_file" "$USERS_FILE"
}

set_robot_inventory() {
  local robot_id="$1"
  local display_name="$2"
  local temp_file
  temp_file="$(mktemp "$STATE_DIR/robots.XXXXXX")"
  if [[ -f "$ROBOTS_FILE" ]]; then
    awk -F '\t' -v robot_id="$robot_id" '$1 != robot_id' "$ROBOTS_FILE" >"$temp_file"
  fi
  printf '%s\t%s\n' "$robot_id" "$display_name" >>"$temp_file"
  chmod 0600 "$temp_file"
  mv -f "$temp_file" "$ROBOTS_FILE"
}

robot_display_name() {
  local robot_id="$1"
  awk -F '\t' -v robot_id="$robot_id" '$1 == robot_id { print $2; found=1; exit } END { exit !found }' "$ROBOTS_FILE"
}

render_emqx_users() {
  local temp_file
  temp_file="$(mktemp "$PKI_DIR/emqx-users.XXXXXX")"
  printf 'user_id,password,is_superuser\n' >"$temp_file"
  if [[ -f "$USERS_FILE" ]]; then
    sort -t $'\t' -k1,1 "$USERS_FILE" | while IFS=$'\t' read -r username password; do
      [[ -n "$username" && -n "$password" ]] || continue
      printf '%s,%s,false\n' "$username" "$password"
    done >>"$temp_file"
  fi
  chmod 0600 "$temp_file"
  mv -f "$temp_file" "$EMQX_USERS_FILE"
}

write_extension_file() {
  local output="$1"
  local purpose="$2"
  local san="${3:-}"
  {
    echo 'basicConstraints=critical,CA:FALSE'
    echo 'keyUsage=critical,digitalSignature,keyEncipherment'
    echo "extendedKeyUsage=$purpose"
    [[ -z "$san" ]] || echo "subjectAltName=$san"
    echo 'subjectKeyIdentifier=hash'
    echo 'authorityKeyIdentifier=keyid,issuer'
  } >"$output"
}

sign_leaf() {
  local common_name="$1"
  local purpose="$2"
  local san="$3"
  local output_cert="$4"
  local output_key="$5"
  local work_dir request_file extension_file

  work_dir="$(mktemp -d "$PKI_DIR/sign.XXXXXX")"
  request_file="$work_dir/request.csr"
  extension_file="$work_dir/extensions.conf"
  trap 'rm -rf "$work_dir"' RETURN

  openssl req -new -newkey rsa:3072 -nodes -sha256 \
    -subj "/O=LR Platform/CN=$common_name" \
    -keyout "$work_dir/private.key" -out "$request_file" >/dev/null 2>&1
  write_extension_file "$extension_file" "$purpose" "$san"
  openssl x509 -req -sha256 -days "$CERT_DAYS" \
    -in "$request_file" -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" \
    -CAserial "$CA_DIR/ca.srl" -CAcreateserial \
    -extfile "$extension_file" -out "$work_dir/certificate.crt" >/dev/null 2>&1

  install -m 0644 "$work_dir/certificate.crt" "$output_cert"
  install -m 0600 "$work_dir/private.key" "$output_key"
  rm -rf "$work_dir"
  trap - RETURN
}

command_init_ca() {
  [[ ! -e "$CA_DIR/ca.key" && ! -e "$CA_DIR/ca.crt" ]] || \
    die "根 CA 已存在；脚本不会覆盖它：$CA_DIR"

  local work_dir
  work_dir="$(mktemp -d "$PKI_DIR/ca-init.XXXXXX")"
  trap 'rm -rf "$work_dir"' RETURN
  openssl req -x509 -new -newkey rsa:4096 -nodes -sha256 -days "$CA_DAYS" \
    -subj '/O=LR Platform/CN=LR Fleet Root CA' \
    -addext 'basicConstraints=critical,CA:TRUE,pathlen:1' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -addext 'subjectKeyIdentifier=hash' \
    -keyout "$work_dir/ca.key" -out "$work_dir/ca.crt" >/dev/null 2>&1
  install -m 0600 "$work_dir/ca.key" "$CA_DIR/ca.key"
  install -m 0644 "$work_dir/ca.crt" "$CA_DIR/ca.crt"
  rm -rf "$work_dir"
  trap - RETURN
  info "根 CA 已创建：$CA_DIR"
  info "请离线备份 ca.key；不要把它复制到控制站运行目录或机器人。"
}

command_issue_station() {
  local station_dns="${1:-}"
  local station_ip="${2:-}"
  [[ -n "$station_dns" ]] || die "issue-station 需要控制站域名"
  validate_dns_name "$station_dns"
  [[ -z "$station_ip" ]] || validate_ip_address "$station_ip"
  require_ca

  [[ ! -e "$STATION_DIR/mqtt/server.crt" && ! -e "$STATION_FILE" ]] || \
    die "控制站凭据已存在；脚本不会直接覆盖。若需更换站点证书，请建立新的 PKI 维护窗口。"
  user_exists 'lr-control-api' && die "lr-control-api 账号已存在，状态不一致，请先执行 verify"

  local work_dir san control_password
  work_dir="$(mktemp -d "$PKI_DIR/station-issue.XXXXXX")"
  install -d -m 0700 "$work_dir/mqtt" "$work_dir/web"
  san="DNS:emqx,DNS:$station_dns"
  [[ -z "$station_ip" ]] || san="$san,IP:$station_ip"

  sign_leaf "$station_dns MQTT" 'serverAuth' "$san" \
    "$work_dir/mqtt/server.crt" "$work_dir/mqtt/server.key"
  sign_leaf 'lr-control-api' 'clientAuth' '' \
    "$work_dir/mqtt/control-api.crt" "$work_dir/mqtt/control-api.key"
  sign_leaf "$station_dns" 'serverAuth' "${san#DNS:emqx,}" \
    "$work_dir/web/web.crt" "$work_dir/web/web.key"
  install -m 0644 "$CA_DIR/ca.crt" "$work_dir/mqtt/ca.crt"

  install -d -m 0700 "$STATION_DIR/mqtt" "$STATION_DIR/web"
  install -m 0644 "$work_dir/mqtt/ca.crt" "$STATION_DIR/mqtt/ca.crt"
  install -m 0644 "$work_dir/mqtt/server.crt" "$STATION_DIR/mqtt/server.crt"
  install -m 0600 "$work_dir/mqtt/server.key" "$STATION_DIR/mqtt/server.key"
  install -m 0644 "$work_dir/mqtt/control-api.crt" "$STATION_DIR/mqtt/control-api.crt"
  install -m 0600 "$work_dir/mqtt/control-api.key" "$STATION_DIR/mqtt/control-api.key"
  install -m 0644 "$work_dir/web/web.crt" "$STATION_DIR/web/web.crt"
  install -m 0600 "$work_dir/web/web.key" "$STATION_DIR/web/web.key"
  rm -rf "$work_dir"

  printf '%s\t%s\n' "$station_dns" "$station_ip" >"$STATION_FILE"
  chmod 0600 "$STATION_FILE"
  control_password="$(random_password)"
  set_user_password 'lr-control-api' "$control_password"
  render_emqx_users
  info "控制站证书已签发：DNS=$station_dns${station_ip:+, IP=$station_ip}"
  info "执行 export-station 生成控制站部署包。"
}

command_rotate_station() {
  local station_dns="${1:-}"
  local station_ip="${2:-}"
  [[ -n "$station_dns" ]] || die "rotate-station 需要控制站域名"
  validate_dns_name "$station_dns"
  [[ -z "$station_ip" ]] || validate_ip_address "$station_ip"
  require_ca
  [[ -s "$STATION_DIR/mqtt/server.crt" && -s "$STATION_DIR/web/web.crt" && -s "$STATION_FILE" ]] || \
    die "控制站凭据尚未签发；请使用 issue-station"
  user_exists 'lr-control-api' || die "缺少 lr-control-api 账号，状态不一致，请先执行 verify"

  local timestamp archive_dir old_dns old_ip work_dir san
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive_dir="$ARCHIVE_DIR/station/$timestamp"
  [[ ! -e "$archive_dir" ]] || die "归档目录已存在，请稍后重试：$archive_dir"
  read -r old_dns old_ip <"$STATION_FILE" || die "无法读取现有控制站状态"
  work_dir="$(mktemp -d "$PKI_DIR/station-rotate.XXXXXX")"
  trap 'rm -rf "$work_dir"' RETURN
  install -d -m 0700 "$work_dir/mqtt" "$work_dir/web" "$archive_dir"

  # Build all new credentials before replacing the active station files.
  san="DNS:emqx,DNS:$station_dns"
  [[ -z "$station_ip" ]] || san="$san,IP:$station_ip"
  sign_leaf "$station_dns MQTT" 'serverAuth' "$san" \
    "$work_dir/mqtt/server.crt" "$work_dir/mqtt/server.key"
  sign_leaf 'lr-control-api' 'clientAuth' '' \
    "$work_dir/mqtt/control-api.crt" "$work_dir/mqtt/control-api.key"
  sign_leaf "$station_dns" 'serverAuth' "${san#DNS:emqx,}" \
    "$work_dir/web/web.crt" "$work_dir/web/web.key"
  install -m 0644 "$CA_DIR/ca.crt" "$work_dir/mqtt/ca.crt"

  cp -a "$STATION_DIR/." "$archive_dir/"
  install -m 0644 "$work_dir/mqtt/ca.crt" "$STATION_DIR/mqtt/ca.crt"
  install -m 0644 "$work_dir/mqtt/server.crt" "$STATION_DIR/mqtt/server.crt"
  install -m 0600 "$work_dir/mqtt/server.key" "$STATION_DIR/mqtt/server.key"
  install -m 0644 "$work_dir/mqtt/control-api.crt" "$STATION_DIR/mqtt/control-api.crt"
  install -m 0600 "$work_dir/mqtt/control-api.key" "$STATION_DIR/mqtt/control-api.key"
  install -m 0644 "$work_dir/web/web.crt" "$STATION_DIR/web/web.crt"
  install -m 0600 "$work_dir/web/web.key" "$STATION_DIR/web/web.key"
  printf '%s\t%s\n' "$station_dns" "$station_ip" >"$STATION_FILE"
  chmod 0600 "$STATION_FILE"
  rm -rf "$work_dir"
  trap - RETURN

  info "控制站叶子证书已轮换：DNS=$station_dns${station_ip:+, IP=$station_ip}"
  info "旧控制站凭据已归档：$archive_dir（DNS=$old_dns${old_ip:+, IP=$old_ip}）"
  info "请立即执行 export-station 并重新启动控制站平台。"
}

issue_robot_internal() {
  local robot_id="$1"
  local display_name="$2"
  local allow_existing="${3:-false}"
  validate_robot_id "$robot_id"
  require_ca

  if [[ "$allow_existing" != true ]]; then
    [[ ! -e "$ROBOTS_DIR/$robot_id/$robot_id.crt" ]] || \
      die "$robot_id 已签发；如需更换凭据请执行 rotate-robot $robot_id"
    user_exists "$robot_id" && die "$robot_id 的账号已存在，状态不一致，请先执行 verify"
  fi

  local work_dir password
  work_dir="$(mktemp -d "$PKI_DIR/robot-issue.XXXXXX")"
  sign_leaf "$robot_id" 'clientAuth' '' \
    "$work_dir/$robot_id.crt" "$work_dir/$robot_id.key"
  install -m 0644 "$CA_DIR/ca.crt" "$work_dir/ca.crt"
  password="$(random_password)"
  printf 'mqtt_username=%s\nmqtt_password=%s\n' "$robot_id" "$password" >"$work_dir/mqtt-credentials.env"
  chmod 0600 "$work_dir/mqtt-credentials.env"

  install -d -m 0700 "$ROBOTS_DIR/$robot_id"
  install -m 0644 "$work_dir/ca.crt" "$ROBOTS_DIR/$robot_id/ca.crt"
  install -m 0644 "$work_dir/$robot_id.crt" "$ROBOTS_DIR/$robot_id/$robot_id.crt"
  install -m 0600 "$work_dir/$robot_id.key" "$ROBOTS_DIR/$robot_id/$robot_id.key"
  install -m 0600 "$work_dir/mqtt-credentials.env" "$ROBOTS_DIR/$robot_id/mqtt-credentials.env"
  rm -rf "$work_dir"

  set_user_password "$robot_id" "$password"
  set_robot_inventory "$robot_id" "$display_name"
  render_emqx_users
}

command_issue_robot() {
  local robot_id="${1:-}"
  local display_name="${2:-${1:-}}"
  [[ -n "$robot_id" ]] || die "issue-robot 需要机器人 ID"
  [[ "$display_name" != *$'\t'* && "$display_name" != *$'\n'* && "$display_name" != *$'\r'* ]] || \
    die "显示名称不能包含制表符或换行"
  issue_robot_internal "$robot_id" "$display_name"
  info "$robot_id 凭据已签发。执行 export-robot $robot_id 生成机器人交付包。"
}

command_batch() {
  local csv_file="${1:-}"
  [[ -n "$csv_file" && -r "$csv_file" ]] || die "batch 需要可读取的 robots.csv"
  require_ca

  local line_number=0 robot_id display_name extra issued=0
  local -A seen_robot_ids=()

  # Reject malformed or duplicate rows before any certificate is issued. This
  # lets an operator correct a CSV without leaving a partially created fleet.
  while IFS=, read -r robot_id display_name extra || [[ -n "${robot_id:-}${display_name:-}${extra:-}" ]]; do
    line_number=$((line_number + 1))
    robot_id="${robot_id%$'\r'}"
    display_name="${display_name%$'\r'}"
    extra="${extra%$'\r'}"
    [[ $line_number -ne 1 || "$robot_id" != 'robot_id' ]] || continue
    [[ -n "$robot_id" ]] || continue
    [[ "$robot_id" != \#* ]] || continue
    [[ -z "$extra" ]] || die "$csv_file:$line_number 包含多余列；名称中不能包含逗号"
    validate_robot_id "$robot_id"
    [[ -z "${seen_robot_ids[$robot_id]:-}" ]] || die "$csv_file:$line_number 的机器人 ID 重复：$robot_id"
    seen_robot_ids[$robot_id]=1
    [[ ! -e "$ROBOTS_DIR/$robot_id/$robot_id.crt" ]] || \
      die "$robot_id 已签发；请从清单移除它或改用 rotate-robot"
    user_exists "$robot_id" && die "$robot_id 的账号已存在，状态不一致，请先执行 verify"
    [[ "$display_name" != *$'\t'* && "$display_name" != *$'\n'* && "$display_name" != *$'\r'* ]] || \
      die "$csv_file:$line_number 的显示名称不能包含制表符或换行"
  done <"$csv_file"

  line_number=0
  while IFS=, read -r robot_id display_name extra || [[ -n "${robot_id:-}${display_name:-}${extra:-}" ]]; do
    line_number=$((line_number + 1))
    robot_id="${robot_id%$'\r'}"
    display_name="${display_name%$'\r'}"
    extra="${extra%$'\r'}"
    [[ $line_number -ne 1 || "$robot_id" != 'robot_id' ]] || continue
    [[ -n "$robot_id" ]] || continue
    [[ "$robot_id" != \#* ]] || continue
    [[ -z "$extra" ]] || die "$csv_file:$line_number 包含多余列；名称中不能包含逗号"
    [[ -n "$display_name" ]] || display_name="$robot_id"
    command_issue_robot "$robot_id" "$display_name"
    issued=$((issued + 1))
  done <"$csv_file"
  info "批量签发完成，共 $issued 台机器人。"
}

command_rotate_robot() {
  local robot_id="${1:-}"
  [[ -n "$robot_id" ]] || die "rotate-robot 需要机器人 ID"
  validate_robot_id "$robot_id"
  require_ca
  [[ -d "$ROBOTS_DIR/$robot_id" ]] || die "$robot_id 尚未签发"
  user_exists "$robot_id" || die "$robot_id 的账号不存在，状态不一致，请先执行 verify"

  local timestamp archive_dir display_name
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive_dir="$ARCHIVE_DIR/robots/$robot_id/$timestamp"
  [[ ! -e "$archive_dir" ]] || die "归档目录已存在，请稍后重试：$archive_dir"
  install -d -m 0700 "$archive_dir"
  cp -a "$ROBOTS_DIR/$robot_id/." "$archive_dir/"
  display_name="$(robot_display_name "$robot_id" 2>/dev/null || printf '%s' "$robot_id")"
  issue_robot_internal "$robot_id" "$display_name" true
  info "$robot_id 已轮换，旧凭据归档于：$archive_dir"
  info "请重新导出并更新机器人；运行中的 EMQX 还需同步新密码，详见部署文档。"
}

verify_certificate() {
  local cert_file="$1"
  local expected_purpose="$2"
  openssl verify -CAfile "$CA_DIR/ca.crt" "$cert_file" >/dev/null
  openssl x509 -in "$cert_file" -checkend 86400 -noout >/dev/null || \
    die "证书将在 24 小时内过期：$cert_file"
  openssl x509 -in "$cert_file" -purpose -noout | \
    grep -Fq "$expected_purpose : Yes" || die "证书用途不正确：$cert_file"
}

verify_key_pair() {
  local cert_file="$1"
  local key_file="$2"
  local cert_digest key_digest
  cert_digest="$(openssl x509 -in "$cert_file" -pubkey -noout | \
    openssl pkey -pubin -outform DER 2>/dev/null | openssl dgst -sha256)"
  key_digest="$(openssl pkey -in "$key_file" -pubout -outform DER 2>/dev/null | \
    openssl dgst -sha256)"
  [[ "$cert_digest" == "$key_digest" ]] || die "证书与私钥不匹配：$cert_file"
}

command_verify() {
  require_ca
  openssl x509 -in "$CA_DIR/ca.crt" -checkend 86400 -noout >/dev/null || die "根 CA 将在 24 小时内过期"
  openssl pkey -in "$CA_DIR/ca.key" -check -noout >/dev/null

  if [[ -d "$STATION_DIR/mqtt" ]]; then
    verify_certificate "$STATION_DIR/mqtt/server.crt" 'SSL server'
    verify_certificate "$STATION_DIR/mqtt/control-api.crt" 'SSL client'
    verify_certificate "$STATION_DIR/web/web.crt" 'SSL server'
    verify_key_pair "$STATION_DIR/mqtt/server.crt" "$STATION_DIR/mqtt/server.key"
    verify_key_pair "$STATION_DIR/mqtt/control-api.crt" "$STATION_DIR/mqtt/control-api.key"
    verify_key_pair "$STATION_DIR/web/web.crt" "$STATION_DIR/web/web.key"
    user_exists 'lr-control-api' || die "缺少 lr-control-api MQTT 账号"
  fi

  local robot_dir robot_id cert_count=0 user_count=0
  shopt -s nullglob
  for robot_dir in "$ROBOTS_DIR"/LR-*; do
    [[ -d "$robot_dir" ]] || continue
    robot_id="$(basename "$robot_dir")"
    validate_robot_id "$robot_id"
    verify_certificate "$robot_dir/$robot_id.crt" 'SSL client'
    verify_key_pair "$robot_dir/$robot_id.crt" "$robot_dir/$robot_id.key"
    [[ -s "$robot_dir/$robot_id.key" && -s "$robot_dir/mqtt-credentials.env" ]] || \
      die "$robot_id 的私钥或密码文件缺失"
    user_exists "$robot_id" || die "$robot_id 缺少 MQTT 账号"
    cert_count=$((cert_count + 1))
  done
  shopt -u nullglob

  if [[ -f "$USERS_FILE" ]]; then
    user_count="$(wc -l <"$USERS_FILE")"
  fi
  render_emqx_users
  info "验证通过：$cert_count 台机器人，$user_count 个 MQTT 账号。"
}

command_export_station() {
  local destination="${1:-$EXPORT_DIR/control-station}"
  [[ -s "$STATION_DIR/mqtt/server.crt" && -s "$STATION_DIR/web/web.crt" ]] || \
    die "控制站凭据尚未签发"
  user_exists 'lr-control-api' || die "缺少 lr-control-api MQTT 账号"
  render_emqx_users

  install -d -m 0700 "$destination" "$destination/mqtt-certs" "$destination/web-certs"
  install -m 0644 "$STATION_DIR/mqtt/ca.crt" "$destination/mqtt-certs/ca.crt"
  install -m 0644 "$STATION_DIR/mqtt/server.crt" "$destination/mqtt-certs/server.crt"
  install -m 0600 "$STATION_DIR/mqtt/server.key" "$destination/mqtt-certs/server.key"
  install -m 0644 "$STATION_DIR/mqtt/control-api.crt" "$destination/mqtt-certs/control-api.crt"
  install -m 0600 "$STATION_DIR/mqtt/control-api.key" "$destination/mqtt-certs/control-api.key"
  install -m 0644 "$STATION_DIR/web/web.crt" "$destination/web-certs/web.crt"
  install -m 0600 "$STATION_DIR/web/web.key" "$destination/web-certs/web.key"
  install -m 0600 "$EMQX_USERS_FILE" "$destination/emqx-users.csv"
  install -m 0644 "$CA_DIR/ca.crt" "$destination/client-ca.crt"
  {
    printf 'LR_MQTT_USERNAME=lr-control-api\n'
    printf 'LR_MQTT_PASSWORD=%s\n' "$(get_password 'lr-control-api')"
    printf 'LR_MQTT_CERTS_DIR=%q\n' "$destination/mqtt-certs"
    printf 'LR_WEB_TLS_CERTS_DIR=%q\n' "$destination/web-certs"
    printf 'LR_EMQX_BOOTSTRAP_USERS_FILE=%q\n' "$destination/emqx-users.csv"
  } >"$destination/platform-pki.env"
  chmod 0600 "$destination/platform-pki.env"
  info "控制站部署包已导出：$destination"
  info "包内不含根 CA 私钥。platform-pki.env 中的变量需合并到 /etc/lr-platform/platform.env。"
}

command_export_robot() {
  local robot_id="${1:-}"
  local destination="${2:-}"
  [[ -n "$robot_id" ]] || die "export-robot 需要机器人 ID"
  validate_robot_id "$robot_id"
  [[ -d "$ROBOTS_DIR/$robot_id" ]] || die "$robot_id 尚未签发"
  [[ -n "$destination" ]] || destination="$EXPORT_DIR/robots/$robot_id"

  install -d -m 0700 "$destination" "$destination/mqtt"
  install -m 0644 "$ROBOTS_DIR/$robot_id/ca.crt" "$destination/mqtt/ca.crt"
  install -m 0644 "$ROBOTS_DIR/$robot_id/$robot_id.crt" "$destination/mqtt/$robot_id.crt"
  install -m 0600 "$ROBOTS_DIR/$robot_id/$robot_id.key" "$destination/mqtt/$robot_id.key"
  install -m 0600 "$ROBOTS_DIR/$robot_id/mqtt-credentials.env" "$destination/mqtt-credentials.env"
  info "$robot_id 交付包已导出：$destination"
  info "在机器人上将 mqtt/ 安装到 /etc/lr-robot/mqtt，并把账号密码写入 edge-gateway.yaml。"
}

command_sync_emqx() {
  local target="${1:-all}"
  [[ "$target" == all ]] || validate_robot_id "$target"
  require_command curl
  require_command jq
  [[ -s "$USERS_FILE" ]] || die "尚未生成 MQTT 账号"

  local username password dashboard_url authenticator_id login_payload token payload status
  local platform_env_file pki_env_file
  local header_file response_file
  platform_env_file="${LR_PLATFORM_ENV_FILE:-/etc/lr-platform/platform.env}"
  pki_env_file="${LR_PLATFORM_PKI_ENV_FILE:-/etc/lr-platform/platform-pki.env}"
  if [[ -r "$platform_env_file" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$platform_env_file"
    [[ ! -r "$pki_env_file" ]] || source "$pki_env_file"
    set +a
  fi
  [[ -n "${LR_EMQX_DASHBOARD_USERNAME:-}" && -n "${LR_EMQX_DASHBOARD_PASSWORD:-}" ]] || \
    die "缺少 LR_EMQX_DASHBOARD_USERNAME 或 LR_EMQX_DASHBOARD_PASSWORD"

  dashboard_url="http://127.0.0.1:${LR_EMQX_DASHBOARD_PORT:-18083}"
  authenticator_id='password_based:built_in_database'
  header_file="$(mktemp "$PKI_DIR/emqx-header.XXXXXX")"
  response_file="$(mktemp "$PKI_DIR/emqx-response.XXXXXX")"
  trap 'rm -f "$header_file" "$response_file"' RETURN
  login_payload="$(jq -cn \
    --arg username "$LR_EMQX_DASHBOARD_USERNAME" \
    --arg password "$LR_EMQX_DASHBOARD_PASSWORD" \
    '{username: $username, password: $password}')"
  token="$(curl --fail --silent --show-error --max-time 10 \
    -H 'Content-Type: application/json' --data-binary @- \
    "$dashboard_url/api/v5/login" <<<"$login_payload" | jq -er '.token')" || \
    die "无法登录本机 EMQX 管理 API"
  printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$token" >"$header_file"

  while IFS=$'\t' read -r username password; do
    [[ -n "$username" && -n "$password" ]] || continue
    if [[ "$target" != all && "$username" != "$target" ]]; then
      continue
    fi
    payload="$(jq -cn --arg password "$password" \
      '{password: $password, is_superuser: false}')"
    status="$(curl --silent --show-error --max-time 10 -o "$response_file" -w '%{http_code}' \
      -X PUT -H @"$header_file" --data-binary @- \
      "$dashboard_url/api/v5/authentication/$authenticator_id/users/$username" \
      <<<"$payload")"
    if [[ "$status" == 200 || "$status" == 204 ]]; then
      info "已更新 EMQX 账号：$username"
      continue
    fi
    [[ "$status" == 404 ]] || die "更新 EMQX 账号 $username 失败（HTTP $status）：$(<"$response_file")"

    payload="$(jq -cn --arg user_id "$username" --arg password "$password" \
      '{user_id: $user_id, password: $password, is_superuser: false}')"
    status="$(curl --silent --show-error --max-time 10 -o "$response_file" -w '%{http_code}' \
      -X POST -H @"$header_file" --data-binary @- \
      "$dashboard_url/api/v5/authentication/$authenticator_id/users" \
      <<<"$payload")"
    [[ "$status" == 200 || "$status" == 201 || "$status" == 204 ]] || \
      die "新增 EMQX 账号 $username 失败（HTTP $status）：$(<"$response_file")"
    info "已新增 EMQX 账号：$username"
  done <"$USERS_FILE"
  rm -f "$header_file" "$response_file"
  trap - RETURN
}

while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --pki-dir)
      [[ -n "${2:-}" ]] || die "--pki-dir 需要目录"
      PKI_DIR="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *) die "未知选项：$1" ;;
  esac
done

command="${1:-}"
[[ -n "$command" ]] || {
  usage
  exit 1
}
shift

require_command openssl
require_command flock
require_command awk
require_command install
require_command mktemp
init_layout

# Serial numbers, account files and archive directories are shared state.
# A single exclusive lock protects every command, including batch issuance.
exec 9>"$PKI_DIR/.lock"
flock -x 9

case "$command" in
  init-ca) command_init_ca "$@" ;;
  issue-station) command_issue_station "$@" ;;
  rotate-station) command_rotate_station "$@" ;;
  issue-robot) command_issue_robot "$@" ;;
  batch) command_batch "$@" ;;
  rotate-robot) command_rotate_robot "$@" ;;
  verify) command_verify "$@" ;;
  export-station) command_export_station "$@" ;;
  export-robot) command_export_robot "$@" ;;
  sync-emqx) command_sync_emqx "$@" ;;
  help|-h|--help) usage ;;
  *)
    usage >&2
    die "未知命令：$command"
    ;;
esac
