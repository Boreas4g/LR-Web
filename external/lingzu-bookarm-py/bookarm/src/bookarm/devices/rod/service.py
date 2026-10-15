"""Serial JSON implementation for the bookarm rod motor."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import sys
import time
from typing import Any, Callable, Optional

from bookarm.runtime.command_queue import DeviceCommandWorker


@dataclass(frozen=True)
class RodDeviceConfig:
    port: str = "/dev/rodmotor"
    baudrate: int = 921600
    timeout_s: float = 0.2
    poll_interval_s: float = 0.05


class RodMotorService:
    """Own the rod-motor serial port and confirm angle feedback after motion."""

    READ_ANGLE = 103
    WRITE_ANGLE = 104
    MIN_ANGLE_DEG = -100.0
    MAX_ANGLE_DEG = 150.0

    def __init__(
        self,
        config: RodDeviceConfig,
        serial_port: Optional[Any] = None,
        serial_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.config = config
        self._serial = serial_port
        self._owns_serial = serial_port is None
        # Reopen path after USB re-enumeration: the /dev symlink resolves to
        # the new node, so reconnecting is just a fresh open of the same path.
        self._serial_factory = serial_factory
        self._worker = DeviceCommandWorker("rod")
        # Pause after closing a dead handle so the re-enumerated node settles.
        self.reconnect_settle_s = 0.5
        self._reconnects = 0

    @property
    def reconnects(self) -> int:
        """How often the serial port was reopened after a communication failure."""

        return self._reconnects

    def start(self) -> None:
        self._worker.start()
        self._worker.call(self._connect)

    def stop(self) -> None:
        if not self._worker.started:
            return
        try:
            self._worker.call(self._disconnect, timeout_s=2.0)
        finally:
            self._worker.stop()

    def move_to_deg(
        self,
        target_deg: float,
        speed: int,
        acceleration: int,
        tolerance_deg: float,
        timeout_s: float,
    ) -> None:
        target_deg = float(target_deg)
        if not math.isfinite(target_deg) or not self.MIN_ANGLE_DEG <= target_deg <= self.MAX_ANGLE_DEG:
            raise ValueError(
                "rod target angle must be between %.1f and %.1f deg; got %r"
                % (self.MIN_ANGLE_DEG, self.MAX_ANGLE_DEG, target_deg)
            )
        self._worker.call(
            lambda: self._move_to_deg(target_deg, speed, acceleration, tolerance_deg, timeout_s),
            timeout_s=max(0.1, float(timeout_s)) + 1.0,
        )

    def read_angle_deg(self) -> float:
        """Read the current rod angle without commanding any motion."""

        return float(self._worker.call(self._read_angle, timeout_s=3.0))

    def _connect(self) -> None:
        if self._serial is not None:
            return
        if self._serial_factory is not None:
            self._serial = self._serial_factory()
            time.sleep(0.05)
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            return
        try:
            import serial
        except ImportError as error:
            raise RuntimeError("rod motor requires pyserial") from error
        self._serial = serial.Serial(
            port=self.config.port,
            baudrate=int(self.config.baudrate),
            timeout=float(self.config.timeout_s),
            write_timeout=float(self.config.timeout_s),
        )
        time.sleep(0.05)
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    def _reconnect(self) -> None:
        """Drop the dead handle and reopen the port (symlink → new node)."""

        print(
            "[ROD] serial communication failed; reconnecting %s" % self.config.port,
            file=sys.stderr,
        )
        try:
            self._disconnect()
        finally:
            self._serial = None
        time.sleep(self.reconnect_settle_s)
        self._connect()
        self._reconnects += 1

    def _disconnect(self) -> None:
        if self._serial is None:
            return
        try:
            if self._owns_serial and bool(getattr(self._serial, "is_open", False)):
                self._serial.close()
        finally:
            if self._owns_serial:
                self._serial = None

    def _move_to_deg(
        self, target_deg: float, speed: int, acceleration: int, tolerance_deg: float, timeout_s: float
    ) -> None:
        self._request(
            self.WRITE_ANGLE,
            {"rad": math.radians(float(target_deg)), "spd": int(speed), "acc": int(acceleration)},
            response=False,
        )
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        while True:
            measured = self._read_angle()
            if abs(measured - float(target_deg)) <= float(tolerance_deg):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "rod failed to reach %.3f deg; last measured %.3f deg" % (target_deg, measured)
                )
            time.sleep(max(0.01, float(self.config.poll_interval_s)))

    def _read_angle(self) -> float:
        response = self._request(self.READ_ANGLE, {}, response=True)
        for key in ("rad", "angle", "deg", "position", "pos", "value"):
            if key in response:
                return math.degrees(float(response[key]))
        raise RuntimeError("rod read response does not contain an angle")

    def _request(self, command: int, payload: dict[str, Any], *, response: bool) -> dict[str, Any]:
        try:
            return self._request_once(command, payload, response=response)
        except OSError as error:
            # USB re-enumeration kills the held fd (write EIO, or read
            # returning readiness with no data); reopen once and retry.
            self._reconnect()
            try:
                return self._request_once(command, payload, response=response)
            except OSError as retry_error:
                raise OSError(
                    "rod serial auto-reconnect failed once already; original error: %s" % error
                ) from retry_error

    def _request_once(
        self, command: int, payload: dict[str, Any], *, response: bool
    ) -> dict[str, Any]:
        if self._serial is None:
            raise RuntimeError("rod motor is not connected")
        message = {"T": int(command)}
        message.update(payload)
        self._serial.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
        self._serial.flush()
        if not response:
            return {}
        deadline = time.monotonic() + float(self.config.timeout_s)
        while time.monotonic() < deadline:
            line = self._serial.readline()
            if not line:
                continue
            try:
                result = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(result, dict):
                continue
            if not bool(result.get("ok", result.get("success", True))):
                raise RuntimeError(str(result.get("error") or result.get("err") or "rod command failed"))
            return result
        raise TimeoutError("rod command %d timed out" % int(command))
