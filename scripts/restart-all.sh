#!/usr/bin/env bash
# LR 平台一键重启脚本：重启全部后端功能模块。
#
# 与 start-all.sh 一样是固定命令宿主脚本：不接受任何参数或调用方输入。
# 安全约束：
# - 只重启"已在运行"的机械臂服务，绝不把一次网页点击变成机械臂硬件上电；
# - can0 只在掉线时恢复，不主动 bounce 正在使用的机械臂总线；
# - Web / control-api 容器在本脚本返回后延迟数秒在后台重启，确保调用方能
#   收到本次执行结果，随后接口短暂失联属于预期行为。
# Every component emits: STEP <name> <OK|STARTED|RESTARTED|FAILED|SKIPPED> <message>

set -u

PLATFORM_WEB_CONTAINER="${LR_PLATFORM_WEB_CONTAINER:-docker-compose-web-1}"
PLATFORM_API_CONTAINER="${LR_PLATFORM_API_CONTAINER:-docker-compose-control-api-1}"
PLATFORM_RESTART_DELAY_S="${LR_PLATFORM_RESTART_DELAY_S:-3}"
BOOKARM_SERVICE="${LR_BOOKARM_SYSTEMD_SERVICE:-lr-bookarm-takeout.service}"
BOOKARM_CAN_SERVICE="${LR_BOOKARM_CAN_SYSTEMD_SERVICE:-lr-bookarm-can.service}"

step() { printf 'STEP %s %s %s\n' "$1" "$2" "$3"; }

failed=0

# ---------- CAN ----------
if ip link show can0 2>/dev/null | grep -q "state UP"; then
  step can0 OK "can0 已上线，保持现状（重启总线会干扰机械臂）"
elif sudo -n systemctl restart "$BOOKARM_CAN_SERVICE" && sleep 2 && \
     ip link show can0 2>/dev/null | grep -q "state UP"; then
  step can0 STARTED "can0 已恢复上线"
else
  step can0 FAILED "无法恢复 can0；检查 CAN 硬件与 $BOOKARM_CAN_SERVICE"
  failed=1
fi

# ---------- BookArm ----------
if systemctl is-active --quiet "$BOOKARM_SERVICE"; then
  if sudo -n systemctl restart "$BOOKARM_SERVICE"; then
    step bookarm-service RESTARTED "已重启 $BOOKARM_SERVICE"
  else
    step bookarm-service FAILED "重启失败；查看 journalctl -u $BOOKARM_SERVICE"
    failed=1
  fi
else
  step bookarm-service SKIPPED "$BOOKARM_SERVICE 未在运行，不自动给机械臂上电"
fi

# ---------- Robot runtime ----------
if sudo -n systemctl restart lr-robot-runtime.service && sleep 3 && \
   systemctl is-active --quiet lr-robot-runtime.service; then
  step robot-runtime RESTARTED "导航栈与 Edge Gateway 已重启"
else
  step robot-runtime FAILED "lr-robot-runtime.service 重启失败；查看 journalctl -u lr-robot-runtime"
  failed=1
fi

# ---------- Mapping agent ----------
if systemctl is-active --quiet lr-robot-mapping-agent.service; then
  if sudo -n systemctl restart lr-robot-mapping-agent.service && sleep 2 && \
     systemctl is-active --quiet lr-robot-mapping-agent.service; then
    step mapping-agent RESTARTED "建图代理已重启 (:18090)"
  else
    step mapping-agent FAILED "lr-robot-mapping-agent.service 重启失败"
    failed=1
  fi
else
  step mapping-agent SKIPPED "建图代理未在运行"
fi

# ---------- 车端辅助遥测 ----------
aux_failed=""
for unit in lr-station-discovery.service; do
  if systemctl cat "$unit" >/dev/null 2>&1; then
    sudo -n systemctl restart "$unit" || aux_failed="$aux_failed $unit"
  fi
done
if [[ -z "$aux_failed" ]]; then
  step telemetry RESTARTED "控制站发现遥测已重启"
else
  step telemetry FAILED "部分遥测服务重启失败：$aux_failed"
  failed=1
fi

# ---------- Map sync（一次性同步任务，失败只报告不阻断）----------
if systemctl cat lr-robot-map-sync.service >/dev/null 2>&1; then
  if sudo -n systemctl restart lr-robot-map-sync.service; then
    step map-sync OK "地图同步已触发（结果见 journalctl -u lr-robot-map-sync）"
  else
    step map-sync FAILED "地图同步触发失败；通常是控制站网络不可达"
  fi
else
  step map-sync SKIPPED "未安装 lr-robot-map-sync.service"
fi

# ---------- 平台 Web 与 control-api（延迟、后台重启）----------
# Docker 应用层已退役：control-api 由宿主 lr-control-api.service 提供。
# 仍在脚本返回后延迟数秒后台重启，确保调用方能收到本次执行结果，
# 随后 :8000 接口短暂失联属于预期行为。
if systemctl cat lr-control-api.service >/dev/null 2>&1; then
  nohup bash -c "sleep '$PLATFORM_RESTART_DELAY_S' && sudo -n systemctl restart lr-control-api.service" \
    >/dev/null 2>&1 &
  step control-api OK "已安排 ${PLATFORM_RESTART_DELAY_S}s 后重启宿主 control-api；接口将短暂失联"
elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$PLATFORM_API_CONTAINER"; then
  nohup bash -c "sleep '$PLATFORM_RESTART_DELAY_S' && docker restart '$PLATFORM_WEB_CONTAINER' '$PLATFORM_API_CONTAINER'" \
    >/dev/null 2>&1 &
  step platform-web OK "已安排 ${PLATFORM_RESTART_DELAY_S}s 后重启 Web 与 control-api；接口将短暂失联"
else
  step control-api FAILED "未安装 lr-control-api.service，也没有可重启的容器化 control-api"
  failed=1
fi

if [[ "$failed" -eq 0 ]]; then
  exit 0
fi
exit 1
