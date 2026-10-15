"""车辆缓速脉冲节点：为 bookarm 识别后居中校正提供本机 HTTP 控制面。

bookarm 任务在升降杆高位时需要前后微量动车（取/还书对齐目标书）。
本节点只监听 127.0.0.1（默认端口 18091），对外仅有三个端点：

  POST /pulse {"direction": "forward"|"backward", "duration_ms": int}
  POST /stop
  GET  /status

安全特性（升降杆高位场景）：
  - 空闲时不发布速度；脉冲结束归零后只发送一次停车，不干扰手控或导航；
  - 发布端斜坡：定时器以 accel 限步逼近目标速度，到点/停止同样斜坡归零，
    缓启动缓停；底盘驱动层 accel_rpm_per_sec 是第二层兜底；
  - 速度硬上限 hard_speed_cap（默认 0.08 m/s），脉冲目标速度默认 0.03 m/s；
  - 单脉冲时长硬上限 max_pulse_ms（默认 500ms）；
  - 看门狗：watchdog_ms 内无任何新指令立即归零（不走斜坡，保证停车语义）；
  - 行程闸：累计估算行程超 travel_cap_m 后拒绝新脉冲，冷却 travel_cooldown_s。
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import rclpy
    from geometry_msgs.msg import Twist
except ImportError as error:  # ROS 环境未 source 时直接失败，由 systemd 重启
    import sys

    print(f"vehicle_nudge_node: 无法导入 rclpy（ROS 环境未 source？）: {error}", file=sys.stderr)
    raise SystemExit(1)


def _ramp_toward(current: float, target: float, step: float) -> float:
    if current < target:
        return min(current + step, target)
    return max(current - step, target)


class VehicleNudgeCore:
    """ROS 节点 + 斜坡脉冲执行体。"""

    def __init__(self) -> None:
        self.node = rclpy.create_node("vehicle_nudge_node")
        self.pulse_speed = float(self.node.declare_parameter("pulse_speed", 0.03).value)
        self.accel = float(self.node.declare_parameter("accel", 0.12).value)
        self.rate_hz = float(self.node.declare_parameter("rate_hz", 20.0).value)
        self.hard_speed_cap = float(self.node.declare_parameter("hard_speed_cap", 0.08).value)
        self.max_pulse_ms = int(self.node.declare_parameter("max_pulse_ms", 500).value)
        self.watchdog_ms = int(self.node.declare_parameter("watchdog_ms", 800).value)
        self.travel_cap_m = float(self.node.declare_parameter("travel_cap_m", 0.25).value)
        self.travel_cooldown_s = float(
            self.node.declare_parameter("travel_cooldown_s", 60.0).value
        )

        self.pulse_speed = min(max(self.pulse_speed, 0.005), self.hard_speed_cap)

        self._lock = threading.Lock()
        self._target_linear = 0.0
        self._out_linear = 0.0
        self._last_published_nonzero = False
        self._deadline = 0.0  # time.monotonic()；脉冲结束时刻
        self._commanded_at = 0.0  # 最近一次任何指令的时间；0=从未
        self._travel_m = 0.0
        self._travel_locked_until = 0.0
        self._last_tick = time.monotonic()

        self._cmd_pub = self.node.create_publisher(Twist, "/cmd_vel", 10)
        self._timer = self.node.create_timer(1.0 / self.rate_hz, self._on_timer)
        self._spin_thread = threading.Thread(target=self._spin, name="nudge-spin", daemon=True)
        self._spin_thread.start()
        self.node.get_logger().info(
            "vehicle_nudge_node 就绪：speed=%.3f m/s accel=%.2f m/s² cap=%.2f m"
            % (self.pulse_speed, self.accel, self.travel_cap_m)
        )

    def _spin(self) -> None:
        try:
            rclpy.spin(self.node)
        except Exception:
            pass

    def _on_timer(self) -> None:
        now = time.monotonic()
        with self._lock:
            dt = max(now - self._last_tick, 1e-3)
            self._last_tick = now
            step = self.accel / self.rate_hz
            expired = now >= self._deadline
            stale = self._commanded_at > 0.0 and (now - self._commanded_at) * 1000.0 > self.watchdog_ms
            if stale:
                # 看门狗：立即零速，不走斜坡
                self._out_linear = 0.0
                self._target_linear = 0.0
                self._deadline = 0.0
            elif expired:
                self._target_linear = 0.0
                self._out_linear = _ramp_toward(self._out_linear, 0.0, step)
            else:
                self._out_linear = _ramp_toward(self._out_linear, self._target_linear, step)
            self._travel_m += abs(self._out_linear) * dt
            linear = self._out_linear
            if linear == 0.0 and not self._last_published_nonzero:
                return
            message = Twist()
            message.linear.x = linear
            try:
                self._cmd_pub.publish(message)
            except Exception:
                # Keep a pending stop if publishing fails; retry on the next tick.
                return
            self._last_published_nonzero = linear != 0.0

    # -- 对外操作 -----------------------------------------------------------

    def pulse(self, direction: str, duration_ms: int) -> dict:
        if direction not in ("forward", "backward"):
            return {"ok": False, "error": "direction 必须为 forward/backward"}
        duration_ms = int(duration_ms)
        if duration_ms <= 0:
            return {"ok": False, "error": "duration_ms 必须为正"}
        if duration_ms > self.max_pulse_ms:
            return {"ok": False, "error": "duration_ms 超过单脉冲上限 %d ms" % self.max_pulse_ms}
        now = time.monotonic()
        with self._lock:
            if now < self._travel_locked_until:
                return {
                    "ok": False,
                    "error": "行程闸冷却中（累计 %.3f m ≥ %.2f m），%.0fs 后解除"
                    % (self._travel_m, self.travel_cap_m, self._travel_locked_until - now),
                }
            sign = 1.0 if direction == "forward" else -1.0
            self._target_linear = sign * self.pulse_speed
            self._deadline = now + duration_ms / 1000.0
            self._commanded_at = now
            travel = self._travel_m
        return {
            "ok": True,
            "direction": direction,
            "duration_ms": duration_ms,
            "speed_mps": self.pulse_speed,
            "travel_m_est": round(travel, 4),
        }

    def stop(self) -> dict:
        with self._lock:
            self._target_linear = 0.0
            self._deadline = 0.0
            self._commanded_at = time.monotonic()
        return {"ok": True}

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "moving": abs(self._target_linear) > 1e-6,
                "out_linear": round(self._out_linear, 4),
                "travel_m_est": round(self._travel_m, 4),
                "travel_locked": time.monotonic() < self._travel_locked_until,
                "pulse_speed": self.pulse_speed,
                "accel": self.accel,
            }

    def _check_travel_gate(self) -> None:
        """定时器内检查行程闸：超限即锁定并冷却。"""
        with self._lock:
            if self._travel_m >= self.travel_cap_m and self._travel_locked_until == 0.0:
                self._travel_locked_until = time.monotonic() + self.travel_cooldown_s
                self._target_linear = 0.0
                self._deadline = 0.0
                self.node.get_logger().warn("行程闸触发：冷却 %.0fs" % self.travel_cooldown_s)
            elif self._travel_locked_until and time.monotonic() >= self._travel_locked_until:
                self._travel_locked_until = 0.0
                self._travel_m = 0.0
                self.node.get_logger().info("行程闸冷却结束，计数清零")


class _Handler(BaseHTTPRequestHandler):
    server_version = "lr-vehicle-nudge/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib 签名
        return

    def _reply(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib 签名
        if self.path == "/status":
            self._reply(self.server.core.status())
        else:
            self._reply({"ok": False, "error": "未知端点"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib 签名
        if self.path == "/stop":
            self._reply(self.server.core.stop())
            return
        if self.path != "/pulse":
            self._reply({"ok": False, "error": "未知端点"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            direction = payload["direction"]
            duration_ms = payload["duration_ms"]
        except (ValueError, KeyError, TypeError):
            self._reply({"ok": False, "error": "请求体须为 JSON 且含 direction/duration_ms"}, 400)
            return
        self._reply(self.server.core.pulse(direction, duration_ms))


def main() -> int:
    rclpy.init(args=None)
    core = VehicleNudgeCore()

    node = core.node
    host = str(node.declare_parameter("host", "127.0.0.1").value)
    port = int(node.declare_parameter("port", 18091).value)

    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.core = core  # type: ignore[attr-defined]
    node.get_logger().info(f"HTTP 控制面监听 http://{host}:{port}")

    # 行程闸在 1Hz 慢线程里检查
    def _gate_loop() -> None:
        while True:
            core._check_travel_gate()
            time.sleep(1.0)

    threading.Thread(target=_gate_loop, name="travel-gate", daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        core.stop()
        httpd.server_close()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
