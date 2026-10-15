#!/usr/bin/env python3
"""通过 Waveshare DDSM Driver HAT (A) 手动测试 DDSM115 电机的 ROS 2 节点。"""

import json
import threading
import time
from typing import Dict, List, Optional, Tuple

import rclpy
from rclpy.node import Node

try:
    import serial
except ImportError:  # pragma: no cover - 机器人运行环境中处理缺失依赖
    serial = None


DEFAULT_PORT = "/dev/chassis_serial_port"
DEFAULT_IDS = [1, 2, 3, 4]
VALID_DIRECTIONS = {"forward", "backward", "stop"}


class DDSMDriverHat:
    """Waveshare DDSM Driver HAT (A) JSON 串口协议封装。"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.25, logger=None):
        if serial is None:
            raise RuntimeError("未安装 python3-serial，请先安装 ROS 依赖或 pyserial")

        self._logger = logger
        self.ser = serial.Serial(
            port,
            baudrate=baudrate,
            timeout=timeout,
            write_timeout=1.0,
            dsrdtr=None,
        )
        # Waveshare 示例会在打开串口后拉低 RTS/DTR，避免 ESP32 串口控制线影响通信。
        self.ser.setRTS(False)
        self.ser.setDTR(False)
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def close(self) -> None:
        if self.ser.is_open:
            self.ser.close()

    def send(self, command: Dict[str, int], read_time: float = 0.25) -> List[str]:
        """发送一条 JSON 命令，并在短时间窗口内读取 HAT 返回。"""
        payload = json.dumps(command, separators=(",", ":")).encode("utf-8") + b"\n"
        self.ser.write(payload)
        self.ser.flush()
        return self.read_lines(read_time)

    def read_lines(self, read_time: float) -> List[str]:
        """读取 HAT 的文本返回，每行通常是一条 JSON 或状态信息。"""
        lines = []
        deadline = time.monotonic() + read_time
        while time.monotonic() < deadline:
            raw = self.ser.readline()
            if raw:
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    lines.append(text)

        return lines

    def configure_for_ddsm115(self, heartbeat_ms: int) -> None:
        # T=11002 设置电机类型，type=115 对应 DDSM115。
        self.send({"T": 11002, "type": 115})
        # T=11001 设置 HAT 心跳时间，HAT 会用它做安全超时。
        self.send({"T": 11001, "time": heartbeat_ms})

    def set_velocity_mode(self, motor_id: int) -> None:
        # mode=2 为速度模式，对应后续按 rpm 控制。
        self.send({"T": 10012, "id": motor_id, "mode": 2})

    def set_rpm(self, motor_id: int, rpm: int, accel: int) -> List[str]:
        # T=10010 是速度控制命令；cmd 为 rpm，act 为 HAT 的加减速/动作字段。
        return self.send({"T": 10010, "id": motor_id, "cmd": rpm, "act": accel})

    def stop(self, motor_id: int, accel: int) -> List[str]:
        return self.set_rpm(motor_id, 0, accel)

    def info(self, motor_id: int) -> None:
        # T=10032 查询指定电机信息，读取窗口稍长一些。
        self.send({"T": 10032, "id": motor_id}, read_time=0.4)


class DDSMHatMotorTestNode(Node):
    """启动后选中四个电机，通过终端输入控制指定电机。"""

    def __init__(self) -> None:
        super().__init__("ddsm_hat_motor_test")

        # 运行前请确保机器人架空或轮子离地，避免测试动作造成位移。
        self.declare_parameter("port", DEFAULT_PORT)
        self.declare_parameter("baudrate", 115200)
        self.declare_parameter("serial_timeout", 0.25)
        self.declare_parameter("motor_ids", DEFAULT_IDS)
        self.declare_parameter("direction", "stop")
        self.declare_parameter("rpm", 30)
        self.declare_parameter("accel", 3)
        self.declare_parameter("heartbeat_ms", 1000)
        self.declare_parameter("skip_init", False)
        self.declare_parameter("read_info", False)
        self.declare_parameter("auto_start", True)

        self.port = self.get_parameter("port").value
        self.baudrate = int(self.get_parameter("baudrate").value)
        self.serial_timeout = float(self.get_parameter("serial_timeout").value)
        self.motor_ids = self._normalize_motor_ids(self.get_parameter("motor_ids").value)
        self.direction = str(self.get_parameter("direction").value)
        self.rpm = abs(int(self.get_parameter("rpm").value))
        self.accel = int(self.get_parameter("accel").value)
        self.heartbeat_ms = int(self.get_parameter("heartbeat_ms").value)
        self.skip_init = bool(self.get_parameter("skip_init").value)
        self.read_info = bool(self.get_parameter("read_info").value)
        self.auto_start = bool(self.get_parameter("auto_start").value)

        if self.direction not in VALID_DIRECTIONS:
            raise RuntimeError("direction 必须是 forward、backward 或 stop")

        self.command_rpm = self._command_rpm(self.direction, self.rpm)
        self._hat: Optional[DDSMDriverHat] = None
        self._hat_lock = threading.Lock()
        self._running = True
        self._finished = False
        self._test_thread: Optional[threading.Thread] = None

        self.get_logger().info(
            f"DDSM HAT 电机测试节点已启动: port={self.port}, ids={self.motor_ids}, "
            f"default_direction={self.direction}, default_rpm={self.rpm}, accel={self.accel}"
        )

        if self.auto_start:
            self._test_thread = threading.Thread(target=self._run_test_once, daemon=True)
            self._test_thread.start()

    def destroy_node(self) -> bool:
        self._running = False
        if self._test_thread is not None and self._test_thread.is_alive():
            self._test_thread.join(timeout=2.0)
        self._stop_selected_motors()
        if self._hat is not None:
            self._hat.close()
            self._hat = None
        return super().destroy_node()

    def is_finished(self) -> bool:
        return self._finished

    def _normalize_motor_ids(self, raw_value) -> List[int]:
        """兼容 ROS 参数中的数组，也兼容字符串 'all' 或 '1,2,3'。"""
        if isinstance(raw_value, str):
            values = [item.strip() for item in raw_value.replace(",", " ").split()]
            if len(values) == 1 and values[0].lower() == "all":
                return list(DEFAULT_IDS)
        else:
            values = list(raw_value)

        motor_ids = []
        for value in values:
            if isinstance(value, str) and value.lower() == "all":
                motor_ids.extend(DEFAULT_IDS)
                continue
            motor_id = int(value)
            if motor_id not in DEFAULT_IDS:
                raise RuntimeError("motor_ids 只能包含 1、2、3、4，或使用字符串 'all'")
            motor_ids.append(motor_id)

        return sorted(set(motor_ids))

    def _command_rpm(self, direction: str, rpm: int) -> int:
        if direction == "backward":
            return -rpm
        if direction == "stop":
            return 0
        return rpm

    def _run_test_once(self) -> None:
        try:
            self._hat = DDSMDriverHat(
                self.port,
                baudrate=self.baudrate,
                timeout=self.serial_timeout,
                logger=self.get_logger(),
            )
            self.get_logger().warn("开始电机测试，请确认机器人已架空或轮子离地")

            if not self.skip_init:
                self._hat.configure_for_ddsm115(self.heartbeat_ms)
                for motor_id in self.motor_ids:
                    self._hat.set_velocity_mode(motor_id)

            # 启动后先让已选电机停止，之后完全由终端输入控制每个电机。
            for motor_id in self.motor_ids:
                lines = self._hat.set_rpm(motor_id, self.command_rpm, self.accel)
                self._log_motor_result(motor_id, self.direction, self.command_rpm, lines)

            if self.read_info:
                for motor_id in self.motor_ids:
                    self._hat.info(motor_id)

            self._log_input_help()
            self._input_loop()
        except Exception as exc:
            self.get_logger().error(f"DDSM HAT 电机测试失败: {exc}")
        finally:
            self._stop_selected_motors()
            if self._hat is not None:
                self._hat.close()
                self._hat = None
            self._finished = True

    def _stop_selected_motors(self) -> None:
        if self._hat is None:
            return
        for motor_id in self.motor_ids:
            try:
                with self._hat_lock:
                    lines = self._hat.stop(motor_id, self.accel)
                    self._log_motor_result(motor_id, "stop", 0, lines)
            except Exception as exc:  # pragma: no cover - 关闭阶段尽力停止
                self.get_logger().warn(f"ID {motor_id}: 停止失败: {exc}")

    def _log_input_help(self) -> None:
        self.get_logger().info("输入命令控制电机: <id|all> <forward|backward|stop> [rpm]")
        self.get_logger().info("示例: 1 forward, 2 backward 50, 3 stop, all stop, q")

    def _input_loop(self) -> None:
        while self._running and rclpy.ok():
            try:
                line = input("ddsm_hat> ").strip()
            except EOFError:
                self.get_logger().warn("标准输入已关闭，停止所有已选电机并退出")
                return

            if not line:
                continue
            if line.lower() in {"q", "quit", "exit"}:
                self.get_logger().info("收到退出命令，停止所有已选电机")
                return
            if line.lower() in {"help", "h", "?"}:
                self._log_input_help()
                continue

            try:
                target_ids, direction, rpm = self._parse_input_command(line)
            except ValueError as exc:
                self.get_logger().warn(str(exc))
                self._log_input_help()
                continue

            command_rpm = self._command_rpm(direction, rpm)
            for motor_id in target_ids:
                self._send_motor_command(motor_id, direction, command_rpm)

    def _parse_input_command(self, line: str) -> tuple[List[int], str, int]:
        parts = line.replace(",", " ").split()
        if len(parts) not in (2, 3):
            raise ValueError("命令格式错误")

        target_ids = self._parse_target_ids(parts[0])
        direction = self._parse_direction(parts[1])
        rpm = self.rpm if len(parts) == 2 else abs(int(parts[2]))
        return target_ids, direction, rpm

    def _parse_target_ids(self, value: str) -> List[int]:
        if value.lower() == "all":
            return list(self.motor_ids)

        motor_id = int(value)
        if motor_id not in self.motor_ids:
            raise ValueError(f"电机 ID {motor_id} 未被选中，当前可用: {self.motor_ids}")
        return [motor_id]

    def _parse_direction(self, value: str) -> str:
        direction_aliases = {
            "f": "forward",
            "forward": "forward",
            "front": "forward",
            "前": "forward",
            "正": "forward",
            "b": "backward",
            "back": "backward",
            "backward": "backward",
            "后": "backward",
            "反": "backward",
            "s": "stop",
            "stop": "stop",
            "0": "stop",
            "停": "stop",
        }
        direction = direction_aliases.get(value.lower())
        if direction is None:
            raise ValueError("方向必须是 forward、backward 或 stop")
        return direction

    def _send_motor_command(self, motor_id: int, direction: str, rpm: int) -> None:
        if self._hat is None:
            raise RuntimeError("HAT 串口未打开")

        with self._hat_lock:
            lines = self._hat.set_rpm(motor_id, rpm, self.accel)
        self._log_motor_result(motor_id, direction, rpm, lines)

    def _log_motor_result(
        self,
        motor_id: int,
        direction: str,
        published_rpm: int,
        lines: List[str],
    ) -> None:
        reply_id, returned_rpm = self._extract_motor_speed(lines, motor_id)
        display_id = reply_id if reply_id is not None else motor_id
        display_rpm = returned_rpm if returned_rpm is not None else "无反馈"
        self.get_logger().info(
            f"ID: {display_id}, 朝向: {direction}, 发布的转速: {published_rpm}, 返回的转速: {display_rpm}"
        )

    def _extract_motor_speed(self, lines: List[str], expected_id: int) -> Tuple[Optional[int], Optional[float]]:
        """从 HAT 返回行中提取电机 ID 和速度字段。"""
        fallback_id = None
        fallback_rpm = None
        for line in lines:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue

            motor_id = self._read_int_field(data, ("id", "ID"))
            rpm = self._read_float_field(data, ("spd", "rpm", "speed"))
            if motor_id == expected_id and rpm is not None:
                return motor_id, rpm
            if motor_id is not None or rpm is not None:
                fallback_id = motor_id
                fallback_rpm = rpm
        return fallback_id, fallback_rpm

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
    node = DDSMHatMotorTestNode()
    try:
        while rclpy.ok() and not node.is_finished():
            rclpy.spin_once(node, timeout_sec=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
