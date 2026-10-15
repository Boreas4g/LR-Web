"""SocketCAN and SLCAN transport implementations owned by bookarm."""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Dict, Optional

from bookarm.devices.arm.protocol import (
    ArmTransport,
    CommType,
    DEFAULT_MOTOR_TYPES,
    MOTOR_LIMITS,
    MotorFeedback,
    MotorType,
    ParamIndex,
    RunMode,
)


CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_FRAME_FORMAT = "=IB3x8s"
CAN_FRAME_SIZE = 16


def _map_to_u16(value: float, lower: float, upper: float) -> int:
    bounded = min(max(float(value), lower), upper)
    return int(round((bounded - lower) * 65535.0 / (upper - lower)))


def _map_from_u16(value: int, lower: float, upper: float) -> float:
    return float(value) * (upper - lower) / 65535.0 + lower


class _RobstrideTransportBase:
    """Shared frame codec and feedback store for the two direct CAN backends."""

    def __init__(self, host_can_id: int = 0xFD, motor_types: Optional[Dict[int, MotorType]] = None) -> None:
        self.host_can_id = int(host_can_id)
        self.motor_types = dict(motor_types or DEFAULT_MOTOR_TYPES)
        self._feedback: Dict[int, MotorFeedback] = {}
        self._lock = threading.RLock()
        self._running = threading.Event()
        self._receiver: Optional[threading.Thread] = None
        self._frame_count = 0
        self._fps = 0.0
        self._fps_started_s = time.monotonic()

    @staticmethod
    def _can_id(comm_type: CommType, data_area: int, motor_id: int) -> int:
        return ((int(comm_type) & 0x1F) << 24) | ((int(data_area) & 0xFFFF) << 8) | (int(motor_id) & 0xFF)

    def _encode_command(self, comm_type: CommType, motor_id: int, data: bytes = b"") -> tuple[int, bytes]:
        return self._can_id(comm_type, self.host_can_id, motor_id), data.ljust(8, b"\x00")[:8]

    def _parse(self, can_id: int, data: bytes) -> None:
        if ((can_id >> 24) & 0x1F) != int(CommType.FEEDBACK):
            return
        motor_id = (can_id >> 8) & 0xFF
        if motor_id not in self.motor_types or len(data) < 8:
            return
        position_raw, velocity_raw, torque_raw, temperature_raw = struct.unpack(">HHHH", data[:8])
        limits = MOTOR_LIMITS[self.motor_types[motor_id]]
        feedback = MotorFeedback(
            motor_id=motor_id,
            position_rad=_map_from_u16(position_raw, limits.position_min_rad, limits.position_max_rad),
            velocity_rad_s=_map_from_u16(velocity_raw, limits.velocity_min_rad_s, limits.velocity_max_rad_s),
            torque_nm=_map_from_u16(torque_raw, limits.torque_min_nm, limits.torque_max_nm),
            temperature_c=float(temperature_raw) / 10.0,
            fault_code=(can_id >> 16) & 0x3F,
            timestamp_s=time.time(),
            mode_state=(can_id >> 22) & 0x03,
        )
        with self._lock:
            self._feedback[motor_id] = feedback
            self._frame_count += 1
            elapsed = time.monotonic() - self._fps_started_s
            if elapsed >= 1.0:
                self._fps = self._frame_count / elapsed
                self._frame_count = 0
                self._fps_started_s = time.monotonic()

    def enable_motor(self, motor_id: int) -> bool:
        return self._send(*self._encode_command(CommType.ENABLE, motor_id))

    def set_zero_position(self, motor_id: int) -> bool:
        # Type 6：把电机当前位置写为零位。只改变编码器零点，不引起运动。
        return self._send(*self._encode_command(CommType.SET_ZERO, motor_id, b"\x01"))

    def disable_motor(self, motor_id: int, clear_fault: bool = False) -> bool:
        # Type 4 失能帧 Byte[0]=1 时同时清除电机的锁存故障（如夹取过流）；
        # 电机无故障时该位无副作用。
        data = b"\x01" if clear_fault else b""
        return self._send(*self._encode_command(CommType.DISABLE, motor_id, data))

    def enable_feedback(self, motor_id: int) -> bool:
        return self._send(*self._encode_command(CommType.SET_AUTO_REPORT, motor_id, b"\x01"))

    def set_run_mode(self, motor_id: int, mode: RunMode) -> bool:
        data = struct.pack("<HBxI", int(ParamIndex.RUN_MODE), 0, int(mode))
        return self._send(*self._encode_command(CommType.WRITE_PARAM, motor_id, data))

    def _write_float_parameter(self, motor_id: int, parameter: ParamIndex, value: float) -> bool:
        data = struct.pack("<HBx", int(parameter), 0) + struct.pack("<f", float(value))
        return self._send(*self._encode_command(CommType.WRITE_PARAM, motor_id, data))

    def set_can_timeout(self, motor_id: int, timeout_s: float) -> bool:
        return self._write_float_parameter(motor_id, ParamIndex.CAN_TIMEOUT, timeout_s)

    def set_pp_velocity(self, motor_id: int, velocity_rad_s: float) -> bool:
        return self._write_float_parameter(motor_id, ParamIndex.VEL_MAX, velocity_rad_s)

    def set_pp_acceleration(self, motor_id: int, acceleration_rad_s2: float) -> bool:
        return self._write_float_parameter(motor_id, ParamIndex.ACC_SET, acceleration_rad_s2)

    def send_motion(
        self, motor_id: int, position_rad: float, velocity_rad_s: float,
        kp: float, kd: float, torque_nm: float
    ) -> bool:
        limits = MOTOR_LIMITS[self.motor_types[motor_id]]
        torque_raw = _map_to_u16(torque_nm, limits.torque_min_nm, limits.torque_max_nm)
        can_id = self._can_id(CommType.MOTION_CONTROL, torque_raw, motor_id)
        data = struct.pack(
            ">HHHH",
            _map_to_u16(position_rad, limits.position_min_rad, limits.position_max_rad),
            _map_to_u16(velocity_rad_s, limits.velocity_min_rad_s, limits.velocity_max_rad_s),
            _map_to_u16(kp, limits.kp_min, limits.kp_max),
            _map_to_u16(kd, limits.kd_min, limits.kd_max),
        )
        return self._send(can_id, data)

    def feedback(self) -> Dict[int, MotorFeedback]:
        with self._lock:
            return dict(self._feedback)

    def can_fps(self) -> float:
        with self._lock:
            if time.monotonic() - self._fps_started_s > 2.0:
                return 0.0
            return self._fps

    def emergency_stop(self) -> bool:
        success = True
        for motor_id in sorted(self.motor_types):
            success = self.disable_motor(motor_id) and success
        return success

    def _start_receiver(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self._receiver = threading.Thread(target=self._receive_loop, name="bookarm-arm-rx", daemon=True)
        self._receiver.start()

    def _stop_receiver(self) -> None:
        self._running.clear()
        if self._receiver is not None:
            self._receiver.join(timeout=1.0)
        self._receiver = None

    def _send(self, can_id: int, data: bytes) -> bool:
        raise NotImplementedError

    def _receive_loop(self) -> None:
        raise NotImplementedError


class SocketCanTransport(_RobstrideTransportBase):
    """Linux SocketCAN backend for the arm's 29-bit Robstride frames."""

    def __init__(self, interface: str = "can0", **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.interface = str(interface)
        self._socket: Optional[socket.socket] = None
        self._send_lock = threading.Lock()

    def connect(self) -> None:
        if self._socket is not None:
            return
        channel = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        channel.bind((self.interface,))
        channel.settimeout(0.1)
        self._socket = channel
        self._start_receiver()

    def close(self) -> None:
        self._stop_receiver()
        if self._socket is not None:
            self._socket.close()
        self._socket = None

    def _send(self, can_id: int, data: bytes) -> bool:
        if self._socket is None:
            raise RuntimeError("SocketCAN transport is not connected")
        frame = struct.pack(CAN_FRAME_FORMAT, int(can_id) | CAN_EFF_FLAG, 8, data.ljust(8, b"\x00"))
        with self._send_lock:
            return self._socket.send(frame) == len(frame)

    def _receive_loop(self) -> None:
        while self._running.is_set():
            try:
                if self._socket is None:
                    return
                raw = self._socket.recv(CAN_FRAME_SIZE)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                if self._running.is_set():
                    continue
                return
            if len(raw) != CAN_FRAME_SIZE:
                continue
            identifier, _dlc = struct.unpack("=IB", raw[:5])
            if not identifier & CAN_EFF_FLAG:
                continue
            self._parse(identifier & CAN_EFF_MASK, raw[8:16])


class SlcanTransport(_RobstrideTransportBase):
    """USB-CAN/SLCAN backend without dependency on the old SLCAN driver."""

    BITRATES = {10000: b"S0", 20000: b"S1", 50000: b"S2", 100000: b"S3", 125000: b"S4",
                250000: b"S5", 500000: b"S6", 800000: b"S7", 1000000: b"S8"}

    def __init__(self, port: str, serial_baudrate: int = 2000000, can_bitrate: int = 1000000, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.port = str(port)
        self.serial_baudrate = int(serial_baudrate)
        self.can_bitrate = int(can_bitrate)
        self._serial: Optional[object] = None
        self._send_lock = threading.Lock()

    def connect(self) -> None:
        if self._serial is not None:
            return
        try:
            import serial
        except ImportError as error:
            raise RuntimeError("SLCAN arm transport requires pyserial") from error
        if self.can_bitrate not in self.BITRATES:
            raise ValueError("unsupported SLCAN bitrate: %d" % self.can_bitrate)
        channel = serial.Serial(self.port, self.serial_baudrate, timeout=0.1, write_timeout=0.2)
        channel.write(b"C\r")
        channel.write(self.BITRATES[self.can_bitrate] + b"\r")
        channel.write(b"O\r")
        channel.flush()
        self._serial = channel
        self._start_receiver()

    def close(self) -> None:
        self._stop_receiver()
        if self._serial is not None:
            try:
                self._serial.write(b"C\r")
                self._serial.close()
            except Exception:
                pass
        self._serial = None

    def _send(self, can_id: int, data: bytes) -> bool:
        if self._serial is None:
            raise RuntimeError("SLCAN transport is not connected")
        message = ("T%08X8%s\r" % (int(can_id) & CAN_EFF_MASK, data.ljust(8, b"\x00").hex().upper())).encode("ascii")
        with self._send_lock:
            written = self._serial.write(message)
            self._serial.flush()
        return int(written) == len(message)

    def _receive_loop(self) -> None:
        while self._running.is_set():
            try:
                if self._serial is None:
                    return
                raw = self._serial.readline().decode("ascii", errors="ignore").strip()
            except Exception:
                if self._running.is_set():
                    continue
                return
            if len(raw) < 26 or raw[0] != "T" or raw[9] != "8":
                continue
            try:
                identifier = int(raw[1:9], 16)
                data = bytes.fromhex(raw[10:26])
            except ValueError:
                continue
            self._parse(identifier, data)
