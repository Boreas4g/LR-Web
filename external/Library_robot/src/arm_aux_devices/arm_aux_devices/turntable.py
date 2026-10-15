#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

import rclpy
from rclpy.node import Node
from std_msgs.msg import Empty, Float64, Float64MultiArray, String

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError:  # pragma: no cover - reported at runtime on robot
    serial = None
    list_ports = None


@dataclass(frozen=True)
class SerialBridgeConfig:
    port: str
    baudrate: int
    timeout: float
    write_timeout: float
    open_delay_s: float


@dataclass(frozen=True)
class SerialCommand:
    position: float = 0.0
    kp: float = 2.0
    velocity: float = 0.0
    kd: float = 0.1


def normalize_serial_port(port: str) -> str:
    stripped = port.strip()
    if not stripped:
        return stripped
    if "://" in stripped or stripped.upper().startswith("COM") or stripped.startswith("/"):
        return stripped
    if stripped.startswith(("ttyACM", "ttyUSB", "ttyS")):
        return f"/dev/{stripped}"
    return stripped


def preferred_default_port() -> str:
    if Path("/dev/ttyACM2").exists():
        return "/dev/ttyACM2"
    if Path("/dev").exists():
        return "/dev/ttyACM0"
    return "COM13"


def detect_default_port(preferred: str | None = None) -> str:
    preferred = preferred or preferred_default_port()
    if list_ports is None:
        return preferred
    ports = {port.device.upper(): port.device for port in list_ports.comports()}
    normalized_preferred = normalize_serial_port(preferred)
    if normalized_preferred.upper() in ports:
        return ports[normalized_preferred.upper()]
    if preferred.upper() in ports:
        return ports[preferred.upper()]
    if ports:
        return ports[sorted(ports)[0]]
    return normalized_preferred


def format_bridge_command(command: SerialCommand) -> str:
    return (
        f"{command.position:.3f},"
        f"{command.kp:.3f},"
        f"{command.velocity:.3f},"
        f"{command.kd:.3f}\n"
    )


def open_serial_port(config: SerialBridgeConfig) -> Any:
    if serial is None:
        raise RuntimeError("pyserial is not installed. Install package python3-serial or pip install pyserial.")

    port = normalize_serial_port(config.port)
    kwargs = {
        "baudrate": config.baudrate,
        "timeout": config.timeout,
        "write_timeout": config.write_timeout,
    }
    if "://" in port:
        conn = serial.serial_for_url(port, **kwargs)
    else:
        conn = serial.Serial(port, **kwargs)

    for attr, value in (("dtr", True), ("rts", False)):
        try:
            setattr(conn, attr, value)
        except Exception:
            pass

    for method_name in ("reset_input_buffer", "reset_output_buffer"):
        method = getattr(conn, method_name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
    return conn


class Turntable(Node):
    """ROS 2 serial bridge for the turntable firmware used by motor_can_gui.py."""

    def __init__(self) -> None:
        super().__init__("Turntable")

        self.declare_parameter("port", detect_default_port())
        self.declare_parameter("baudrate", 115200)
        self.declare_parameter("timeout", 0.02)
        self.declare_parameter("write_timeout", 1.0)
        self.declare_parameter("open_delay_s", 2.0)
        self.declare_parameter("send_hz", 20.0)
        self.declare_parameter("continuous_send", False)
        self.declare_parameter("default_position", 0.0)
        self.declare_parameter("default_kp", 2.0)
        self.declare_parameter("default_velocity", 0.0)
        self.declare_parameter("default_kd", 0.1)

        self.connection: Any = None
        self.tx_count = 0
        self.rx_count = 0
        self.current_command = self._default_command()

        self.serial_line_pub = self.create_publisher(String, "~/serial_line", 10)
        self.last_command_pub = self.create_publisher(String, "~/last_command", 10)

        self.create_subscription(Float64, "~/position", self._position_callback, 10)
        self.create_subscription(Float64MultiArray, "~/command", self._command_callback, 10)
        self.create_subscription(Empty, "~/relax", self._relax_callback, 10)

        self._open_connection()

        timeout = max(float(self.get_parameter("timeout").value), 0.001)
        self.read_timer = self.create_timer(timeout, self._read_once)

        send_hz = max(float(self.get_parameter("send_hz").value), 1.0)
        self.send_timer = self.create_timer(1.0 / send_hz, self._send_periodic)

    def destroy_node(self) -> bool:
        self._close_connection()
        return super().destroy_node()

    def _default_command(self) -> SerialCommand:
        return SerialCommand(
            position=float(self.get_parameter("default_position").value),
            kp=float(self.get_parameter("default_kp").value),
            velocity=float(self.get_parameter("default_velocity").value),
            kd=float(self.get_parameter("default_kd").value),
        )

    def _config(self) -> SerialBridgeConfig:
        return SerialBridgeConfig(
            port=normalize_serial_port(str(self.get_parameter("port").value)),
            baudrate=int(self.get_parameter("baudrate").value),
            timeout=float(self.get_parameter("timeout").value),
            write_timeout=float(self.get_parameter("write_timeout").value),
            open_delay_s=float(self.get_parameter("open_delay_s").value),
        )

    def _open_connection(self) -> None:
        config = self._config()
        try:
            self.connection = open_serial_port(config)
            if config.open_delay_s > 0:
                time.sleep(config.open_delay_s)
            self.get_logger().info(
                "Connected turntable serial bridge: port=%s baudrate=%d timeout=%.3fs"
                % (normalize_serial_port(config.port), config.baudrate, config.timeout)
            )
        except Exception as exc:
            self.connection = None
            self.get_logger().error(f"Failed to open turntable serial bridge: {exc}")

    def _close_connection(self) -> None:
        if self.connection is None:
            return
        for method_name in ("cancel_write", "reset_output_buffer"):
            method = getattr(self.connection, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
        try:
            self.connection.close()
        except Exception:
            pass
        self.connection = None

    def _position_callback(self, msg: Float64) -> None:
        self.current_command = SerialCommand(
            position=float(msg.data),
            kp=float(self.get_parameter("default_kp").value),
            velocity=float(self.get_parameter("default_velocity").value),
            kd=float(self.get_parameter("default_kd").value),
        )
        self._send_command(self.current_command)

    def _command_callback(self, msg: Float64MultiArray) -> None:
        if len(msg.data) != 4:
            self.get_logger().warning(
                "~/command expects Float64MultiArray data=[position, kp, velocity, kd]; got %d values"
                % len(msg.data)
            )
            return
        self.current_command = SerialCommand(
            position=float(msg.data[0]),
            kp=float(msg.data[1]),
            velocity=float(msg.data[2]),
            kd=float(msg.data[3]),
        )
        self._send_command(self.current_command)

    def _relax_callback(self, _msg: Empty) -> None:
        self.current_command = SerialCommand(position=0.0, kp=0.0, velocity=0.0, kd=0.0)
        self._send_command(self.current_command)

    def _send_periodic(self) -> None:
        if bool(self.get_parameter("continuous_send").value):
            self._send_command(self.current_command)

    def _send_command(self, command: SerialCommand) -> None:
        if self.connection is None:
            self._open_connection()
            if self.connection is None:
                return

        line = format_bridge_command(command)
        try:
            written = self.connection.write(line.encode("ascii"))
            if written != len(line):
                raise RuntimeError(f"incomplete serial write: {written}/{len(line)} bytes")
        except Exception as exc:
            self.get_logger().error(f"Turntable serial write failed: {exc}")
            self._close_connection()
            return

        self.tx_count += 1
        self.last_command_pub.publish(String(data=line.rstrip("\n")))
        self.get_logger().debug(f"TX {line.rstrip()}")

    def _read_once(self) -> None:
        if self.connection is None:
            return
        try:
            raw = self.connection.readline()
        except Exception as exc:
            self.get_logger().error(f"Turntable serial read failed: {exc}")
            self._close_connection()
            return
        if not raw:
            return

        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        self.rx_count += 1
        self.serial_line_pub.publish(String(data=line))
        self.get_logger().debug(f"RX {line}")


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = Turntable()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
