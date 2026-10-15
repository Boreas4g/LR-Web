#!/usr/bin/env bash
# LR 平台一键启动脚本（幂等）。
#
# This is deliberately a host-side command.  It can operate on CAN, systemd
# and Docker; the containerised control-api endpoint reports 503 instead of
# pretending it has those host capabilities.
# Every component emits: STEP <name> <OK|STARTED|FAILED|SKIPPED> <message>

set -u

LR_WEB_ROOT="${LR_WEB_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BOOKARM_ROOT="${LR_BOOKARM_ROOT:-$LR_WEB_ROOT/../../lingzu_arm/lingzu-bookarm-py/bookarm}"
BOOKARM_SERVICE="${LR_BOOKARM_SYSTEMD_SERVICE:-lr-bookarm-takeout.service}"
BOOKARM_CAN_SERVICE="${LR_BOOKARM_CAN_SYSTEMD_SERVICE:-lr-bookarm-can.service}"
LEGACY_BOOKARM_SERVICE="${LR_LEGACY_BOOKARM_SYSTEMD_SERVICE:-lr-bookarm-runtime.service}"

# 前端总览页「机械臂硬件启动确认」按钮写入的策略文件优先于单元环境默认值，
# 现场操作员确认后即时生效，无需编辑 systemd 单元或重启宿主代理。
BOOKARM_START_POLICY_FILE="${LR_BOOKARM_START_POLICY_FILE:-/etc/lr-platform/bookarm-start-policy.conf}"
ALLOW_START="${LR_BOOKARM_ALLOW_START:-false}"
if [[ -r "$BOOKARM_START_POLICY_FILE" ]]; then
  ALLOW_START="$(tr -d '[:space:]' < "$BOOKARM_START_POLICY_FILE")"
fi

step() { printf 'STEP %s %s %s\n' "$1" "$2" "$3"; }

wait_http() {
  local url="$1" tries="${2:-10}"
  for _ in $(seq 1 "$tries"); do
    curl -fsS --max-time 2 "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

bookarm_ready() {
  local socket_path="${LR_BOOKARM_SOCKET_PATH:-/run/lr-bookarm/bookarm.sock}"
  # 受管 BookArm 服务配置了 --token 后所有接口（含 /health）都强制鉴权，
  # 健康检查必须携带同一 Bearer token，否则永远 401。
  local token="${LR_BOOKARM_TOKEN:-}"
  if [[ -z "$token" && -r /etc/lr-bookarm/bookarm.env ]]; then
    token=$(sed -n 's/^LR_BOOKARM_TOKEN=//p' /etc/lr-bookarm/bookarm.env | tail -n 1)
  fi
  if [[ -S "$socket_path" ]]; then
    if [[ -n "$token" ]]; then
      curl --unix-socket "$socket_path" -fsS --max-time 2 \
        -H "Authorization: Bearer $token" \
        http://localhost/api/v1/takeout/health >/dev/null 2>&1
    else
      curl --unix-socket "$socket_path" -fsS --max-time 2 \
        http://localhost/api/v1/takeout/health >/dev/null 2>&1
    fi
    return
  fi
  return 1
}

legacy_bookarm_tcp_ready() {
  # The previous hand-operated BookArm daemon bound this loopback TCP port.
  # It did not participate in the current process lock or socket-only Web
  # proxy, so treating it as the managed service could let a second daemon
  # race it for CAN, camera and serial hardware.
  wait_http "http://127.0.0.1:8090/api/v1/takeout/health" 1
}

failed=0

# A legacy BookArm service and the current daemon use the same CAN, camera and
# serial devices.  Never start another owner while the legacy service is live.
if systemctl is-active --quiet "$LEGACY_BOOKARM_SERVICE"; then
  step bookarm-conflict FAILED "$LEGACY_BOOKARM_SERVICE 正在运行，不能与新 BookArm 服务并存；请现场确认机械臂已安全停机后再迁移"
  failed=1
elif legacy_bookarm_tcp_ready; then
  step bookarm-conflict FAILED "检测到旧 BookArm TCP 服务 (:8090)；can0 当前不会自动配置。现场确认急停、供电及关节 3/5 状态后，停止旧服务并启动 lr-bookarm-takeout.service 完成迁移"
  failed=1
else
  step bookarm-conflict OK "未检测到旧 BookArm 硬件服务或 TCP 端点"
fi

# ---------- CAN ----------
if [[ "$failed" -eq 0 ]]; then
  if ip link show can0 2>/dev/null | grep -q "state UP"; then
    step can0 OK "can0 已上线"
  elif [[ "$ALLOW_START" != true ]]; then
    # CAN is part of the mechanical-arm power path.  A dashboard click must
    # not turn a stopped arm back into a live bus merely because the rest of
    # the software stack needs recovery.
    step can0 SKIPPED "未自动配置 can0；请在总览页点「机械臂硬件启动确认」并现场检查后重试"
  elif systemctl cat "$BOOKARM_CAN_SERVICE" >/dev/null 2>&1 && \
       sudo -n systemctl start "$BOOKARM_CAN_SERVICE" && \
       ip link show can0 2>/dev/null | grep -q "state UP"; then
    step can0 STARTED "已由 $BOOKARM_CAN_SERVICE 配置 can0 @1Mbps"
  elif [[ -x "$BOOKARM_ROOT/scripts/arm/setup_can.sh" ]] && \
       sudo -n bash "$BOOKARM_ROOT/scripts/arm/setup_can.sh" can0 1000000 >/dev/null 2>&1; then
    step can0 STARTED "can0 @1Mbps 已配置（兼容模式）"
  else
    step can0 FAILED "无法配置 can0；检查 sudo 权限和 $BOOKARM_ROOT"
    failed=1
  fi
else
  step can0 SKIPPED "旧 BookArm 服务仍持有硬件"
fi

# ---------- BookArm ----------
if [[ "$failed" -ne 0 ]]; then
  step bookarm-service SKIPPED "硬件互斥或 CAN 检查失败"
elif bookarm_ready; then
  step bookarm-service OK "BookArm 服务在线（私有 Unix socket）"
elif [[ "$ALLOW_START" != true ]]; then
  # The handover records unresolved joint temperature/fault conditions.  Do
  # not turn a web click into an unreviewed hardware power-up; an operator who
  # has inspected the arm can opt in for this one start attempt.
  step bookarm-service SKIPPED "未自动启动硬件；请在总览页点「机械臂硬件启动确认」并现场检查后重试"
elif systemctl cat "$BOOKARM_SERVICE" >/dev/null 2>&1 && \
     sudo -n systemctl start "$BOOKARM_SERVICE"; then
  # systemd 返回时服务还在初始化硬件（CAN/串口/相机握手需要数秒），
  # 立即健康检查必然失败；给受管服务一段就绪窗口。
  bookarm_up=""
  for _ in $(seq 1 15); do
    if bookarm_ready; then
      bookarm_up=1
      break
    fi
    sleep 2
  done
  if [[ -n "$bookarm_up" ]]; then
    step bookarm-service STARTED "已由 $BOOKARM_SERVICE 受管启动"
  else
    step bookarm-service FAILED "服务启动后健康检查失败：journalctl -u $BOOKARM_SERVICE"
    failed=1
  fi
else
  step bookarm-service FAILED "未安装或无法启动 $BOOKARM_SERVICE；请先安装受管 BookArm 服务"
  failed=1
fi

# ---------- Robot runtime ----------
if systemctl is-active --quiet lr-robot-runtime.service; then
  step robot-runtime OK "导航栈运行中"
elif sudo -n systemctl start lr-robot-runtime.service && sleep 3 && \
     systemctl is-active --quiet lr-robot-runtime.service; then
  step robot-runtime STARTED "导航栈已启动"
else
  step robot-runtime FAILED "lr-robot-runtime.service 启动失败，查看 journalctl -u lr-robot-runtime"
  failed=1
fi

# ---------- Mapping agent ----------
if systemctl is-active --quiet lr-robot-mapping-agent.service; then
  step mapping-agent OK "建图代理运行中 (:18090)"
elif sudo -n systemctl start lr-robot-mapping-agent.service && sleep 2 && \
     systemctl is-active --quiet lr-robot-mapping-agent.service; then
  step mapping-agent STARTED "建图代理已启动"
else
  step mapping-agent FAILED "lr-robot-mapping-agent.service 启动失败"
  failed=1
fi

# ---------- Docker platform: Compose owns every dependency ----------
PLATFORM_SYSTEMD_SERVICE="${LR_PLATFORM_SYSTEMD_SERVICE:-lr-platform.service}"
if [[ "$(systemctl show -p LoadState --value "$PLATFORM_SYSTEMD_SERVICE" 2>/dev/null)" == loaded ]]; then
  # The installed appliance service owns the root-readable production env and
  # PKI env files.  Calling start-platform.sh directly from an ordinary shell
  # silently falls back to local HTTP mode and cannot revive a running HTTPS
  # station.  Use the service's own environment instead.
  if sudo -n systemctl start "$PLATFORM_SYSTEMD_SERVICE" && \
     systemctl is-active --quiet "$PLATFORM_SYSTEMD_SERVICE"; then
    step platform-web STARTED "$PLATFORM_SYSTEMD_SERVICE 已按受管部署配置检查并启动平台"
  else
    step platform-web FAILED "$PLATFORM_SYSTEMD_SERVICE 启动失败；查看 journalctl -u $PLATFORM_SYSTEMD_SERVICE"
    failed=1
  fi
elif bash "$LR_WEB_ROOT/scripts/start-platform.sh"; then
  step platform-web STARTED "Docker Compose 已检查并启动 Web、control-api 与基础设施"
else
  step platform-web FAILED "Compose 平台启动或健康检查失败；查看 docker compose logs"
  failed=1
fi

if [[ "$failed" -eq 0 ]]; then
  exit 0
fi
exit 1
