"""Direct adapter for the standalone YZ-AIM lift SDK."""

from __future__ import annotations

from dataclasses import dataclass
import math
import sys
import threading
import time
from typing import Any, Callable, Optional, TypeVar

from bookarm.domain.models.lift import LiftPositionResult
from bookarm.runtime.command_queue import DeviceCommandWorker


_MOVE_HEIGHT_TIMEOUT_S = 45.0
_MOVE_HEIGHT_TOLERANCE_PULSES = 10
# Position polling cadence while waiting for arrival; matches the AIM SDK's
# own wait loop so an interrupt is felt within one poll cycle.
_MOVE_HEIGHT_POLL_INTERVAL_S = 0.02

_T = TypeVar("_T")


@dataclass(frozen=True)
class LiftDeviceConfig:
    port: str = "/dev/lift_port"
    address: int = 1
    baudrate: int = 19200
    timeout_s: float = 0.2
    retries: int = 1
    # Pulse count that reads as height 0; None falls back to the SDK default.
    reference_absolute_position: Optional[int] = None


class LiftPositioningError(RuntimeError):
    """A lift motion could not be safely completed or verified."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__("[%s] %s" % (self.code, message))


class AimLiftService:
    """Expose millimetre workflow coordinates through ``aim_sdk.LiftPlatform``."""

    def __init__(
        self,
        config: LiftDeviceConfig,
        platform: Optional[Any] = None,
        drive: Optional[Any] = None,
        drive_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.config = config
        self._platform = platform
        self._drive: Optional[Any] = drive or getattr(platform, "drive", None)
        self._enabled_modbus = False
        self._enabled_output = False
        # Test seam: produces (drive, platform) pairs instead of AimDrive.connect.
        self._drive_factory = drive_factory
        self._worker = DeviceCommandWorker("lift")
        # Set by emergency_stop(); the in-flight move aborts at the next poll.
        self._interrupt = threading.Event()
        # Pause after dropping a dead handle so the re-enumerated node settles.
        self.reconnect_settle_s = 0.5
        self._reconnects = 0

    @property
    def reconnects(self) -> int:
        """How often the lift drive was reopened after a communication failure."""

        return self._reconnects

    @property
    def connected(self) -> bool:
        """Whether the lift drive and platform handle were established."""

        return self._platform is not None and self._drive is not None

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

    def move_to_height_mm(
        self,
        target_height_mm: float,
        *,
        speed_level: int,
    ) -> LiftPositionResult:
        """Run the same calibrated motion as ``lift_move.py move-height``.

        Only calibrated height and the SDK's speed mode are operation inputs.
        The fixed 45-second wait and 10-pulse tolerance match the commissioning
        script's ``move-height`` defaults.
        """
        # A newly commanded move supersedes a previous safety halt; a halt
        # during this move still aborts it at the next poll cycle.
        self._interrupt.clear()
        # AIM owns the motion timeout.  Do not abandon its worker command
        # earlier, otherwise a timed-out caller could queue another command
        # while the original physical move remains active.
        return self._worker.call(
            lambda: self._with_reconnect(
                lambda: self._move_to_height_mm(target_height_mm, speed_level)
            )
        )

    def emergency_stop(self) -> bool:
        """Halt any in-flight lift motion immediately (software emergency stop).

        Safety path behind the HTTP safety endpoints: never queued behind a
        running move on the lift worker. The AIM emergency-stop sequence is
        sent directly — the serial transport serializes each Modbus
        transaction — and the in-flight move aborts at its next poll instead
        of retrying or reconnecting. Returns False when no drive is connected
        or the stop frames could not be sent.
        """

        self._interrupt.set()
        drive = self._drive
        if drive is None:
            return False
        try:
            drive.emergency_stop()
        except Exception:
            return False
        return True

    def read_lift_height_mm(self) -> float:
        """Read the current lift height in millimetres through AIM calibration.

        Power-on default: when the controller does not answer (lift unpowered,
        USB still enumerating, MODBUS not yet up), report 0 mm instead of
        failing the studio polling endpoint with a 500.
        """
        try:
            return float(self._worker.call(lambda: self._with_reconnect(self._read_lift_height_mm)))
        except Exception as error:
            print(
                "[LIFT] height read failed (%s); reporting power-on default 0 mm" % error,
                file=sys.stderr,
            )
            return 0.0

    def _with_reconnect(self, operation: Callable[[], _T]) -> _T:
        """Retry one operation after reopening a dead serial/MODBUS handle.

        USB re-enumeration kills the held fd (write EIO, or reads reporting
        readiness with no data). The /dev symlink already points at the new
        node, so reconnecting is just a fresh ``AimDrive.connect``.
        """

        try:
            return operation()
        except Exception as error:
            if self._interrupt.is_set():
                raise LiftPositioningError(
                    "interrupted", "lift motion interrupted by safety stop"
                ) from error
            if not self._is_comm_error(error):
                raise
        self._reconnect()
        try:
            return operation()
        except Exception as retry_error:
            if self._interrupt.is_set():
                raise LiftPositioningError(
                    "interrupted", "lift motion interrupted by safety stop"
                ) from retry_error
            raise LiftPositioningError(
                "aim_reconnect_failed",
                "AIM lift auto-reconnect failed once already; original error: %s" % retry_error,
            ) from retry_error

    @staticmethod
    def _is_comm_error(error: Exception) -> bool:
        if isinstance(error, OSError):
            return True
        return (
            isinstance(error, LiftPositioningError)
            and error.code == "aim_communication_failed"
        )

    def _reconnect(self) -> None:
        """Drop the dead drive and reconnect through the configured port path."""

        print(
            "[LIFT] serial communication failed; reconnecting %s" % self.config.port,
            file=sys.stderr,
        )
        drive = self._drive
        self._drive = None
        self._platform = None
        self._enabled_modbus = False
        self._enabled_output = False
        if drive is not None:
            try:
                drive.close()
            except Exception:
                pass
        time.sleep(self.reconnect_settle_s)
        self._connect()
        self._reconnects += 1

    def _connect(self) -> None:
        if self._platform is not None and self._drive is not None:
            return
        if self._platform is not None:
            raise RuntimeError("injected AIM lift platform must expose its drive")
        if self._drive_factory is not None:
            self._drive, self._platform = self._drive_factory()
            return
        try:
            from aim_sdk import AimDrive, LiftPlatform
        except ImportError as error:
            raise RuntimeError("bookarm requires the configured aim_sdk installation") from error
        self._drive = AimDrive.connect(
            self.config.port,
            address=int(self.config.address),
            baudrate=int(self.config.baudrate),
            timeout_s=float(self.config.timeout_s),
            retries=int(self.config.retries),
        )
        self._platform = LiftPlatform(
            self._drive,
            reference_absolute_position=self.config.reference_absolute_position,
        )

    def _ensure_motion_ready(self) -> None:
        if self._drive is None:
            raise RuntimeError("AIM lift drive is not connected")
        if not self._drive.is_modbus_enabled():
            self._drive.enable_modbus()
            self._enabled_modbus = True
        if not self._drive.is_output_enabled():
            self._drive.enable_output()
            self._enabled_output = True
        self._drive.assert_motion_ready()

    def _move_to_height_mm(self, target_height_mm: float, speed_level: int) -> LiftPositionResult:
        # A halt that landed after the public entry clear (e.g. during a
        # reconnect settle) must still win before any motion is issued here.
        if self._interrupt.is_set():
            raise LiftPositioningError(
                "interrupted", "lift motion interrupted by safety stop"
            )
        if self._platform is None:
            raise RuntimeError("AIM lift platform is not connected")
        target = self._finite(target_height_mm, "target height")
        speed = self._speed_level(speed_level)
        target_cm = target / 10.0
        try:
            # Validate before enabling output, just as an invalid CLI argument
            # is rejected before ``lift_move.py`` opens a motion session.
            self._platform.height_to_absolute_position(target_cm)
            self._platform.speed_profile(speed)
            self._ensure_motion_ready()
            target_pulses = int(self._platform.move_to_height(
                target_cm,
                speed=speed,
                # The arrival wait runs in this service so a safety halt is
                # felt within one poll cycle instead of only after the full
                # SDK-side timeout.
                wait=False,
                timeout_s=_MOVE_HEIGHT_TIMEOUT_S,
                tolerance_pulses=_MOVE_HEIGHT_TOLERANCE_PULSES,
                check_ready=True,
            ))
            measured_pulses = self._wait_until_at_position(target_pulses)
            measured = float(self._platform.read_height_cm()) * 10.0
        except LiftPositioningError:
            raise
        except TimeoutError as error:
            raise LiftPositioningError(
                "timeout", "AIM lift did not reach the requested position within %.0f seconds" % _MOVE_HEIGHT_TIMEOUT_S
            ) from error
        except ValueError as error:
            raise LiftPositioningError("invalid_target", str(error)) from error
        except RuntimeError as error:
            raise LiftPositioningError("aim_motion_failed", "AIM lift motion failed: %s" % error) from error
        except Exception as error:
            raise LiftPositioningError("aim_communication_failed", "AIM lift communication failed: %s" % error) from error
        return LiftPositionResult(
            target_height_mm=target,
            measured_height_mm=measured,
            tolerance_pulses=_MOVE_HEIGHT_TOLERANCE_PULSES,
            controller_target_pulses=target_pulses,
            controller_measured_pulses=measured_pulses,
        )

    def _wait_until_at_position(self, target_pulses: int) -> int:
        """Poll absolute feedback until arrival, aborting on a safety halt.

        Runs on the lift worker, so the repeated emergency stop below never
        races another Modbus transaction on the half-duplex bus.
        """

        drive = self._drive_required()
        deadline = time.monotonic() + _MOVE_HEIGHT_TIMEOUT_S
        while True:
            if self._interrupt.is_set():
                # The safety caller already sent this stop directly; repeat it
                # from the owning worker so the halt is certain even if that
                # direct send raced a reconnect.
                try:
                    drive.emergency_stop()
                except Exception:
                    pass
                raise LiftPositioningError(
                    "interrupted", "lift motion interrupted by safety stop"
                )
            actual = int(drive.read_absolute_position())
            if abs(actual - target_pulses) <= _MOVE_HEIGHT_TOLERANCE_PULSES:
                return actual
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "position did not reach %s; latest feedback was %s" % (target_pulses, actual)
                )
            self._interrupt.wait(_MOVE_HEIGHT_POLL_INTERVAL_S)

    def _read_lift_height_mm(self) -> float:
        if self._platform is None:
            raise RuntimeError("AIM lift platform is not connected")
        return float(self._platform.read_height_cm()) * 10.0

    @staticmethod
    def _finite(value: float, name: str) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as error:
            raise LiftPositioningError("invalid_input", "%s must be a finite number" % name) from error
        if not math.isfinite(numeric):
            raise LiftPositioningError("invalid_input", "%s must be a finite number" % name)
        return numeric

    @staticmethod
    def _speed_level(value: int) -> int:
        if type(value) is not int or value not in (1, 2, 3):
            raise LiftPositioningError("invalid_speed", "lift speed must be the integer 1, 2, or 3")
        return value

    def _drive_required(self) -> Any:
        if self._drive is None:
            raise RuntimeError("AIM lift drive is not connected")
        return self._drive

    def _disconnect(self) -> None:
        try:
            if self._drive is not None:
                if self._enabled_output:
                    self._drive.disable_output()
                if self._enabled_modbus:
                    self._drive.disable_modbus()
        finally:
            self._enabled_output = False
            self._enabled_modbus = False
            if self._drive is not None:
                self._drive.close()
            self._drive = None
            self._platform = None
