#!/usr/bin/env python3
"""手动建图车端代理：HTTP 控制面，在导航栈与建图栈之间安全切换。

默认仅监听本机回环 127.0.0.1:18090（可用 MAPPING_AGENT_HOST / MAPPING_AGENT_PORT
覆盖），不向局域网开放。端点（除快照外均为 JSON）：

  GET  /mapping/status        {"mode": "navigation"|"mapping"|"transition",
                               "slamRunning": bool, "slamRestarted": bool,
                               "message": str}；slamRestarted=true 表示建图栈
                              在会话中途重启过，此前记录的书架点坐标已失效
  POST /mapping/start         停 lr-robot-runtime，起 lr-robot-mapping（幂等）；
                              成功切换时开启新会话：清空书架标注与保存缓存
  POST /mapping/stop          停 lr-robot-mapping，起 lr-robot-runtime（幂等）
  POST /mapping/jog           {"linear": x, "angular": z}，钳制 ±0.3 m/s、±0.8 rad/s
                              后以 20Hz 持续发布 /cmd_vel；输出经加速度斜坡
                              （线 0.5 m/s²、角 1.5 rad/s²）防打滑；0.6s 无新 jog
                              立即零速（看门狗，不走斜坡）；仅建图模式可用，否则 409
  GET  /mapping/pose          {"ok": true, "x", "y", "yaw"}，tf2 查 map→base_link
                              最新变换；非建图模式 409，TF 不可用 503
  GET  /mapping/loc-overlay   最新一帧激光投影到 map 系的定位叠图（导航/建图
                              模式均可用）：{"ok", "capturedAt", "frameId",
                              "pose": {"x","y","yaw"}, "points": [[x,y]...]}，
                              点均匀抽稀到 ≤1500 个、保留 3 位小数；扫描失效
                              或 TF 不可用时 503 {"ok": false, "error": "原因"}
  POST /mapping/mark          {"label": "可选"}，按当前 TF 位姿记录一条书架标注，
                              label 为空自动命名 "书架点 N"；返回 201 {"mark", "count"}
  GET  /mapping/marks         {"marks": [...]} 本次建图会话的书架标注
  DELETE /mapping/marks/{n}   删除第 n 条标注（0 起），越界 404；
                              返回 {"ok": true, "removed": mark, "count": m}
  POST /mapping/marks/clear   清空标注，返回 {"ok": true, "cleared": n}
  GET  /mapping/snapshot.png  最新 SLAM 地图灰度 PNG（-1→205 灰，0→254 白，100→0 黑）；
                              建图未启动或首帧未到返回 404
  POST /mapping/save          {"name": "可选显示名"}，map_saver_cli 落盘后用
                              scripts/register-robot-map.sh 注册，返回
                              {"ok", "mapId", "yamlPath", "pgmPath", "yamlB64", "pgmB64",
                               "marks", "alreadySaved"}。同一建图会话内重复调用幂等：
                              已保存成功过则直接返回首次结果（不重复 map_saver、不重复
                              注册、不新生成 ID），且 alreadySaved=true；保存成功后
                              清空书架标注。会话状态随 /mapping/start 成功切换重置

书架标注在每次变更时原子写入 $MAPPING_AGENT_MARKS_FILE（默认
/var/lib/lr-robot/mapping-marks.json；目录不可用时退化为仅内存并告警）。
代理重启且仍处于建图模式时自动恢复；保存地图成功或 /mapping/start 成功
切换时清空并删除该文件。

错误统一返回 {"detail": "..."} 与合理状态码（/mapping/loc-overlay 例外，
按约定返回 {"ok": false, "error"}）；全部日志写 stderr。
收到 SIGTERM/SIGINT 时先补一帧零速再退出。

安装步骤（本代理以 boreas 运行，与两个 ROS 栈同用户，避免跨用户 DDS
共享内存传输问题；systemctl 与地图注册通过 boreas 的免密 sudo 执行）：
  1. 本文件位于 /home/boreas/LR/Library_robot/scripts/mapping_agent.py。
  2. 安装两个 systemd 单元（源文件在 scripts/systemd/ 目录）：
       sudo install -m 0644 scripts/systemd/lr-robot-mapping.service \
                            scripts/systemd/lr-robot-mapping-agent.service \
                            /etc/systemd/system/
       lr-robot-mapping.service       建图栈，不 enable
       lr-robot-mapping-agent.service 本代理，enable
  3. sudo systemctl daemon-reload
  4. sudo systemctl enable --now lr-robot-mapping-agent.service
  5. 预建书架标注持久化文件（/var/lib/lr-robot 为 root 所有，代理以 boreas
     运行；不预建则标注退化为仅内存并告警，其余功能不受影响）：
       printf '{"version": 1, "marks": []}\n' | sudo install -o boreas -g boreas \
         -m 0660 /dev/stdin /var/lib/lr-robot/mapping-marks.json
  6. 验证：curl 127.0.0.1:18090/mapping/status 应返回 "mode": "navigation"

卸载步骤：
  1. sudo systemctl disable --now lr-robot-mapping-agent.service
  2. sudo systemctl stop lr-robot-mapping.service || true
  3. sudo rm /etc/systemd/system/lr-robot-mapping.service \
             /etc/systemd/system/lr-robot-mapping-agent.service
  4. sudo systemctl daemon-reload
  5. sudo systemctl start lr-robot-runtime.service   # 恢复导航栈
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import rclpy
    import tf2_ros
    from geometry_msgs.msg import Quaternion, Twist
    from nav_msgs.msg import OccupancyGrid
    from rclpy.qos import (
        DurabilityPolicy,
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
        qos_profile_sensor_data,
    )
    from rclpy.time import Time
    from sensor_msgs.msg import LaserScan
except ImportError as error:  # ROS 环境未 source 时直接失败，由 systemd 重启
    print(f"mapping_agent: 无法导入 rclpy（ROS 环境未 source？）: {error}", file=sys.stderr)
    raise SystemExit(1)

RUNTIME_UNIT = "lr-robot-runtime.service"
MAPPING_UNIT = "lr-robot-mapping.service"
REGISTER_SCRIPT = "/home/boreas/LR/Library_robot/scripts/register-robot-map.sh"
MAP_STORAGE_ROOT = Path("/var/lib/lr-robot/maps")
MARKS_FILE = Path(os.environ.get("MAPPING_AGENT_MARKS_FILE", "/var/lib/lr-robot/mapping-marks.json"))

SYSTEMCTL_TIMEOUT_SEC = 30
MAP_SAVER_TIMEOUT_SEC = 90
MAP_SAVER_ATTEMPTS = 3
JOG_RATE_HZ = 20.0
JOG_WATCHDOG_SEC = 0.6
MAX_LINEAR_MPS = 0.3
MAX_ANGULAR_RPS = 0.8
# jog 加速度斜坡上限（防打滑）：20Hz 下每拍步进 0.025 m/s、0.075 rad/s
LINEAR_ACCEL_MPS2 = 0.5
ANGULAR_ACCEL_RPS2 = 1.5
LINEAR_STEP_MPS = LINEAR_ACCEL_MPS2 / JOG_RATE_HZ
ANGULAR_STEP_RPS = ANGULAR_ACCEL_RPS2 / JOG_RATE_HZ
MAP_FRAME_STALE_SEC = 5.0
SCAN_STALE_SEC = 2.0  # 激光扫描超过该时长未更新视为失效
LOC_OVERLAY_MAX_POINTS = 1500  # 定位叠图均匀抽稀后的点数上限
MODE_POLL_SEC = 2.0
MAX_BODY_BYTES = 65536

_transition_lock = threading.Lock()
_map_save_lock = threading.Lock()  # 串行化 /mapping/save，保证会话内幂等


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"[{timestamp}] mapping_agent: {message}", file=sys.stderr, flush=True)


class AgentError(Exception):
    """携带 HTTP 状态码的业务错误，响应体为 {"detail": ...}。"""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# 模式判定与 systemctl 切换
# ---------------------------------------------------------------------------


def unit_active(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def current_mode() -> str:
    if unit_active(MAPPING_UNIT):
        return "mapping"
    if unit_active(RUNTIME_UNIT):
        return "navigation"
    return "transition"


def unit_invocation_id(unit: str) -> str | None:
    """systemd 单元的 InvocationID，每次（重）启动都会变化；未运行或查询失败返回 None。"""

    result = subprocess.run(
        ["systemctl", "show", unit, "--property=InvocationID", "--value"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def run_systemctl(*args: str) -> None:
    # boreas 通过免密 sudo 执行 systemctl；sudo -n 在无 TTY 的 systemd 环境下可用。
    command = ["timeout", str(SYSTEMCTL_TIMEOUT_SEC), "sudo", "-n", "systemctl", *args]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=SYSTEMCTL_TIMEOUT_SEC + 10,
        check=False,
    )
    if result.returncode == 124:
        raise RuntimeError(f"systemctl {' '.join(args)} 超过 {SYSTEMCTL_TIMEOUT_SEC}s 未返回")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"systemctl {' '.join(args)} 失败: {detail or result.returncode}")


def switch_to_mapping(ros: "RosInterface") -> None:
    with _transition_lock:
        if unit_active(MAPPING_UNIT):
            ros.stop_jog()
            ros.refresh_mode()
            log("已处于建图模式，/mapping/start 幂等返回")
            return
        log("停止导航栈 lr-robot-runtime ...")
        run_systemctl("stop", RUNTIME_UNIT)
        try:
            log("启动建图栈 lr-robot-mapping ...")
            run_systemctl("start", MAPPING_UNIT)
        except Exception:
            log("建图栈启动失败，尽力恢复导航栈")
            try:
                run_systemctl("start", RUNTIME_UNIT)
            except Exception as restore_error:
                log(f"恢复导航栈失败: {restore_error}")
            raise
        ros.stop_jog()
        ros.reset_mapping_session()  # 新一次建图会话：清空标注、保存缓存与 SLAM 重启标记
        ros.refresh_mode()  # 绑定新会话的建图栈 InvocationID
        log("已切换到建图模式")


def switch_to_navigation(ros: "RosInterface") -> None:
    with _transition_lock:
        if not unit_active(MAPPING_UNIT) and unit_active(RUNTIME_UNIT):
            ros.stop_jog()
            ros.refresh_mode()
            log("已处于导航模式，/mapping/stop 幂等返回")
            return
        ros.stop_jog()
        log("停止建图栈 lr-robot-mapping ...")
        run_systemctl("stop", MAPPING_UNIT)
        log("启动导航栈 lr-robot-runtime ...")
        run_systemctl("start", RUNTIME_UNIT)
        ros.refresh_mode()
        log("已切换到导航模式")


# ---------------------------------------------------------------------------
# ROS 接口：/cmd_vel 发布（jog + 看门狗 + 加速度斜坡）、/map 与 /scan 订阅、
# TF 位姿查询、定位叠图投影、书架标注、模式轮询
# ---------------------------------------------------------------------------


def _ramp_toward(current: float, target: float, max_step: float) -> float:
    """单拍内以不超过 max_step 的步进逼近 target。"""

    delta = target - current
    if abs(delta) <= max_step:
        return target
    return current + math.copysign(max_step, delta)


def _quaternion_yaw(rotation: Quaternion) -> float:
    """从四元数提取偏航角（平面机器人只关心 yaw）。"""

    return math.atan2(
        2.0 * (rotation.w * rotation.z + rotation.x * rotation.y),
        1.0 - 2.0 * (rotation.y * rotation.y + rotation.z * rotation.z),
    )


# ---------------------------------------------------------------------------
# 建图会话状态：书架标注（含原子持久化）、保存幂等缓存、SLAM 重启跟踪
# ---------------------------------------------------------------------------


class MappingSession:
    """单次建图会话（自 /mapping/start 成功切换起）的可变状态。

    标注每次变更都原子写入 marks_path；目录不可用或写失败时退化为仅内存并
    告警一次。SLAM 会话跟踪基于建图栈 systemd 单元的 InvocationID：轮询发现
    ID 变化即视为建图栈重启，此前的书架点坐标随 SLAM 内部状态一起失效。
    """

    def __init__(self, marks_path: Path) -> None:
        self._marks_path = marks_path
        self._marks: list[dict] = []
        self._saved_response: dict | None = None
        self._slam_restarted = False
        self._mapping_invocation: str | None = None
        self._persist_warned = False
        self._lock = threading.Lock()

    # -- 持久化 ---------------------------------------------------------------

    def _persist_marks_locked(self) -> None:
        parent = self._marks_path.parent
        try:
            if not parent.is_dir():
                raise OSError(f"目录不存在: {parent}")
            fd, temporary = tempfile.mkstemp(prefix=self._marks_path.name + ".", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"version": 1, "marks": self._marks}, stream, ensure_ascii=False)
                    stream.write("\n")
                os.replace(temporary, self._marks_path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError as error:
            if not self._persist_warned:
                self._persist_warned = True
                log(f"书架标注持久化不可用，退化为仅内存: {error}")

    def _delete_marks_file_locked(self) -> None:
        try:
            self._marks_path.unlink(missing_ok=True)
        except OSError as error:
            log(f"删除书架标注文件失败: {error}")

    def restore_marks(self) -> int:
        """代理重启且仍处于建图模式时恢复标注；返回恢复条数。文件缺失或损坏按空处理。"""

        try:
            raw = self._marks_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        except OSError as error:
            log(f"读取书架标注文件失败，按空标注处理: {error}")
            return 0
        if not raw.strip():
            return 0  # 安装时预建的空占位文件
        marks: object = None
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                marks = payload.get("marks")
        except json.JSONDecodeError:
            pass
        if not isinstance(marks, list) or not all(isinstance(mark, dict) for mark in marks):
            log("书架标注文件损坏，按空标注处理")
            return 0
        with self._lock:
            self._marks = [dict(mark) for mark in marks]
            return len(self._marks)

    # -- 标注 -----------------------------------------------------------------

    def add_mark(self, mark: dict) -> tuple[dict, int]:
        """追加一条标注并持久化，返回 (mark, count)；label 为空自动命名 "书架点 N"。"""

        with self._lock:
            if not mark.get("label"):
                mark = dict(mark, label=f"书架点 {len(self._marks) + 1}")
            self._marks.append(mark)
            self._persist_marks_locked()
            return dict(mark), len(self._marks)

    def marks(self) -> list[dict]:
        with self._lock:
            return [dict(mark) for mark in self._marks]

    def delete_mark(self, index: int) -> dict:
        """删除第 index 条（0 起）并持久化；越界抛 IndexError。"""

        with self._lock:
            if index < 0 or index >= len(self._marks):
                raise IndexError(index)
            removed = self._marks.pop(index)
            self._persist_marks_locked()
            return removed

    def clear_marks(self, delete_file: bool = False) -> int:
        """清空标注；delete_file=True 时删除持久化文件，否则原子写入空列表。"""

        with self._lock:
            count = len(self._marks)
            self._marks.clear()
            if delete_file:
                self._delete_marks_file_locked()
            else:
                self._persist_marks_locked()
            return count

    # -- 保存幂等缓存 -----------------------------------------------------------

    def saved_response(self) -> dict | None:
        with self._lock:
            return dict(self._saved_response) if self._saved_response is not None else None

    def record_saved(self, response: dict) -> None:
        with self._lock:
            self._saved_response = dict(response)

    # -- SLAM 会话跟踪 -----------------------------------------------------------

    def slam_restarted(self) -> bool:
        with self._lock:
            return self._slam_restarted

    def track_slam_session(self, mode: str, invocation: str | None) -> bool:
        """周期检查建图栈会话；检测到重启时清空标注并置位标记，返回是否本次检测到。

        invocation 为 None（systemctl 查询瞬态失败）时保留既有绑定，避免恰好
        发生在失败窗口内的重启被吞掉；离开建图模式才复位绑定与标记。
        """

        with self._lock:
            if mode != "mapping":
                self._mapping_invocation = None
                self._slam_restarted = False
                return False
            if not invocation:
                return False
            previous = self._mapping_invocation
            self._mapping_invocation = invocation
            if previous is None or previous == invocation:
                return False
            self._slam_restarted = True
            self._marks.clear()
            self._delete_marks_file_locked()
            return True

    # -- 会话生命周期 -----------------------------------------------------------

    def reset(self) -> None:
        """/mapping/start 成功切换：开启全新会话，清空标注、保存缓存与重启标记。"""

        with self._lock:
            self._marks.clear()
            self._delete_marks_file_locked()
            self._saved_response = None
            self._slam_restarted = False
            self._mapping_invocation = None


class RosInterface:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jog_linear = 0.0
        self._jog_angular = 0.0
        self._jog_last_at = 0.0  # time.monotonic()；0 表示从未收到 jog
        self._out_linear = 0.0  # 斜坡后的实际输出速度
        self._out_angular = 0.0
        self._session = MappingSession(MARKS_FILE)  # 本次建图会话状态（含标注持久化）
        self._latest_map: OccupancyGrid | None = None
        self._latest_map_at = 0.0
        self._latest_scan: LaserScan | None = None
        self._latest_scan_at = 0.0  # time.monotonic()
        self._latest_scan_wall_at = 0.0  # time.time()，供 loc-overlay 的 capturedAt
        self._mode_cache = "transition"
        self._accepting = True
        self._stop_event = threading.Event()

        self.node = rclpy.create_node("mapping_agent")
        self._cmd_pub = self.node.create_publisher(Twist, "/cmd_vel", 10)
        # /tf 订阅由既有 rclpy spin 线程驱动，与 jog 定时器共存
        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self.node)
        map_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._map_sub = self.node.create_subscription(OccupancyGrid, "/map", self._on_map, map_qos)
        # 导航与建图 launch 都有雷达，/scan 两种模式下常存，只保留最新一帧
        self._scan_sub = self.node.create_subscription(
            LaserScan, "/scan", self._on_scan, qos_profile_sensor_data
        )
        self._jog_timer = self.node.create_timer(1.0 / JOG_RATE_HZ, self._publish_cmd_vel)

        self._spin_thread = threading.Thread(target=self._spin, name="rclpy-spin", daemon=True)
        self._mode_thread = threading.Thread(target=self._poll_mode, name="mode-poll", daemon=True)
        self._spin_thread.start()
        self._mode_thread.start()

    # -- 内部线程 -----------------------------------------------------------

    def _spin(self) -> None:
        try:
            rclpy.spin(self.node)
        except Exception as error:  # 关闭时 rclpy 会抛 ExternalShutdownException
            if self._accepting:
                log(f"rclpy spin 异常退出: {error}")

    def _poll_mode(self) -> None:
        while not self._stop_event.is_set():
            self.refresh_mode()
            self._stop_event.wait(MODE_POLL_SEC)

    def _on_map(self, message: OccupancyGrid) -> None:
        with self._lock:
            self._latest_map = message
            self._latest_map_at = time.monotonic()

    def _on_scan(self, message: LaserScan) -> None:
        with self._lock:
            self._latest_scan = message
            self._latest_scan_at = time.monotonic()
            self._latest_scan_wall_at = time.time()

    def _publish_cmd_vel(self) -> None:
        with self._lock:
            if not self._accepting or self._mode_cache != "mapping":
                return
            fresh = (time.monotonic() - self._jog_last_at) <= JOG_WATCHDOG_SEC
            if fresh:
                self._out_linear = _ramp_toward(self._out_linear, self._jog_linear, LINEAR_STEP_MPS)
                self._out_angular = _ramp_toward(self._out_angular, self._jog_angular, ANGULAR_STEP_RPS)
            else:
                # 看门狗超时：立即零速，不走斜坡，保证停车语义不变
                self._out_linear = 0.0
                self._out_angular = 0.0
            linear = self._out_linear
            angular = self._out_angular
        message = Twist()
        message.linear.x = linear
        message.angular.z = angular
        try:
            self._cmd_pub.publish(message)
        except Exception as error:
            log(f"/cmd_vel 发布失败: {error}")

    # -- 对外操作 -----------------------------------------------------------

    def refresh_mode(self) -> None:
        try:
            mode = current_mode()
        except Exception as error:
            log(f"模式判定失败: {error}")
            return
        invocation = None
        if mode == "mapping":
            try:
                invocation = unit_invocation_id(MAPPING_UNIT)
            except Exception as error:
                log(f"建图栈会话标识查询失败: {error}")
        if self._session.track_slam_session(mode, invocation):
            log("检测到建图栈已重启（新 SLAM 会话），书架标注已清空")
        with self._lock:
            self._mode_cache = mode

    def set_jog(self, linear: float, angular: float) -> None:
        with self._lock:
            self._jog_linear = linear
            self._jog_angular = angular
            self._jog_last_at = time.monotonic()

    def stop_jog(self) -> None:
        with self._lock:
            self._jog_linear = 0.0
            self._jog_angular = 0.0
            self._jog_last_at = 0.0
            self._out_linear = 0.0
            self._out_angular = 0.0

    def latest_map(self) -> OccupancyGrid | None:
        with self._lock:
            return self._latest_map

    def map_age(self) -> float | None:
        with self._lock:
            if self._latest_map is None:
                return None
            return time.monotonic() - self._latest_map_at

    def current_pose(self) -> tuple[float, float, float] | None:
        """map 坐标系下 base_link 最新位姿 (x, y, yaw)；TF 不可用返回 None。"""

        try:
            transform = self._tf_buffer.lookup_transform("map", "base_link", Time())
        except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):
            return None
        translation = transform.transform.translation
        return (translation.x, translation.y, _quaternion_yaw(transform.transform.rotation))

    def latest_scan(self) -> tuple[LaserScan, float, float] | None:
        """最新一帧激光 (消息, 单调钟帧龄秒, 接收墙钟)；从未收到返回 None。"""

        with self._lock:
            if self._latest_scan is None:
                return None
            return self._latest_scan, time.monotonic() - self._latest_scan_at, self._latest_scan_wall_at

    def laser_to_base(self, laser_frame: str) -> tuple[float, float, float]:
        """base_link←激光 frame 的 2D 变换 (x, y, yaw)；查不到静态 TF 时按两系重合处理。"""

        frame = laser_frame.strip("/") or "base_link"
        if frame == "base_link":
            return (0.0, 0.0, 0.0)
        try:
            transform = self._tf_buffer.lookup_transform("base_link", frame, Time())
        except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):
            return (0.0, 0.0, 0.0)
        translation = transform.transform.translation
        return (translation.x, translation.y, _quaternion_yaw(transform.transform.rotation))

    def loc_overlay(self) -> tuple[dict | None, str | None]:
        """最新激光帧投影到 map 系的定位叠图；失败返回 (None, 中文原因)。"""

        scan_info = self.latest_scan()
        if scan_info is None:
            return None, "尚未收到激光扫描"
        scan, age, captured_at = scan_info
        if age > SCAN_STALE_SEC:
            return None, f"激光扫描已超过 {SCAN_STALE_SEC:.0f} 秒未更新"
        pose = self.current_pose()
        if pose is None:
            return None, "map→base_link 位姿暂不可用"
        px, py, pyaw = pose
        lx, ly, lyaw = self.laser_to_base(scan.header.frame_id)

        # 合成 map←激光 的 2D 变换：base_link←激光 的平移随车位姿旋转到 map 系，转角相加
        cos_p, sin_p = math.cos(pyaw), math.sin(pyaw)
        tx = px + cos_p * lx - sin_p * ly
        ty = py + sin_p * lx + cos_p * ly
        yaw = pyaw + lyaw
        cos_y, sin_y = math.cos(yaw), math.sin(yaw)

        points: list[list[float]] = []
        angle = scan.angle_min
        for distance in scan.ranges:
            beam_angle = angle
            angle += scan.angle_increment
            # 丢弃 inf/nan 与超量程点
            if not math.isfinite(distance) or distance < scan.range_min or distance > scan.range_max:
                continue
            xl = distance * math.cos(beam_angle)
            yl = distance * math.sin(beam_angle)
            points.append([tx + cos_y * xl - sin_y * yl, ty + sin_y * xl + cos_y * yl])

        # 均匀抽稀到上限以内，坐标保留 3 位小数（毫米精度足够叠图展示）
        stride = max(1, math.ceil(len(points) / LOC_OVERLAY_MAX_POINTS))
        thinned = [[round(x, 3), round(y, 3)] for x, y in points[::stride]]
        return {
            "ok": True,
            "capturedAt": captured_at,
            "frameId": "map",
            "pose": {"x": px, "y": py, "yaw": pyaw},
            "points": thinned,
        }, None

    def add_mark(self, label: str | None) -> tuple[dict, int] | None:
        """按当前 TF 位姿记录一条书架标注，返回 (mark, count)；TF 不可用返回 None。"""

        pose = self.current_pose()
        if pose is None:
            return None
        x, y, yaw = pose
        mark = {"x": x, "y": y, "yaw": yaw, "label": label, "at": time.time()}
        return self._session.add_mark(mark)

    def marks(self) -> list[dict]:
        return self._session.marks()

    def delete_mark(self, index: int) -> dict:
        """删除第 index 条标注（0 起）；越界抛 IndexError。"""

        return self._session.delete_mark(index)

    def clear_marks(self, delete_file: bool = False) -> int:
        return self._session.clear_marks(delete_file=delete_file)

    def restore_marks(self) -> int:
        return self._session.restore_marks()

    def reset_mapping_session(self) -> None:
        """/mapping/start 成功切换：开启全新建图会话。"""

        self._session.reset()

    def saved_map_response(self) -> dict | None:
        return self._session.saved_response()

    def record_saved_map(self, response: dict) -> None:
        self._session.record_saved(response)

    def slam_restarted(self) -> bool:
        return self._session.slam_restarted()

    def initiate_shutdown(self) -> None:
        """SIGTERM/SIGINT：停止 jog 发布并补一帧零速。"""

        with self._lock:
            self._accepting = False
            self._jog_linear = 0.0
            self._jog_angular = 0.0
            self._jog_last_at = 0.0
            self._out_linear = 0.0
            self._out_angular = 0.0
        self._stop_event.set()
        try:
            self._cmd_pub.publish(Twist())
        except Exception:
            pass

    def shutdown(self) -> None:
        self.initiate_shutdown()
        try:
            self.node.destroy_node()
            rclpy.shutdown()
        except Exception:
            pass
        self._spin_thread.join(timeout=2.0)
        self._mode_thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# OccupancyGrid -> 灰度 PNG（stdlib 最小 PNG 编码器，不依赖 Pillow）
# ---------------------------------------------------------------------------


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_grayscale_png(width: int, height: int, pixels: bytes) -> bytes:
    """pixels 为 width*height 字节的 8bit 灰度，按行从上到下排列。"""

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type None
        raw += pixels[y * width : (y + 1) * width]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8bit grayscale
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _png_chunk(b"IEND", b"")
    )


def occupancy_grid_to_png(message: OccupancyGrid) -> bytes:
    """灰度映射：-1→205（未知灰），0→254（空闲白），100→0（占用黑），线性过渡。

    OccupancyGrid 数据从 origin（左下）开始逐行排列，PNG 首行在顶部，需垂直翻转。
    """

    width = int(message.info.width)
    height = int(message.info.height)
    data = message.data
    if width <= 0 or height <= 0 or len(data) < width * height:
        raise AgentError(500, "SLAM 地图数据不完整")
    pixels = bytearray(width * height)
    for y in range(height):
        source_row = height - 1 - y
        base = source_row * width
        target = y * width
        for x in range(width):
            value = data[base + x]
            if value < 0:
                gray = 205
            elif value > 100:
                gray = 0
            else:
                gray = int(round(254.0 * (100 - value) / 100.0))
            pixels[target + x] = gray
    return encode_grayscale_png(width, height, bytes(pixels))


# ---------------------------------------------------------------------------
# 保存并注册地图
# ---------------------------------------------------------------------------


def save_current_map() -> dict:
    if current_mode() != "mapping":
        raise AgentError(409, "当前不在建图模式，无法保存地图")
    temporary_dir = Path(tempfile.mkdtemp(prefix="mapping-save-"))
    try:
        prefix = temporary_dir / "map"
        command = [
            "timeout",
            str(MAP_SAVER_TIMEOUT_SEC),
            "ros2",
            "run",
            "nav2_map_server",
            "map_saver_cli",
            "-t",
            "/map",
            "-f",
            str(prefix),
        ]
        log("调用 map_saver_cli 保存当前 SLAM 地图")
        yaml_path = prefix.with_suffix(".yaml")
        pgm_path = prefix.with_suffix(".pgm")
        # map_saver_cli 内部等待 /map 的窗口只有约 2s，新 participant 的 DDS 发现
        # 偶尔压线超时，故失败快速重试（实测单次 1.2-3s）。
        detail = ""
        for attempt in range(1, MAP_SAVER_ATTEMPTS + 1):
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=MAP_SAVER_TIMEOUT_SEC + 10,
                check=False,
            )
            if result.returncode == 0 and yaml_path.is_file() and pgm_path.is_file():
                break
            detail = (result.stderr or result.stdout or "").strip()[-400:] or str(result.returncode)
            log(f"map_saver_cli 第 {attempt} 次尝试失败: {detail}")
        else:
            raise AgentError(500, f"map_saver_cli 失败（{MAP_SAVER_ATTEMPTS} 次尝试）: {detail}")

        while True:
            map_id = f"MAP-{secrets.token_hex(6).upper()}"
            if not (MAP_STORAGE_ROOT / map_id).exists():
                break

        command = ["sudo", "-n", "bash", REGISTER_SCRIPT, "--map-id", map_id, "--map-yaml", str(yaml_path)]
        log(f"注册地图 {map_id}")
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-400:]
            raise AgentError(500, f"地图注册失败: {detail or result.returncode}")

        staged_yaml = MAP_STORAGE_ROOT / map_id / "map.yaml"
        staged_pgm = MAP_STORAGE_ROOT / map_id / "map.pgm"
        return {
            "ok": True,
            "mapId": map_id,
            "yamlPath": str(staged_yaml),
            "pgmPath": str(staged_pgm),
            "yamlB64": base64.b64encode(staged_yaml.read_bytes()).decode("ascii"),
            "pgmB64": base64.b64encode(staged_pgm.read_bytes()).decode("ascii"),
        }
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _finite_float(body: dict, key: str) -> float:
    if key not in body:
        raise AgentError(400, f"缺少字段 {key}")
    try:
        value = float(body[key])
    except (TypeError, ValueError):
        raise AgentError(400, f"字段 {key} 必须是数字") from None
    if not math.isfinite(value):
        raise AgentError(400, f"字段 {key} 必须是有限数字")
    return value


class MappingAgentHandler(BaseHTTPRequestHandler):
    server_version = "LRMappingAgent/1.0"

    @property
    def ros(self) -> RosInterface:
        return self.server.ros  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 沿用基类签名
        log(f"HTTP {self.client_address[0]} {format % args}")

    # -- 响应辅助 -----------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_png(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, detail: str) -> None:
        self._send_json(status, {"detail": detail})

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise AgentError(400, "Content-Length 非法") from None
        if length > MAX_BODY_BYTES:
            raise AgentError(413, "请求体过大")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise AgentError(400, "请求体不是合法 JSON") from None
        if not isinstance(body, dict):
            raise AgentError(400, "请求体必须是 JSON 对象")
        return body

    def _dispatch(self, routes: dict) -> None:
        handler = routes.get(self.path)
        try:
            if handler is None:
                raise AgentError(404, "未知端点")
            handler()
        except AgentError as error:
            self._error(error.status, error.detail)
        except RuntimeError as error:
            log(f"请求处理失败: {error}")
            self._error(500, str(error))
        except Exception as error:
            log(f"请求处理异常: {error!r}")
            self._error(500, f"内部错误: {error}")

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._dispatch(
            {
                "/mapping/status": self._handle_status,
                "/mapping/pose": self._handle_pose,
                "/mapping/loc-overlay": self._handle_loc_overlay,
                "/mapping/marks": self._handle_marks,
                "/mapping/snapshot.png": self._handle_snapshot,
            }
        )

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch(
            {
                "/mapping/start": self._handle_start,
                "/mapping/stop": self._handle_stop,
                "/mapping/jog": self._handle_jog,
                "/mapping/mark": self._handle_mark,
                "/mapping/marks/clear": self._handle_marks_clear,
                "/mapping/save": self._handle_save,
            }
        )

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        routes = {}
        if re.fullmatch(r"/mapping/marks/\d+", path):
            routes[path] = self._handle_mark_delete
        self._dispatch(routes)

    # -- 端点 ---------------------------------------------------------------

    def _handle_status(self) -> None:
        mode = current_mode()
        age = self.ros.map_age()
        slam_running = mode == "mapping" and age is not None and age <= MAP_FRAME_STALE_SEC
        messages = {
            "navigation": "导航模式：lr-robot-runtime 运行中",
            "mapping": "建图模式：SLAM 输出正常" if slam_running else "建图模式：等待 SLAM 地图帧",
            "transition": "模式切换中：导航与建图栈均未运行",
        }
        self._send_json(
            200,
            {
                "mode": mode,
                "slamRunning": slam_running,
                "slamRestarted": self.ros.slam_restarted(),
                "message": messages[mode],
            },
        )

    def _handle_start(self) -> None:
        switch_to_mapping(self.ros)
        self._send_json(200, {"ok": True, "mode": "mapping"})

    def _handle_stop(self) -> None:
        switch_to_navigation(self.ros)
        self._send_json(200, {"ok": True, "mode": "navigation"})

    def _handle_jog(self) -> None:
        if current_mode() != "mapping":
            raise AgentError(409, "当前不在建图模式，拒绝 jog")
        body = self._read_json()
        linear = _clamp(_finite_float(body, "linear"), MAX_LINEAR_MPS)
        angular = _clamp(_finite_float(body, "angular"), MAX_ANGULAR_RPS)
        self.ros.set_jog(linear, angular)
        self._send_json(200, {"ok": True, "linear": linear, "angular": angular})

    def _handle_pose(self) -> None:
        if current_mode() != "mapping":
            raise AgentError(409, "当前不在建图模式，位姿不可用")
        pose = self.ros.current_pose()
        if pose is None:
            raise AgentError(503, "位姿暂不可用")
        x, y, yaw = pose
        self._send_json(200, {"ok": True, "x": x, "y": y, "yaw": yaw})

    def _handle_loc_overlay(self) -> None:
        # 导航/建图模式均可用，不做模式检查；错误按前端约定返回
        # {"ok": false, "error"}，不走统一 {"detail"} 格式
        payload, error = self.ros.loc_overlay()
        if payload is None:
            self._send_json(503, {"ok": False, "error": error})
            return
        self._send_json(200, payload)

    def _handle_mark(self) -> None:
        if current_mode() != "mapping":
            raise AgentError(409, "当前不在建图模式，无法记录标注")
        body = self._read_json()
        label = body.get("label")
        if label is not None:
            if not isinstance(label, str):
                raise AgentError(400, "label 必须是字符串")
            label = label.strip() or None  # 空串按未提供处理，自动命名
        result = self.ros.add_mark(label)
        if result is None:
            raise AgentError(503, "位姿暂不可用")
        mark, count = result
        self._send_json(201, {"mark": mark, "count": count})

    def _handle_marks(self) -> None:
        self._send_json(200, {"marks": self.ros.marks()})

    def _handle_mark_delete(self) -> None:
        index = int(self.path.split("?", 1)[0].rsplit("/", 1)[1])
        try:
            removed = self.ros.delete_mark(index)
        except IndexError:
            raise AgentError(404, f"标注序号越界: {index}") from None
        self._send_json(200, {"ok": True, "removed": removed, "count": len(self.ros.marks())})

    def _handle_marks_clear(self) -> None:
        self._send_json(200, {"ok": True, "cleared": self.ros.clear_marks()})

    def _handle_snapshot(self) -> None:
        message = self.ros.latest_map()
        if message is None:
            raise AgentError(404, "SLAM 地图首帧尚未到达")
        self._send_png(occupancy_grid_to_png(message))

    def _handle_save(self) -> None:
        body = self._read_json()
        name = body.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 80):
            raise AgentError(400, "name 必须是 1-80 字符的字符串")
        with _map_save_lock:
            cached = self.ros.saved_map_response()
            if cached is not None:
                # 同一建图会话已保存成功过：直接返回首次结果，不重复 map_saver、
                # 不重复注册、不新生成 ID（前端网络重试也能拿回完整首响应）
                cached["alreadySaved"] = True
                log(f"本次建图会话已保存过地图 {cached['mapId']}，幂等返回")
                self._send_json(200, cached)
                return
            result = save_current_map()
            result["marks"] = self.ros.marks()  # 原样带出书架标注，供控制站创建地图标注
            result["alreadySaved"] = False
            self.ros.record_saved_map(result)
            cleared = self.ros.clear_marks(delete_file=True)  # 保存成功后标注随会话结束
            log(f"地图已保存并注册: {result['mapId']} (name={name!r})，清空 {cleared} 条书架标注")
            self._send_json(200, result)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    host = os.environ.get("MAPPING_AGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("MAPPING_AGENT_PORT", "18090"))

    rclpy.init(args=None)
    ros = RosInterface()

    # 代理重启时若仍处于建图模式，恢复上次会话持久化的书架标注
    try:
        if current_mode() == "mapping":
            restored = ros.restore_marks()
            if restored:
                log(f"代理重启时仍处于建图模式，恢复 {restored} 条书架标注")
    except Exception as error:
        log(f"启动时模式判定失败，书架标注未恢复: {error}")

    httpd = ThreadingHTTPServer((host, port), MappingAgentHandler)
    httpd.daemon_threads = True
    httpd.ros = ros  # type: ignore[attr-defined]

    def _handle_signal(signum, _frame) -> None:
        log(f"收到信号 {signum}，补一帧零速并退出")
        ros.initiate_shutdown()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log(f"监听 http://{host}:{port}（仅本机回环）")
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        ros.shutdown()
    log("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
