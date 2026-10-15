#!/usr/bin/env python3
"""ROS 2 differential drive controller for DDSM115 four-wheel bases."""

import json
import math
import struct
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float32MultiArray
from std_msgs.msg import String

try:
    import serial
except ImportError:  # pragma: no cover - handled at runtime on robot
    serial = None


WHEEL_NAMES = [
    "left_front_wheel",
    "right_front_wheel",
    "left_rear_wheel",
    "right_rear_wheel",
]


class DDSMDriverHat:
    """Small JSON serial client for Waveshare DDSM Driver HAT (A)."""

    def __init__(self, port: str, baudrate: int, timeout: float, logger):
        if serial is None:
            raise RuntimeError("python3-serial is not installed")

        self._logger = logger
        self._serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=timeout,
            write_timeout=timeout,
            dsrdtr=None,
        )
        self._serial.setRTS(False)
        self._serial.setDTR(False)
        time.sleep(0.2)
        self._serial.reset_input_buffer()

    def close(self) -> None:
        if self._serial.is_open:
            self._serial.close()

    def send(self, command: Dict[str, int], log_debug: bool = False) -> None:
        payload = json.dumps(command, separators=(",", ":")).encode("utf-8") + b"\n"
        if log_debug:
            self._logger.debug(f"serial tx: {payload.decode('utf-8').strip()}")
        self._serial.write(payload)

    def send_batch(self, commands: List[Dict[str, int]], log_debug: bool = False) -> None:
        payload = b"".join(
            json.dumps(command, separators=(",", ":")).encode("utf-8") + b"\n"
            for command in commands
        )
        if log_debug:
            self._logger.debug(f"serial tx batch: {payload.decode('utf-8').strip()}")
        self._serial.write(payload)

    def send_commands(self, commands: List[Dict[str, int]], gap_s: float, log_debug: bool = False) -> None:
        for command in commands:
            self.send(command, log_debug)
            self.flush()
            if gap_s > 0.0:
                time.sleep(gap_s)

    def flush(self) -> None:
        self._serial.flush()

    def readline(self) -> bytes:
        return self._serial.readline()


def crc8_maxim(data: bytes) -> int:
    """CRC-8/MAXIM used by the DDSM115 binary protocol."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x01:
                crc = (crc >> 1) ^ 0x8C
            else:
                crc >>= 1
            crc &= 0xFF
    return crc


def attach_crc(frame_without_crc: bytes) -> bytes:
    return frame_without_crc + bytes([crc8_maxim(frame_without_crc)])


def int16_to_bytes(value: int) -> Tuple[int, int]:
    packed = struct.pack(">h", int(value))
    return packed[0], packed[1]


def bytes_to_int16(high: int, low: int) -> int:
    return struct.unpack(">h", bytes([high, low]))[0]


class DDSM115Rs485Bus:
    """Direct DDSM115 binary-protocol client over the HAT USB port or RS485."""

    MODE_VELOCITY = 0x02

    def __init__(
        self,
        port: str,
        baudrate: int,
        serial_timeout: float,
        response_timeout: float,
        max_rpm: int,
        logger,
        log_raw_tx: bool = False,
        log_raw_rx: bool = False,
        mode_command_uses_crc: bool = False,
        feedback_request_on_timeout: bool = True,
    ):
        if serial is None:
            raise RuntimeError("python3-serial is not installed")

        self._logger = logger
        self._serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=serial_timeout,
            write_timeout=max(serial_timeout, 0.5),
        )
        self.response_timeout = response_timeout
        self.max_rpm = max_rpm
        self.log_raw_tx = log_raw_tx
        self.log_raw_rx = log_raw_rx
        self.mode_command_uses_crc = mode_command_uses_crc
        self.feedback_request_on_timeout = feedback_request_on_timeout

    def close(self) -> None:
        if self._serial.is_open:
            self._serial.close()

    def set_velocity_mode(self, motor_id: int) -> None:
        if self.mode_command_uses_crc:
            frame = attach_crc(bytes([motor_id, 0xA0, 0, 0, 0, 0, 0, 0, self.MODE_VELOCITY]))
        else:
            frame = bytes([motor_id, 0xA0, 0, 0, 0, 0, 0, 0, 0, self.MODE_VELOCITY])
        self._write_frame(frame)

    def send_commands(
        self,
        commands: Sequence[Dict[str, int]],
        gap_s: float,
    ) -> List[Tuple[int, float]]:
        feedback = []
        for command in commands:
            motor_id = int(command["id"])
            rpm = max(-self.max_rpm, min(self.max_rpm, int(command["cmd"])))
            accel = int(command.get("act", 0)) & 0xFF
            reply = self.set_rpm(motor_id, rpm, accel)
            if reply is None and self.feedback_request_on_timeout:
                reply = self.request_feedback(motor_id)
            parsed = self.parse_feedback(reply, motor_id)
            if parsed is not None:
                feedback.append(parsed)
            if gap_s > 0.0:
                time.sleep(gap_s)
        return feedback

    def set_rpm(self, motor_id: int, rpm: int, accel: int = 0) -> Optional[bytes]:
        high, low = int16_to_bytes(rpm)
        frame = attach_crc(bytes([motor_id, 0x64, high, low, 0, 0, accel & 0xFF, 0, 0]))
        self._serial.reset_input_buffer()
        self._write_frame(frame)
        return self._read_reply(motor_id)

    def brake(self, motor_id: int) -> Optional[bytes]:
        frame = attach_crc(bytes([motor_id, 0x64, 0, 0, 0, 0, 0, 0xFF, 0]))
        self._serial.reset_input_buffer()
        self._write_frame(frame)
        return self._read_reply(motor_id)

    def request_feedback(self, motor_id: int) -> Optional[bytes]:
        frame = attach_crc(bytes([motor_id, 0x74, 0, 0, 0, 0, 0, 0, 0]))
        self._serial.reset_input_buffer()
        self._write_frame(frame)
        return self._read_reply(motor_id)

    def parse_feedback(self, reply: Optional[bytes], expected_id: int) -> Optional[Tuple[int, float]]:
        if reply is None or len(reply) != 10:
            return None
        if reply[0] != expected_id or reply[1] != self.MODE_VELOCITY:
            return None
        if crc8_maxim(reply[:9]) != reply[9]:
            return None
        rpm = float(bytes_to_int16(reply[4], reply[5]))
        return expected_id, rpm

    def _write_frame(self, frame: bytes) -> None:
        if self.log_raw_tx:
            self._logger.debug(f"rs485 tx: {frame.hex(' ')}")
        self._serial.write(frame)
        self._serial.flush()

    def _read_reply(self, motor_id: int) -> Optional[bytes]:
        deadline = time.monotonic() + self.response_timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            raw = self._serial.read(1)
            if not raw:
                continue
            byte = raw[0]
            if not buf:
                if byte == motor_id:
                    buf.append(byte)
                continue
            if len(buf) == 1 and byte != self.MODE_VELOCITY:
                buf.clear()
                if byte == motor_id:
                    buf.append(byte)
                continue
            buf.append(byte)
            if len(buf) == 10:
                frame = bytes(buf)
                if self.log_raw_rx:
                    self._logger.debug(f"rs485 rx: {frame.hex(' ')}")
                if crc8_maxim(frame[:9]) == frame[9]:
                    return frame
                buf.clear()
        return None


class DDSMHatDiffDriveNode(Node):
    def __init__(self) -> None:
        super().__init__("ddsm_hat_diff_drive")

        self.declare_parameter("port", "/dev/chassis_serial_port")
        self.declare_parameter("baudrate", 115200)
        self.declare_parameter("driver_backend", "hat_json")
        self.declare_parameter("wheel_radius", 0.05)
        self.declare_parameter("wheel_track", 0.33)
        self.declare_parameter("max_rpm", 330)
        self.declare_parameter("accel_rpm_per_sec", 600.0)
        self.declare_parameter("command_rate_hz", 30.0)
        self.declare_parameter("cmd_vel_timeout", 0.5)
        self.declare_parameter("heartbeat_ms", 1000)
        self.declare_parameter("hat_act", 3)
        self.declare_parameter("motor_ids", [1, 2, 3, 4])
        self.declare_parameter("motor_signs", [1, -1, 1, -1])
        self.declare_parameter("command_order", [3, 2, 1, 4])
        self.declare_parameter("cmd_vel_topic", "/cmd_vel")
        self.declare_parameter("wheel_speed_topic", "/wheel_speeds")
        self.declare_parameter("joint_state_topic", "/wheel_joint_states")
        self.declare_parameter("wheel_speed_source_topic", "/wheel_speed_source")
        self.declare_parameter("use_motor_feedback", True)
        self.declare_parameter("feedback_timeout", 0.25)
        self.declare_parameter("init_hat", True)
        self.declare_parameter("log_serial_tx", False)
        self.declare_parameter("log_serial_rx", False)
        self.declare_parameter("per_motor_command_gap", 0.003)
        self.declare_parameter("feedback_log_rate_hz", 0.0)
        self.declare_parameter("rs485_serial_timeout", 0.02)
        self.declare_parameter("rs485_response_timeout", 0.08)
        self.declare_parameter("rs485_set_velocity_mode", True)
        self.declare_parameter("rs485_brake_on_stop", False)
        self.declare_parameter("rs485_feedback_request_on_timeout", True)
        self.declare_parameter("rs485_mode_command_uses_crc", False)
        self.declare_parameter("rs485_log_raw_tx", False)
        self.declare_parameter("rs485_log_raw_rx", False)
        self.declare_parameter("serial_reconnect_period", 1.0)
        self.declare_parameter("feedback_health_timeout", 2.0)

        self.port = self.get_parameter("port").value
        self.baudrate = int(self.get_parameter("baudrate").value)
        self.driver_backend = str(self.get_parameter("driver_backend").value)
        self.wheel_radius = float(self.get_parameter("wheel_radius").value)
        self.wheel_track = float(self.get_parameter("wheel_track").value)
        self.max_rpm = int(self.get_parameter("max_rpm").value)
        self.accel_rpm_per_sec = float(self.get_parameter("accel_rpm_per_sec").value)
        self.command_rate_hz = float(self.get_parameter("command_rate_hz").value)
        self.cmd_vel_timeout = float(self.get_parameter("cmd_vel_timeout").value)
        self.heartbeat_ms = int(self.get_parameter("heartbeat_ms").value)
        self.hat_act = int(self.get_parameter("hat_act").value)
        self.motor_ids = [int(x) for x in self.get_parameter("motor_ids").value]
        self.motor_signs = [int(x) for x in self.get_parameter("motor_signs").value]
        self.command_order = [int(x) for x in self.get_parameter("command_order").value]
        self.cmd_vel_topic = self.get_parameter("cmd_vel_topic").value
        self.wheel_speed_topic = self.get_parameter("wheel_speed_topic").value
        self.joint_state_topic = self.get_parameter("joint_state_topic").value
        self.wheel_speed_source_topic = self.get_parameter("wheel_speed_source_topic").value
        self.use_motor_feedback = bool(self.get_parameter("use_motor_feedback").value)
        self.feedback_timeout = float(self.get_parameter("feedback_timeout").value)
        self.init_hat = bool(self.get_parameter("init_hat").value)
        self.log_serial_tx = bool(self.get_parameter("log_serial_tx").value)
        self.log_serial_rx = bool(self.get_parameter("log_serial_rx").value)
        self.per_motor_command_gap = float(self.get_parameter("per_motor_command_gap").value)
        self.feedback_log_rate_hz = float(self.get_parameter("feedback_log_rate_hz").value)
        self.rs485_serial_timeout = float(self.get_parameter("rs485_serial_timeout").value)
        self.rs485_response_timeout = float(self.get_parameter("rs485_response_timeout").value)
        self.rs485_set_velocity_mode = bool(self.get_parameter("rs485_set_velocity_mode").value)
        self.rs485_brake_on_stop = bool(self.get_parameter("rs485_brake_on_stop").value)
        self.rs485_feedback_request_on_timeout = bool(
            self.get_parameter("rs485_feedback_request_on_timeout").value
        )
        self.rs485_mode_command_uses_crc = bool(
            self.get_parameter("rs485_mode_command_uses_crc").value
        )
        self.rs485_log_raw_tx = bool(self.get_parameter("rs485_log_raw_tx").value)
        self.rs485_log_raw_rx = bool(self.get_parameter("rs485_log_raw_rx").value)
        self.serial_reconnect_period = float(
            self.get_parameter("serial_reconnect_period").value
        )
        self.feedback_health_timeout = float(
            self.get_parameter("feedback_health_timeout").value
        )

        if len(self.motor_ids) != 4 or len(self.motor_signs) != 4:
            raise RuntimeError("motor_ids and motor_signs must both contain four values")
        if sorted(self.command_order) != sorted(self.motor_ids):
            raise RuntimeError("command_order must contain the same IDs as motor_ids")
        if self.wheel_radius <= 0.0 or self.wheel_track <= 0.0:
            raise RuntimeError("wheel_radius and wheel_track must be positive")
        if self.command_rate_hz <= 0.0:
            raise RuntimeError("command_rate_hz must be positive")
        if self.per_motor_command_gap < 0.0:
            raise RuntimeError("per_motor_command_gap must be greater than or equal to zero")
        if self.feedback_log_rate_hz < 0.0:
            raise RuntimeError("feedback_log_rate_hz must be greater than or equal to zero")
        if self.rs485_serial_timeout <= 0.0 or self.rs485_response_timeout <= 0.0:
            raise RuntimeError("rs485 timeouts must be positive")
        if self.serial_reconnect_period <= 0.0:
            raise RuntimeError("serial_reconnect_period must be positive")
        if self.feedback_health_timeout <= 0.0:
            raise RuntimeError("feedback_health_timeout must be positive")
        if self.driver_backend not in ("hat_json", "ddsm_rs485"):
            raise RuntimeError("driver_backend must be 'hat_json' or 'ddsm_rs485'")

        self._cmd_lock = threading.Lock()
        self._linear = 0.0
        self._angular = 0.0
        self._last_cmd_time = self.get_clock().now()
        self._target_rpms = [0.0, 0.0, 0.0, 0.0]
        self._current_rpms = [0.0, 0.0, 0.0, 0.0]
        self._feedback_lock = threading.Lock()
        self._feedback_rpms: List[Optional[float]] = [None, None, None, None]
        self._feedback_times = [0.0, 0.0, 0.0, 0.0]
        self._command_lock = threading.Lock()
        self._last_commanded_rpms = {motor_id: 0 for motor_id in self.motor_ids}
        self._last_tx_time = time.monotonic()
        self._running = True
        self._driver_lock = threading.Lock()
        self._driver = None
        self._last_reconnect_attempt = float("-inf")
        self._feedback_recovery_started_at = time.monotonic()

        self._wheel_speed_pub = self.create_publisher(
            Float32MultiArray, self.wheel_speed_topic, 10
        )
        self._joint_state_pub = self.create_publisher(
            JointState, self.joint_state_topic, 10
        )
        self._wheel_speed_source_pub = self.create_publisher(
            String, self.wheel_speed_source_topic, 10
        )
        self._cmd_sub = self.create_subscription(
            Twist, self.cmd_vel_topic, self._cmd_vel_callback, 10
        )
        self._feedback_log_timer = None
        if self.feedback_log_rate_hz > 0.0:
            self._feedback_log_timer = self.create_timer(
                1.0 / self.feedback_log_rate_hz,
                self._log_feedback_snapshot,
            )
        # A transient USB/serial outage must not kill the command thread.  The
        # loop below keeps attempting to reopen the stable udev device alias.
        self._attempt_driver_reconnect(force=True)
        self._rx_thread = None
        if self.driver_backend == "hat_json":
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self._rx_thread.start()
        self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True)
        self._tx_thread.start()

    def destroy_node(self) -> bool:
        self._running = False
        if hasattr(self, "_tx_thread") and self._tx_thread.is_alive():
            self._tx_thread.join(timeout=1.0)
        if self._rx_thread is not None and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=1.0)
        self._send_stop()
        if hasattr(self, "_driver"):
            self._close_driver()
        return super().destroy_node()

    def _create_driver(self):
        if self.driver_backend == "hat_json":
            return DDSMDriverHat(
                self.port,
                self.baudrate,
                timeout=0.1,
                logger=self.get_logger(),
            )
        return DDSM115Rs485Bus(
            self.port,
            self.baudrate,
            serial_timeout=self.rs485_serial_timeout,
            response_timeout=self.rs485_response_timeout,
            max_rpm=self.max_rpm,
            logger=self.get_logger(),
            log_raw_tx=self.rs485_log_raw_tx,
            log_raw_rx=self.rs485_log_raw_rx,
            mode_command_uses_crc=self.rs485_mode_command_uses_crc,
            feedback_request_on_timeout=self.rs485_feedback_request_on_timeout,
        )

    def _initialize_driver(self, driver) -> None:
        if self.driver_backend == "hat_json":
            driver.send({"T": 11002, "type": 115}, self.log_serial_tx)
            driver.flush()
            time.sleep(0.05)
            driver.send({"T": 11001, "time": self.heartbeat_ms}, self.log_serial_tx)
            driver.flush()
            time.sleep(0.05)
            for motor_id in self.motor_ids:
                driver.send({"T": 10012, "id": motor_id, "mode": 2}, self.log_serial_tx)
            driver.flush()
            time.sleep(0.05)
            return

        if self.rs485_set_velocity_mode:
            for motor_id in self.motor_ids:
                driver.set_velocity_mode(motor_id)
                time.sleep(0.02)
        time.sleep(0.05)

    def _attempt_driver_reconnect(self, force: bool = False) -> bool:
        """Open the motor bus again after a transient serial failure."""
        now = time.monotonic()
        with self._driver_lock:
            if self._driver is not None:
                return True
            if not force and now - self._last_reconnect_attempt < self.serial_reconnect_period:
                return False
            self._last_reconnect_attempt = now

        driver = None
        try:
            driver = self._create_driver()
            if self.init_hat:
                self._initialize_driver(driver)
        except Exception as exc:
            if driver is not None:
                try:
                    driver.close()
                except Exception:
                    pass
            self.get_logger().warn(f"motor serial reconnect failed: {exc}")
            return False

        with self._driver_lock:
            # The transmit loop is the only reconnect owner, but retain this
            # guard so a future callback cannot leak a second open handle.
            if self._driver is None:
                self._driver = driver
                driver = None

        if driver is not None:
            driver.close()
            return True

        self._clear_feedback()
        self._feedback_recovery_started_at = time.monotonic()
        self.get_logger().info(f"motor serial connected: {self.port}")
        return True

    def _mark_driver_unavailable(self, error: Exception) -> None:
        """Fail safe on I/O errors and let the transmit loop reconnect later."""
        with self._driver_lock:
            driver = self._driver
            self._driver = None
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass

        self._current_rpms = [0.0, 0.0, 0.0, 0.0]
        self._target_rpms = [0.0, 0.0, 0.0, 0.0]
        # Do not resume a stale manual or Nav2 velocity when the bus returns.
        # A controller must publish a fresh command after recovery.
        with self._cmd_lock:
            self._linear = 0.0
            self._angular = 0.0
        self._clear_feedback()
        self._feedback_recovery_started_at = time.monotonic()
        self.get_logger().error(f"motor serial failed; outputs stopped and reconnect scheduled: {error}")

    def _close_driver(self) -> None:
        with self._driver_lock:
            driver = self._driver
            self._driver = None
        if driver is not None:
            try:
                driver.close()
            except Exception as exc:  # pragma: no cover - shutdown best effort
                self.get_logger().warn(f"failed to close motor serial: {exc}")

    def _clear_feedback(self) -> None:
        with self._feedback_lock:
            self._feedback_rpms = [None, None, None, None]
            self._feedback_times = [0.0, 0.0, 0.0, 0.0]

    def _cmd_vel_callback(self, msg: Twist) -> None:
        with self._cmd_lock:
            self._linear = float(msg.linear.x)
            self._angular = float(msg.angular.z)
            self._last_cmd_time = self.get_clock().now()

    def _tx_loop(self) -> None:
        period = 1.0 / self.command_rate_hz
        next_wake = time.monotonic()
        while self._running:
            try:
                if self._attempt_driver_reconnect():
                    self._update_targets_from_cmd()
                    self._ramp_current_rpms()
                    self._send_current_rpms()
            except Exception as exc:
                # Never allow a serial exception to silently terminate this
                # background thread: that leaves /odom publishing stale data.
                self._mark_driver_unavailable(exc)

            next_wake += period
            sleep_s = next_wake - time.monotonic()
            if sleep_s > 0.0:
                time.sleep(sleep_s)
            else:
                next_wake = time.monotonic()

    def _update_targets_from_cmd(self) -> None:
        with self._cmd_lock:
            linear = self._linear
            angular = self._angular
            elapsed = (self.get_clock().now() - self._last_cmd_time).nanoseconds / 1e9

        if self.cmd_vel_timeout > 0.0 and elapsed > self.cmd_vel_timeout:
            linear = 0.0
            angular = 0.0

        left_vel = linear - angular * self.wheel_track / 2.0
        right_vel = linear + angular * self.wheel_track / 2.0
        rpm_per_mps = 60.0 / (2.0 * math.pi * self.wheel_radius)
        wheel_rpms = [left_vel * rpm_per_mps, right_vel * rpm_per_mps,
                      left_vel * rpm_per_mps, right_vel * rpm_per_mps]

        self._target_rpms = [
            self._clamp_rpm(wheel_rpms[i] * self.motor_signs[i])
            for i in range(4)
        ]

    def _ramp_current_rpms(self) -> None:
        now = time.monotonic()
        dt = min(now - self._last_tx_time, 0.2)
        self._last_tx_time = now
        max_step = self.accel_rpm_per_sec * dt

        for i in range(4):
            diff = self._target_rpms[i] - self._current_rpms[i]
            if abs(diff) <= max_step:
                self._current_rpms[i] = self._target_rpms[i]
            else:
                self._current_rpms[i] += math.copysign(max_step, diff)

    def _send_current_rpms(self) -> None:
        rpm_by_id = dict(zip(self.motor_ids, self._current_rpms))
        commands = [
            {
                "T": 10010,
                "id": motor_id,
                "cmd": int(round(rpm_by_id[motor_id])),
                "act": self.hat_act,
            }
            for motor_id in self.command_order
        ]
        with self._command_lock:
            self._last_commanded_rpms = {
                command["id"]: command["cmd"]
                for command in commands
            }
        feedback = self._send_batch(commands)
        self._apply_feedback(feedback)
        if self._feedback_is_unhealthy():
            raise RuntimeError(
                f"no valid wheel feedback for {self.feedback_health_timeout:.1f} seconds"
            )
        self._publish_wheel_states()

    def _send_stop(self) -> None:
        commands = [
            {"T": 10010, "id": motor_id, "cmd": 0, "act": self.hat_act}
            for motor_id in self.command_order
        ]
        try:
            with self._command_lock:
                self._last_commanded_rpms = {
                    command["id"]: command["cmd"]
                    for command in commands
                }
            feedback = self._send_batch(commands)
            self._apply_feedback(feedback)
            if self.driver_backend == "ddsm_rs485" and self.rs485_brake_on_stop:
                for motor_id in self.command_order:
                    parsed = self._driver.parse_feedback(
                        self._driver.brake(motor_id),
                        motor_id,
                    )
                    if parsed is not None:
                        self._apply_feedback([parsed])
            self._current_rpms = [0.0, 0.0, 0.0, 0.0]
            self._target_rpms = [0.0, 0.0, 0.0, 0.0]
            self._publish_wheel_states()
        except Exception as exc:  # pragma: no cover - shutdown best effort
            self.get_logger().warn(f"failed to stop motors during shutdown: {exc}")

    def _send_batch(self, commands: List[Dict[str, int]]) -> List[Tuple[int, float]]:
        # HAT 固件在一次写入多条 JSON 时可能只返回最后一条命令的反馈。
        # 逐个电机发送并留出极短间隔，便于读取 1/2/3/4 四个电机的返回转速。
        with self._driver_lock:
            driver = self._driver
        if driver is None:
            raise RuntimeError("motor serial is disconnected")
        if self.driver_backend == "hat_json":
            driver.send_commands(commands, self.per_motor_command_gap, self.log_serial_tx)
            return []
        return driver.send_commands(commands, self.per_motor_command_gap)

    def _apply_feedback(self, feedback: Sequence[Tuple[int, float]]) -> None:
        if not feedback:
            return
        now = time.monotonic()
        with self._feedback_lock:
            for motor_id, rpm in feedback:
                if motor_id not in self.motor_ids:
                    continue
                index = self.motor_ids.index(motor_id)
                self._feedback_rpms[index] = rpm
                self._feedback_times[index] = now

    def _feedback_is_unhealthy(self) -> bool:
        """Require fresh encoder feedback before trusting motion telemetry."""
        if not self.use_motor_feedback or self.driver_backend != "ddsm_rs485":
            return False
        # DDSM115 does not necessarily reply while all wheels are stopped.
        # Treat missing feedback as a fault only when a non-zero command is
        # actually being sent; stationary odometry is safely zero either way.
        if not any(abs(rpm) >= 1.0 for rpm in self._current_rpms):
            return False
        now = time.monotonic()
        if now - self._feedback_recovery_started_at < self.feedback_health_timeout:
            return False
        with self._feedback_lock:
            return any(
                rpm is None or now - received_at > self.feedback_health_timeout
                for rpm, received_at in zip(self._feedback_rpms, self._feedback_times)
            )

    def _clamp_rpm(self, rpm: float) -> float:
        return max(-self.max_rpm, min(self.max_rpm, rpm))

    def _current_physical_wheel_speeds(self) -> Tuple[List[float], List[str]]:
        circumference = 2.0 * math.pi * self.wheel_radius
        speeds = []
        sources = []
        now = time.monotonic()
        with self._feedback_lock:
            feedback_rpms = list(self._feedback_rpms)
            feedback_times = list(self._feedback_times)

        for index, (command_rpm, sign) in enumerate(zip(self._current_rpms, self.motor_signs)):
            rpm_source = 0.0
            source = "feedback_unavailable" if self.use_motor_feedback else "command_fallback"
            if (
                self.use_motor_feedback
                and feedback_rpms[index] is not None
                and now - feedback_times[index] <= self.feedback_timeout
            ):
                rpm_source = feedback_rpms[index]
                source = "feedback"
            elif not self.use_motor_feedback:
                rpm_source = command_rpm
            physical_rpm = rpm_source / sign if sign != 0 else 0.0
            speeds.append(physical_rpm * circumference / 60.0)
            sources.append(source)
        return speeds, sources

    def _publish_wheel_states(self) -> None:
        wheel_speeds, sources = self._current_physical_wheel_speeds()

        speeds_msg = Float32MultiArray()
        speeds_msg.data = [float(value) for value in wheel_speeds]
        self._wheel_speed_pub.publish(speeds_msg)

        joint_msg = JointState()
        joint_msg.header.stamp = self.get_clock().now().to_msg()
        joint_msg.name = WHEEL_NAMES
        joint_msg.position = []
        joint_msg.effort = []
        joint_msg.velocity = [
            float(value / self.wheel_radius) if self.wheel_radius > 0.0 else 0.0
            for value in wheel_speeds
        ]
        self._joint_state_pub.publish(joint_msg)

        source_msg = String()
        source_msg.data = json.dumps(
            dict(zip(WHEEL_NAMES, sources)),
            separators=(",", ":"),
        )
        self._wheel_speed_source_pub.publish(source_msg)

    def _rx_loop(self) -> None:
        while self._running and rclpy.ok():
            with self._driver_lock:
                driver = self._driver
            if driver is None:
                time.sleep(0.05)
                continue
            try:
                raw = driver.readline()
            except Exception as exc:
                if self._running:
                    self._mark_driver_unavailable(exc)
                continue

            if not raw:
                continue

            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            parsed = self._parse_feedback_line(line)
            if parsed is None:
                continue

            motor_id, rpm = parsed
            if motor_id not in self.motor_ids:
                continue
            index = self.motor_ids.index(motor_id)
            with self._feedback_lock:
                self._feedback_rpms[index] = rpm
                self._feedback_times[index] = time.monotonic()
            self._publish_wheel_states()

    def _parse_feedback_line(self, line: str) -> Optional[Tuple[int, float]]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        motor_id = self._read_int_field(data, ("id", "ID"))
        rpm = self._read_float_field(data, ("spd", "rpm", "speed"))
        if motor_id is None or rpm is None:
            return None

        return motor_id, rpm

    def _last_published_rpm(self, motor_id: int) -> int:
        with self._command_lock:
            return int(self._last_commanded_rpms.get(motor_id, 0))

    def _direction_from_rpm(self, rpm: int) -> str:
        if rpm > 0:
            return "forward"
        if rpm < 0:
            return "backward"
        return "stop"

    def _log_feedback_snapshot(self) -> None:
        now = time.monotonic()
        with self._command_lock:
            commanded_rpms = dict(self._last_commanded_rpms)
        with self._feedback_lock:
            feedback_rpms = list(self._feedback_rpms)
            feedback_times = list(self._feedback_times)

        for index, motor_id in enumerate(self.motor_ids):
            published_rpm = int(commanded_rpms.get(motor_id, 0))
            returned_rpm = "无反馈"
            if (
                feedback_rpms[index] is not None
                and now - feedback_times[index] <= self.feedback_timeout
            ):
                returned_rpm = feedback_rpms[index]
            self.get_logger().info(
                f"ID：{motor_id}，朝向：{self._direction_from_rpm(published_rpm)}，"
                f"发布的转速：{published_rpm}，返回的转速：{returned_rpm}"
            )

    def _read_int_field(self, data: Dict[str, object], names: Tuple[str, ...]) -> Optional[int]:
        for name in names:
            value = data.get(name)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        return None

    def _read_float_field(self, data: Dict[str, object], names: Tuple[str, ...]) -> Optional[float]:
        for name in names:
            value = data.get(name)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return None


def main(args: Optional[List[str]] = None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = DDSMHatDiffDriveNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
