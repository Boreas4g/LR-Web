#!/usr/bin/env bash
# LR 平台后端一键启动脚本（幂等，可重复执行）。
#
# 用法：
#   bash scripts/start-backend.sh          # 启动/修复全部后端组件
#   bash scripts/start-backend.sh status   # 只查看各组件状态
#
# 覆盖组件：CAN → docker基础设施(EMQX/PG/Redis) → 导航栈 → 建图代理
#          → bookarm 服务(unix socket) → control-api(:8080) → 前端静态服务(:8000)

set -u

# 本脚本只与本机服务通信：清掉继承来的代理变量，避免子进程（如 control-api
# 的 urllib）把 127.0.0.1 请求送进本机代理导致"建图代理不可达"。
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
export no_proxy="127.0.0.1,localhost" NO_PROXY="127.0.0.1,localhost"

LR_WEB_ROOT="/home/boreas/project/LR/LR-Web"
BOOKARM_ROOT="/home/boreas/project/lingzu_arm/lingzu-bookarm-py/bookarm"
NODE22_DIR="$LR_WEB_ROOT/.tools/node-v22.17.0-linux-x64/bin"
CONDA_BOOKARM="/home/boreas/miniconda3/envs/bookarm/bin/python"
CONDA="/home/boreas/miniconda3/bin/conda"
BOOKARM_SOCK="/run/lr-bookarm/bookarm.sock"

say() { printf '%-18s %s\n' "[$1]" "$2"; }
ok()  { say "$1" "✔ $2"; }
bad() { say "$1" "✘ $2"; }

http_ok() { curl -fsS -m 3 "$1" >/dev/null 2>&1; }
sock_ok() { curl -fsS -m 3 --unix-socket "$BOOKARM_SOCK" "$1" >/dev/null 2>&1; }

status_only=false
[ "${1:-}" = "status" ] && status_only=true

# ---------- 1. CAN ----------
if ip link show can0 2>/dev/null | grep -q "state UP"; then
  ok can0 "已上线"
else
  if $status_only; then bad can0 "未上线"; else
    sudo -n bash "$BOOKARM_ROOT/scripts/arm/setup_can.sh" can0 1000000 >/dev/null 2>&1 \
      && ok can0 "已配置 @1Mbps" || bad can0 "配置失败"
  fi
fi

# ---------- 2. docker 基础设施 ----------
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx docker-compose-emqx-1; then
  ok docker "EMQX/Postgres/Redis 运行中"
else
  if $status_only; then bad docker "部分容器未运行"; else
    for c in docker-compose-emqx-1 docker-compose-postgres-1 docker-compose-redis-1; do
      docker start "$c" >/dev/null 2>&1 || true
    done
    ok docker "已拉起基础设施容器"
  fi
fi

# ---------- 3. 导航栈 ----------
if systemctl is-active --quiet lr-robot-runtime.service; then
  ok robot-runtime "导航栈运行中"
else
  if $status_only; then bad robot-runtime "未运行"; else
    sudo -n systemctl start lr-robot-runtime.service
    sleep 5
    systemctl is-active --quiet lr-robot-runtime.service \
      && ok robot-runtime "已启动" || bad robot-runtime "启动失败（journalctl -u lr-robot-runtime 排查）"
  fi
fi

# ---------- 4. 建图代理 ----------
if systemctl is-active --quiet lr-robot-mapping-agent.service; then
  ok mapping-agent "运行中 (:18090)"
else
  if $status_only; then bad mapping-agent "未运行"; else
    sudo -n systemctl start lr-robot-mapping-agent.service
    sleep 2
    systemctl is-active --quiet lr-robot-mapping-agent.service \
      && ok mapping-agent "已启动 (:18090)" || bad mapping-agent "启动失败"
  fi
fi

# ---------- 5. bookarm 服务（unix socket） ----------
if [ -S "$BOOKARM_SOCK" ] && sock_ok "http://localhost/api/v1/takeout/health"; then
  ok bookarm "运行中（$BOOKARM_SOCK）"
else
  if $status_only; then
    bad bookarm "未运行"
  else
    if [ ! -e /dev/rodmotor ]; then
      bad bookarm "推杆未连接（/dev/rodmotor 不存在），服务跳过启动；插好后重跑本脚本"
    else
      pkill -f "bookarm.service" 2>/dev/null || true
      sleep 1
      (
        cd "$BOOKARM_ROOT" || exit 1
        setsid nohup "$CONDA_BOOKARM" -m bookarm.service \
          --unix-socket "$BOOKARM_SOCK" \
          --task-store-path recordings/runtime/bookarm-tasks.json \
          --hardware-lock-path recordings/runtime/bookarm-hardware.lock \
          >>"$BOOKARM_ROOT/recordings/service-sock.log" 2>&1 < /dev/null &
      )
      for _ in $(seq 1 20); do
        sock_ok "http://localhost/api/v1/takeout/health" && break
        sleep 1
      done
      sock_ok "http://localhost/api/v1/takeout/health" \
        && ok bookarm "已启动（遥测/温度保护运行中）" \
        || bad bookarm "启动失败（见 $BOOKARM_ROOT/recordings/service-sock.log）"
    fi
  fi
fi

# ---------- 6. control-api ----------
if http_ok "http://127.0.0.1:8080/health"; then
  ok control-api "运行中 (:8080)"
else
  if $status_only; then bad control-api "未运行"; else
    pkill -f "uvicorn app.main:app" 2>/dev/null || true
    sleep 1
    (
      cd "$LR_WEB_ROOT/apps/control-api" || exit 1
      setsid nohup "$CONDA" run -n lr-control-api --no-capture-output \
        uvicorn app.main:app --host 0.0.0.0 --port 8080 \
        >>/tmp/lr-control-api.log 2>&1 < /dev/null &
    )
    for _ in $(seq 1 15); do
      http_ok "http://127.0.0.1:8080/health" && break
      sleep 1
    done
    http_ok "http://127.0.0.1:8080/health" \
      && ok control-api "已启动 (:8080)" || bad control-api "启动失败（见 /tmp/lr-control-api.log）"
  fi
fi

# ---------- 7. 前端静态服务 ----------
if http_ok "http://127.0.0.1:8000/"; then
  ok lr-web "前端在线 (:8000)"
else
  if $status_only; then bad lr-web "未运行"; else
    pkill -f "serve-static.cjs" 2>/dev/null || true
    sleep 1
    (
      cd "$LR_WEB_ROOT" || exit 1
      PATH="$NODE22_DIR:$PATH" setsid nohup node scripts/serve-static.cjs --port 8000 \
        >>"$LR_WEB_ROOT/log/static-server.log" 2>&1 < /dev/null &
    )
    for _ in $(seq 1 10); do
      http_ok "http://127.0.0.1:8000/" && break
      sleep 1
    done
    http_ok "http://127.0.0.1:8000/" \
      && ok lr-web "已启动 (:8000)" || bad lr-web "启动失败（见 log/static-server.log）"
  fi
fi
