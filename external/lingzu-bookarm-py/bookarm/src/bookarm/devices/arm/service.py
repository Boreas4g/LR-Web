"""Single-owner direct arm service using bookarm's CAN/SLCAN transport stack."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import math
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence

from bookarm.devices.arm.feedback import gripper_angle_from_feedback, joint_pose_from_feedback
from bookarm.devices.arm.kinematics import PinocchioKinematics
from bookarm.devices.arm.motion import joint_path_derivatives, joint_trajectory, linear_pose
from bookarm.devices.arm.protocol import (
    ArmTransport,
    DEFAULT_DIRECTIONS,
    DEFAULT_OFFSETS,
    DEFAULT_JOINT_LIMITS,
    MotorFeedback,
    MotorModeState,
    RunMode,
)
from bookarm.devices.arm.safety import ensure_joint_arrival, validate_joint_target
from bookarm.devices.arm.transport import SlcanTransport, SocketCanTransport
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.domain.plans.take_book import GripperCommand
from bookarm.runtime.command_queue import DeviceCommandWorker


_PACKAGED_URDF = Path(__file__).resolve().parents[2] / "resources" / "urdf" / "el_a3.urdf"
DEFAULT_TCP_OFFSET = (0.0, 0.01, -0.14, 0.0, 0.0, 0.0)
ZERO_JOINT = JointPose.from_values((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
# Built-in book-debug pose in degrees: [0, 10, -65, 50, 0, 0].
BOOK_DEBUG_JOINT = JointPose.from_values(tuple(math.radians(value) for value in (0.0, 10.0, -65.0, 50.0, 0.0, 0.0)))
# Safe disable pose: near all-zero joint angles. On the reassembled arm with
# the current zero calibration this is the physically folded/balanced storage
# pose — cutting motor power here does not drop the arm. (An earlier
# model-suggested [0, 90, -180, 90, 0, 0]° pose turned out to be vertical-up
# on the real arm and was dangerous to disable at; do not "optimize" this
# back to a computed pose without on-hardware confirmation.)
DISABLE_JOINT = JointPose.from_values((-0.033182, 0.004412, -0.005946, 0.367308, 0.015920, 0.000192))

# Carry-hold frames for joint 7 are re-sent at this rate, decimated from the
# 200 Hz joint 1--6 loop so gripper holding never starves arm control.
_GRIPPER_CARRY_RATE_HZ = 25.0

# Messages marking a transient CAN feedback stall (frozen hold target). Only
# this failure class is eligible for motion resend; thermal lockout, control
# loop death and interrupt errors deliberately do not match.
_FEEDBACK_STALL_MARKERS = (
    "feedback keepalive",
    "feedback refresh failed before motion command",
)


def _is_feedback_stall(error: BaseException) -> bool:
    message = str(error)
    return any(marker in message for marker in _FEEDBACK_STALL_MARKERS)


@dataclass(frozen=True)
class ArmDeviceConfig:
    backend: str = "socketcan"
    can_interface: str = "can0"
    slcan_port: Optional[str] = None
    slcan_baudrate: int = 2000000
    can_bitrate: int = 1000000
    host_can_id: int = 0xFD
    control_rate_hz: float = 200.0
    feedback_max_age_s: float = 0.35
    # Type-24 feedback requests are independent of the Type-1 hold loop. This
    # keeps a fresh IK seed available while the arm is stationary.
    feedback_keepalive_interval_s: float = 0.10
    enable_feedback_timeout_s: float = 3.0
    final_tolerance_rad: float = 0.08
    default_kp: float = 80.0
    default_kd: float = 4.0
    joint_directions: Dict[int, float] = field(default_factory=lambda: dict(DEFAULT_DIRECTIONS))
    joint_offsets: Dict[int, float] = field(default_factory=lambda: dict(DEFAULT_OFFSETS))
    joint_limits: Dict[int, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_JOINT_LIMITS))
    max_velocity_rad_s: float = 3.0
    max_acceleration_rad_s2: float = 15.0
    max_jerk_rad_s3: float = 50.0
    velocity_limit_rad_s: float = 10.0
    gravity_feedforward_ratio: float = 1.0
    gravity_joint_scale: Dict[int, float] = field(
        default_factory=lambda: {motor_id: 1.0 for motor_id in range(1, 7)}
    )
    limit_stop_margin_rad: float = 0.02
    # Type-1 motors must not reset while another joint is moving. The arm
    # service requires this to remain zero whenever it enables the arm.
    can_timeout_s: float = 0.0
    gripper_pp_velocity_rad_s: float = 6.0
    gripper_pp_acceleration_rad_s2: float = 15.0
    urdf_path: Path = _PACKAGED_URDF
    # TCP uses the URDF/end-effector frame: [x, y, z, rx, ry, rz] in m/rad.
    tcp_offset: tuple[float, float, float, float, float, float] = DEFAULT_TCP_OFFSET
    # Field-calibrated link inertia overrides for gravity feedforward.
    inertia_config_path: Optional[Path] = None
    # Thermal watchdog: trips only after a body joint (1-6) stays at or above
    # this Celsius reading for `thermal_trip_sustained_s` continuously, so
    # brief torque/heat spikes no longer latch a lockout. Interval of 0 disables.
    thermal_limit_c: float = 90.0
    thermal_trip_sustained_s: float = 60.0
    thermal_check_interval_s: float = 10.0
    # Once latched, the lockout releases itself automatically when every body
    # joint cools back below this Celsius reading.
    thermal_release_c: float = 50.0
    # On a thermal trip the arm first parks at the collapse pose (this is the
    # MoveJ duration, i.e. the time budget for parking) before disabling;
    # if parking fails for any reason it is emergency-stopped in place instead.
    thermal_safe_pose_duration_s: float = 8.0
    # Gripper (joint 7) thermal policy: the motor firmware cuts torque on its
    # own around 75 C, so software derates the carry hold first, then stops it,
    # and never locks out joints 1--6 for gripper temperature.
    gripper_derate_onset_c: float = 60.0
    gripper_derate_release_c: float = 55.0
    gripper_derate_min_effort: float = 0.1
    gripper_thermal_limit_c: float = 70.0
    # Joint telemetry CSV logging (torque/temperature per joint). Interval of
    # 0 or a None directory disables logging.
    telemetry_interval_s: float = 1.0
    telemetry_dir: Optional[Path] = None
    # Retention for telemetry_*.csv files: when a new log file starts, files
    # older than this many days are deleted. 0 keeps every file forever.
    telemetry_retention_days: float = 7.0


class ArmService:
    """The only bookarm component allowed to own the arm CAN transport.

    All commands, including the checked motor-disable sequence, run through
    one owner worker.  The raw transport disable remains private to this class.
    """

    def __init__(self, config: ArmDeviceConfig, transport: Optional[ArmTransport] = None) -> None:
        self.config = config
        self._transport = transport
        self._worker = DeviceCommandWorker("arm")
        self._interrupt = threading.Event()
        self._enabled = False
        self._kinematics: Optional[PinocchioKinematics] = None
        self._kinematics_lock = threading.RLock()
        # Motion methods update these setpoints; a dedicated loop is the sole
        # producer of Type-1 frames for arm joints 1--6.
        self._control_lock = threading.RLock()
        self._control_target = ZERO_JOINT
        self._control_velocities = (0.0,) * 6
        # Two-stage grip state: when set, the control loop re-sends a low-torque
        # carry frame for joint 7 at a decimated rate (see _control_loop_main).
        self._gripper_carry: Optional[Dict[str, float]] = None
        # Joint-7 (gripper) state is tracked independently from the arm body:
        # an overheated gripper drops out of Motor mode while joints 1--6 keep
        # working, and that must not paralyse the whole arm.
        self._gripper_enabled = False
        self._gripper_carry_derated = False
        self._gripper_thermal_warning: Optional[str] = None
        # Keep multi-frame Type-1 control ticks and Type-24 refresh batches
        # contiguous on the CAN bus.
        self._can_send_lock = threading.Lock()
        self._control_stop = threading.Event()
        self._control_running = threading.Event()
        self._control_thread: Optional[threading.Thread] = None
        self._control_error: Optional[str] = None
        self._arm_feedback_error: Optional[str] = None
        self._last_arm_feedback_request_s: Optional[float] = None
        self._last_arm_feedback_request_error: Optional[str] = None
        # Thermal watchdog state: latched outcome dict while locked out.
        self._thermal_stop = threading.Event()
        self._thermal_thread: Optional[threading.Thread] = None
        self._thermal_lockout: Optional[Dict[str, Any]] = None
        self._last_temperatures: Dict[int, float] = {}
        # Wall time since any body joint first read over the limit; the trip
        # fires only if it stays over for thermal_trip_sustained_s.
        self._over_limit_since: Optional[float] = None
        # Telemetry CSV logger state.
        self._telemetry_stop = threading.Event()
        self._telemetry_thread: Optional[threading.Thread] = None
        self._telemetry_file = None
        self._telemetry_path: Optional[Path] = None
        self._last_sample_at: Optional[float] = None

    def start(self) -> None:
        self._worker.start()
        self._worker.call(self._connect)
        self._start_thermal_monitor()
        self._start_telemetry_logger()

    def stop(self) -> None:
        """Close transport without changing the physical motor enable state."""

        self._stop_telemetry_logger()
        self._stop_thermal_monitor()
        self.close()

    def close(self) -> None:
        """Close the transport without changing the motors' enable state."""

        if not self._worker.started:
            return
        self._stop_telemetry_logger()
        self._stop_thermal_monitor()
        try:
            self._worker.call(self._close, timeout_s=2.0)
        finally:
            self._worker.stop()

    # ------------------------------------------------------------------
    # Thermal watchdog: overheat trips an emergency stop and latches a
    # lockout that only clears after the arm cools below the limit.
    # ------------------------------------------------------------------

    def _start_thermal_monitor(self) -> None:
        interval_s = float(self.config.thermal_check_interval_s)
        if interval_s <= 0.0 or self._thermal_thread is not None:
            return
        self._thermal_stop.clear()
        self._thermal_thread = threading.Thread(
            target=self._thermal_monitor_main,
            args=(interval_s,),
            name="bookarm-arm-thermal",
            daemon=True,
        )
        self._thermal_thread.start()

    def _stop_thermal_monitor(self) -> None:
        self._thermal_stop.set()
        thread = self._thermal_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thermal_thread = None

    def _thermal_monitor_main(self, interval_s: float) -> None:
        while not self._thermal_stop.wait(interval_s):
            try:
                # The safe-pose park on a trip may take several seconds; the
                # call timeout only releases the monitor thread, the worker
                # task itself always runs to completion.
                self._worker.call(self._check_temperatures, timeout_s=20.0)
            except Exception:
                # A busy worker (long motion) or a transport hiccup must not
                # kill the watchdog; the next interval retries.
                continue

    def _check_temperatures(self) -> None:
        transport = self._require_transport()
        with self._can_send_lock:
            for motor_id in range(1, 8):
                transport.enable_feedback(motor_id)
        self._interrupt.wait(0.15)
        feedback = transport.feedback()
        now = time.time()
        temperatures: Dict[int, float] = {}
        for motor_id in range(1, 8):
            item = feedback.get(motor_id)
            if item is not None and now - float(item.timestamp_s) <= 5.0:
                temperatures[motor_id] = float(item.temperature_c)
        self._last_temperatures = temperatures
        # Joint 7 (gripper) has its own policy: its firmware cuts torque by
        # itself near 75 C, so software only warns and pauses the carry hold,
        # letting the motor cool. It never locks out the arm body.
        gripper_temp = temperatures.get(7)
        if gripper_temp is not None:
            gripper_limit = float(self.config.gripper_thermal_limit_c)
            if gripper_temp >= gripper_limit:
                if self._gripper_thermal_warning is None:
                    print(
                        "[THERMAL] gripper joint 7 at %.1f C (limit %.1f C); carry hold paused, arm body unaffected"
                        % (gripper_temp, gripper_limit),
                        file=sys.stderr,
                    )
                self._gripper_thermal_warning = (
                    "gripper motor 7 at %.1f C (limit %.1f C); carry hold paused for cooling"
                    % (gripper_temp, gripper_limit)
                )
                self._clear_gripper_carry()
            elif gripper_temp < gripper_limit - 5.0:
                self._gripper_thermal_warning = None
        release_c = float(self.config.thermal_release_c)
        if self._thermal_lockout is not None:
            # Auto-release once every freshly reported body joint has cooled
            # below the release threshold. The arm stays disabled until the
            # operator re-enables it; only the lockout latch clears.
            body_temps = [value for motor_id, value in temperatures.items() if motor_id != 7]
            if body_temps and all(value < release_c for value in body_temps):
                with self._control_lock:
                    self._arm_feedback_error = None
                self._thermal_lockout = None
                self._over_limit_since = None
                print(
                    "[THERMAL] all joints cooled below %.1f C; lockout auto-released" % release_c,
                    file=sys.stderr,
                )
            return
        limit = float(self.config.thermal_limit_c)
        hottest_id: Optional[int] = None
        hottest = float("-inf")
        for motor_id, temperature in temperatures.items():
            if motor_id == 7:
                continue
            if temperature >= limit and temperature > hottest:
                hottest_id, hottest = motor_id, temperature
        if hottest_id is None:
            self._over_limit_since = None
            return
        if self._over_limit_since is None:
            self._over_limit_since = now
            return
        sustained_s = float(self.config.thermal_trip_sustained_s)
        if now - self._over_limit_since >= sustained_s:
            self._trip_thermal_lockout(hottest_id, hottest, limit)

    def _trip_thermal_lockout(self, motor_id: int, temperature: float, limit: float) -> None:
        """Park at the collapse pose before disabling; never skip the disable.

        Runs on the arm worker (via the watchdog's ``_check_temperatures``).
        Sequence: interrupt any in-flight motion, try the checked park-and-
        disable, and on ANY failure fall back to an immediate in-place
        emergency stop. The lockout always latches and records whether the arm
        was safely parked first.
        """

        reason = (
            "thermal lockout: joint %d reached %.1f C (limit %.1f C)"
            % (motor_id, temperature, limit)
        )
        print(
            "[THERMAL] %s; parking at the collapse pose before disabling" % reason,
            file=sys.stderr,
        )
        # Cancel any in-flight or queued motion before parking.
        self._interrupt.set()
        safe_posed = False
        detail: Optional[str] = None
        try:
            self._disable_at_safe_pose(float(self.config.thermal_safe_pose_duration_s))
            safe_posed = True
        except Exception as error:  # Safety fallback: catch everything.
            detail = str(error)
            print(
                "[THERMAL] safe-pose park failed (%s); emergency stop in place NOW"
                % error,
                file=sys.stderr,
            )
            try:
                if self._enabled:
                    self._stop_control_loop()
            except Exception:
                pass
            try:
                transport = self._transport
                if transport is not None:
                    transport.emergency_stop()
            except Exception:
                pass
            self._enabled = False
            self._clear_gripper_carry()
        outcome = (
            "disabled after parking at the collapse pose"
            if safe_posed
            else "park to collapse pose failed%s; emergency-stopped in place"
            % (" (%s)" % detail if detail else "")
        )
        self._thermal_lockout = {
            "message": "%s; %s" % (reason, outcome),
            "reason": reason,
            "motor_id": motor_id,
            "temperature_c": temperature,
            "limit_c": limit,
            "safe_posed": safe_posed,
            "detail": detail,
        }
        self._set_arm_feedback_error(str(self._thermal_lockout["message"]))

    def thermal_status(self) -> Dict[str, object]:
        """Current watchdog view: per-joint Celsius and any latched lockout."""

        return {
            "limit_c": float(self.config.thermal_limit_c),
            "release_c": float(self.config.thermal_release_c),
            "trip_sustained_s": float(self.config.thermal_trip_sustained_s),
            "over_limit_since": self._over_limit_since,
            "temperatures_c": dict(self._last_temperatures),
            "lockout": self._thermal_lockout,
        }

    def gripper_carry_status(self) -> Dict[str, Any]:
        """Carry-hold view for hardware status: armed/derated + thermal note."""

        with self._control_lock:
            return {
                "armed": self._gripper_carry is not None,
                "derated": self._gripper_carry_derated,
                "thermal_warning": self._gripper_thermal_warning,
            }

    def clear_thermal_lockout(self) -> Dict[str, object]:
        """Stop all in-flight arm activity, then clear a latched lockout.

        Operator intent on this button is "stop everything and clear", so the
        call first interrupts any running motion instead of failing busy
        behind a long in-flight action.
        """
        self._interrupt.set()
        status = self._worker.call(self._clear_thermal_lockout, timeout_s=30.0)
        return status

    def _clear_thermal_lockout(self) -> Dict[str, object]:
        self._check_temperatures()
        if self._thermal_lockout is None:
            return self.thermal_status()
        limit = float(self.config.thermal_limit_c)
        hottest = max(self._last_temperatures.values(), default=0.0)
        if hottest >= limit:
            raise RuntimeError(
                "thermal lockout still active: hottest joint %.1f C >= %.1f C" % (hottest, limit)
            )
        with self._control_lock:
            self._arm_feedback_error = None
        self._thermal_lockout = None
        return self.thermal_status()

    def _require_no_thermal_lockout(self) -> None:
        if self._thermal_lockout is not None:
            raise RuntimeError(str(self._thermal_lockout["message"]))

    # ------------------------------------------------------------------
    # Telemetry logging: 1 Hz (configurable) torque/temperature CSV capture
    # for field debugging. Sampling rides the arm worker like the watchdog.
    # ------------------------------------------------------------------

    def _start_telemetry_logger(self) -> None:
        interval_s = float(self.config.telemetry_interval_s)
        directory = self.config.telemetry_dir
        if interval_s <= 0.0 or directory is None or self._telemetry_thread is not None:
            return
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self._cleanup_telemetry_files(directory)
        self._telemetry_path = directory / (
            "telemetry_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        header = ["timestamp"] + [
            "j%d_%s" % (motor_id, field)
            for motor_id in range(1, 8)
            for field in ("torque_nm", "temperature_c", "fault_code")
        ]
        self._telemetry_file = self._telemetry_path.open("w", encoding="utf-8", buffering=1)
        self._telemetry_file.write(",".join(header) + "\n")
        print("[TELEMETRY] joint telemetry logging to %s" % self._telemetry_path, file=sys.stderr)
        self._telemetry_stop.clear()
        self._telemetry_thread = threading.Thread(
            target=self._telemetry_main,
            args=(interval_s,),
            name="bookarm-arm-telemetry",
            daemon=True,
        )
        self._telemetry_thread.start()

    def _cleanup_telemetry_files(self, directory: Path) -> None:
        """Delete telemetry CSVs older than the configured retention.

        Only ``telemetry_*.csv`` files are eligible; bench-test captures such
        as ``motor*_bench_*.csv`` share the directory and are always kept.
        Cleanup failures must never abort service startup.
        """

        retention_days = float(self.config.telemetry_retention_days)
        if retention_days <= 0.0:
            return
        cutoff = time.time() - retention_days * 86400.0
        for path in directory.glob("telemetry_*.csv"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _stop_telemetry_logger(self) -> None:
        self._telemetry_stop.set()
        thread = self._telemetry_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._telemetry_thread = None
        if self._telemetry_file is not None:
            try:
                self._telemetry_file.close()
            except Exception:
                pass
            self._telemetry_file = None

    def _telemetry_main(self, interval_s: float) -> None:
        while not self._telemetry_stop.wait(interval_s):
            try:
                self._worker.call(self._sample_telemetry, timeout_s=10.0)
            except Exception:
                continue

    def _sample_telemetry(self) -> None:
        transport = self._require_transport()
        with self._can_send_lock:
            for motor_id in range(1, 8):
                transport.enable_feedback(motor_id)
        self._interrupt.wait(0.12)
        feedback = transport.feedback()
        now = time.time()
        fields = []
        for motor_id in range(1, 8):
            item = feedback.get(motor_id)
            if item is not None and now - float(item.timestamp_s) <= 5.0:
                fields.extend(
                    (
                        "%.3f" % float(item.torque_nm),
                        "%.1f" % float(item.temperature_c),
                        str(int(item.fault_code)),
                    )
                )
                self._last_temperatures[motor_id] = float(item.temperature_c)
            else:
                fields.extend(("", "", ""))
        self._last_sample_at = now
        if self._telemetry_file is not None:
            row = datetime.fromtimestamp(now).isoformat(timespec="milliseconds") + "," + ",".join(fields)
            try:
                self._telemetry_file.write(row + "\n")
            except Exception:
                pass

    @property
    def telemetry_path(self) -> Optional[Path]:
        return self._telemetry_path

    def enable(self) -> None:
        self._require_no_thermal_lockout()
        self._worker.call(self._enable, timeout_s=self.config.enable_feedback_timeout_s + 1.0)

    def interrupt_motion(self) -> None:
        """Cancel an in-flight SDK motion without sending motor-disable frames."""

        self._interrupt.set()
        # The persistent loop would otherwise continue holding the most recent
        # trajectory waypoint after the synchronous motion exits. Hold current
        # feedback instead, while preserving the no-disable interrupt contract.
        try:
            hold_target = self._read_joint_pose()
        except RuntimeError:
            return
        with self._control_lock:
            self._control_target = hold_target
            self._control_velocities = (0.0,) * 6

    def emergency_stop(self) -> bool:
        """Disable every arm motor in place right now (software emergency stop).

        Safety path behind the HTTP safety endpoints: like the thermal-trip
        fallback it never queues behind an in-flight motion on the owning
        worker. Any running motion is interrupted first and the 200 Hz hold
        loop is stopped so no new Type-1 targets can follow the disable
        frames. The arm stays disabled until an explicit ``enable()``.
        Returns the transport's all-motors-disabled acknowledgement.
        """

        self._interrupt.set()
        self._stop_control_loop()
        transport = self._transport
        if transport is None:
            self._enabled = False
            self._gripper_enabled = False
            return False
        try:
            return bool(transport.emergency_stop())
        finally:
            self._enabled = False
            self._gripper_enabled = False
            self._clear_gripper_carry()

    def move_j(self, target: JointPose, duration_s: float) -> None:
        self._call_motion_with_stall_retry(
            lambda: self._move_j(target, duration_s), timeout_s=max(0.1, float(duration_s)) + 3.0
        )

    def _call_motion_with_stall_retry(self, action: Callable[[], Any], timeout_s: float) -> Any:
        """Run one worker-routed motion; resend the same target once after a
        transient CAN feedback stall.

        The stall freeze holds the last published target (no motion), so the
        retry simply re-runs the motion: its preamble re-establishes feedback
        health (bounded by ``enable_feedback_timeout_s``) and the trajectory
        is re-planned from the current pose. A persisting stall fails the
        retry the same way, so the motion fails at most ~one recovery window
        later than before. Thermal lockout / dead control loop / interrupts
        never match the stall markers and are never resent.
        """

        try:
            return self._worker.call(action, timeout_s=timeout_s)
        except RuntimeError as error:
            if not _is_feedback_stall(error):
                raise
        return self._worker.call(
            action, timeout_s=timeout_s + float(self.config.enable_feedback_timeout_s)
        )

    def move_to_zero(self, duration_s: float = 3.0) -> None:
        """Move the six arm joints to the built-in zero pose."""

        self.move_j(ZERO_JOINT, duration_s)

    def set_zero_position(self, motor_num: int = 0xFF) -> Dict[int, bool]:
        """Write the current encoder position as the zero of the given motors.

        Commissioning only: the arm must be disabled so the control loop
        cannot fight the redefined zero frame. ``0xFF`` means arm joints 1-6;
        an explicit 1..7 addresses one motor (7 is the gripper).
        """

        return self._worker.call(lambda: self._set_zero_position(motor_num), timeout_s=10.0)

    def _set_zero_position(self, motor_num: int) -> Dict[int, bool]:
        if self._enabled:
            raise RuntimeError("arm must be disabled before zero calibration")
        transport = self._require_transport()
        if int(motor_num) == 0xFF:
            motor_ids = list(range(1, 7))
        elif 1 <= int(motor_num) <= 7:
            motor_ids = [int(motor_num)]
        else:
            raise ValueError("motor_num must be 1..7 or 0xFF (all arm joints)")
        results: Dict[int, bool] = {}
        for motor_id in motor_ids:
            results[motor_id] = bool(transport.set_zero_position(motor_id))
            time.sleep(0.05)
        return results

    def clear_motor_faults(self) -> Dict[str, Any]:
        """Clear latched fault codes on all seven motors (operator recovery).

        Sends the firmware fault-clear frame (disable with clear_fault) to each
        motor; every motor ends disabled afterwards, so re-enable the arm from
        the debug page once the codes read zero.
        """

        return self._worker.call(self._clear_motor_faults, timeout_s=10.0)

    def _clear_motor_faults(self) -> Dict[str, Any]:
        transport = self._require_transport()
        results: Dict[str, bool] = {}
        for motor_id in range(1, 8):
            results[str(motor_id)] = bool(transport.disable_motor(motor_id, clear_fault=True))
            time.sleep(0.03)
        fault_codes: Dict[str, int | None] = {}
        for motor_id, item in transport.feedback().items():
            fault_codes[str(motor_id)] = int(item.fault_code) if item is not None else None
        return {"results": results, "fault_codes": fault_codes}

    def move_to_book_debug(self, duration_s: float = 3.0) -> None:
        """Move to the built-in take-book debug pose: [0, 10, -65, 50, 0, 0] deg."""

        self.move_j(BOOK_DEBUG_JOINT, duration_s)

    def move_to_disable_pose(self, duration_s: float = 3.0) -> None:
        """Move to the built-in stable pose intended before an operator disables the arm."""

        self.move_j(DISABLE_JOINT, duration_s)

    def read_joint_pose(self) -> JointPose:
        return self._worker.call(self._read_joint_pose, timeout_s=1.0)

    def tcp_pose(self, joints: JointPose) -> BasePose:
        """Return the TCP pose for a logical joint pose via Pinocchio FK."""
        # Pinocchio stores mutable kinematics data, so FK must serialize
        # against control-thread gravity/IK use (same guard as _gravity_torques).
        with self._kinematics_lock:
            return self._kinematics_for_motion().forward_kinematics(joints)

    def move_to_pose_by_move_j(self, target: BasePose, duration_s: float) -> None:
        self._call_motion_with_stall_retry(
            lambda: self._move_to_pose_by_move_j(target, duration_s),
            timeout_s=max(0.1, float(duration_s)) + 5.0,
        )

    def move_l(self, target: BasePose, duration_s: float) -> None:
        self._call_motion_with_stall_retry(
            lambda: self._move_l(target, duration_s), timeout_s=max(0.1, float(duration_s)) + 8.0
        )

    def check_pose_reachable(
        self, target: BasePose, seed: Optional[JointPose], *, label: str
    ) -> JointPose:
        """Probe IK reachability without moving; used by workflow fail-fast prechecks."""

        return self._worker.call(
            lambda: self._check_pose_reachable(target, seed, label=label),
            timeout_s=5.0,
        )

    def check_pose_reachable_offworker(
        self, target: BasePose, seed: JointPose, *, label: str
    ) -> JointPose:
        """Pure-IK reachability probe safe to call from a non-worker thread.

        Bypasses the command worker so the probe can overlap a running motion
        (e.g. the return flow's background insert-chain computation while the
        book-stand pickup executes). An explicit ``seed`` is mandatory: with
        it the probe never reads live joint feedback and never issues motion
        commands. The only shared state is the lazily-built kinematics model,
        and ``_check_pose_reachable`` already guards it with the same
        ``_kinematics_lock`` the motion paths hold for their own solves, so
        the probe serializes only against another brief solve — never against
        trajectory execution.
        """

        if seed is None:
            raise ValueError("off-worker reachability probes require an explicit seed")
        return self._check_pose_reachable(target, seed, label=label)

    def open_gripper(
        self, target_deg: float, command: Optional[GripperCommand] = None
    ) -> None:
        """Open the gripper to an explicit angle in degrees and confirm arrival."""

        effective_command = command or GripperCommand()
        self._worker.call(
            lambda: self._move_gripper(float(target_deg), effective_command, monitored=False),
            timeout_s=(
                float(effective_command.timeout_s)
                + float(self.config.enable_feedback_timeout_s)
                + 2.0
            ),
        )

    def close_gripper_monitored(self, command: GripperCommand) -> dict[str, object]:
        """Close progressively and hold at the target, object contact, or timeout position."""

        return self._worker.call(
            lambda: self._move_gripper(command.close_deg, command, monitored=True),
            timeout_s=float(command.timeout_s) + float(self.config.enable_feedback_timeout_s) + 2.0,
        )

    def disable_at_safe_pose(self, duration_s: float = 3.0) -> dict[str, Any]:
        """Move to ``DISABLE_JOINT``, verify feedback, then disable all motors.

        This is the sole software path that leaves the arm motors disabled. The
        common enable path may briefly disable joint 7 while configuring its run
        mode, then enables all seven motors together. An already fully disabled
        arm is reported without transmitting anything; a partially enabled or
        unknown state is rejected without disabling.
        """

        duration = float(duration_s)
        if not math.isfinite(duration) or duration <= 0.0:
            raise ValueError("disable-pose duration must be a finite value greater than zero")
        return self._worker.call(
            lambda: self._disable_at_safe_pose(duration),
            # Include the motor-mode feedback wait and MoveJ's arrival timeout.
            timeout_s=duration + float(self.config.enable_feedback_timeout_s) + 5.0,
        )

    def health(self) -> dict[str, object]:
        return self._worker.call(self._health, timeout_s=1.0)

    def enable_status(self) -> dict[str, object]:
        """Report physical motor modes without sending an enable or motion command."""

        timeout_s = float(self.config.enable_feedback_timeout_s) + 0.5
        return self._worker.call(self._enable_status, timeout_s=timeout_s)

    def is_enabled(self) -> bool:
        """Synchronize and return the physical all-joint Motor-mode state."""

        timeout_s = float(self.config.enable_feedback_timeout_s) + 0.5
        return bool(self._worker.call(self._is_enabled, timeout_s=timeout_s))

    def is_gripper_ready(self) -> bool:
        """夹爪可用性：Motor 模式且无锁存故障（纯读取，不发送状态变更帧）。

        臂体与夹爪刻意分离（夹爪过流/过热掉扭矩时臂体仍可用），因此各
        “使能”入口需要在臂体已使能时单独依据本方法决定是否补跑完整使能
        序列；只看 is_enabled() 会让夹爪故障永远得不到恢复。
        """

        timeout_s = float(self.config.enable_feedback_timeout_s) + 0.5
        status = self._worker.call(self._enable_status, timeout_s=timeout_s)
        return status["gripper_enabled"] is True and not status["fault_codes"].get("7")

    def _connect(self) -> None:
        if self._transport is None:
            backend = self.config.backend.strip().lower()
            if backend in {"socketcan", "socket", "can"}:
                self._transport = SocketCanTransport(
                    interface=self.config.can_interface,
                    host_can_id=self.config.host_can_id,
                )
            elif backend == "slcan":
                self._transport = SlcanTransport(
                    port=self.config.slcan_port or self.config.can_interface,
                    serial_baudrate=self.config.slcan_baudrate,
                    can_bitrate=self.config.can_bitrate,
                    host_can_id=self.config.host_can_id,
                )
            else:
                raise ValueError("unsupported arm backend: %s" % self.config.backend)
        self._transport.connect()

    def _close(self) -> None:
        if not self._stop_control_loop():
            raise RuntimeError("arm control loop did not stop before transport close")
        if self._transport is not None:
            self._transport.close()
        self._enabled = False

    def _enable(self) -> None:
        transport = self._require_transport()
        can_timeout_s = float(self.config.can_timeout_s)
        if not math.isfinite(can_timeout_s) or can_timeout_s != 0.0:
            raise ValueError("arm enable requires can_timeout_s to be exactly 0.0")
        # A repeated enable must never leave a background Type-1 producer
        # running while the established motor-7 setup sequence executes.
        if not self._stop_control_loop():
            raise RuntimeError("arm control loop did not stop before enable")
        self._interrupt.clear()
        self._enabled = False
        for motor_id in range(1, 8):
            self._require_transport_result(
                transport.enable_feedback(motor_id), "enable feedback for motor %d" % motor_id
            )
        # 电机故障是锁存的（例如夹爪夹取过流），只发使能帧永远无法恢复，
        # 调参会因此陷入“使能永远失败”。使能前按反馈逐台清除已锁存的故障；
        # 无故障时不多发任何帧，既定上电时序保持不变。
        pre_enable_feedback = transport.feedback()
        for motor_id in range(1, 8):
            item = pre_enable_feedback.get(motor_id)
            if item is None or int(item.fault_code) == 0:
                continue
            self._require_transport_result(
                transport.disable_motor(motor_id, clear_fault=True),
                "clear latched fault on motor %d before enable" % motor_id,
            )
            time.sleep(0.03)
        # Preserve the established BookArm startup sequence: only the gripper
        # is reset into Type-1 motion control before all motors are enabled.
        # 这里再清一次夹爪故障，覆盖反馈过期但电机实际带故障的情况。
        self._require_transport_result(
            transport.disable_motor(7, clear_fault=True), "disable motor 7 before mode setup"
        )
        time.sleep(0.03)
        self._require_transport_result(
            transport.set_run_mode(7, RunMode.MOTION_CONTROL),
            "set motor 7 run mode to MOTION_CONTROL",
        )
        time.sleep(0.03)
        for motor_id in range(1, 8):
            self._require_transport_result(
                transport.enable_motor(motor_id), "enable motor %d" % motor_id
            )
            time.sleep(0.03)
        # The gripper receives no Type-1 targets while joints 1--6 move to the
        # debug pose. Clear any persisted timeout so it remains in Motor mode
        # and is ready for its first command after arm.enable() returns.
        self._require_transport_result(
            transport.set_can_timeout(7, can_timeout_s), "clear CAN timeout for motor 7"
        )
        # Motor enable can clear Type-2 subscriptions on this firmware. Renew
        # them only after the established enable sequence has finished; this
        # sends no state-change or motion command.
        gripper_feedback_requested_at = time.time()
        with self._can_send_lock:
            for motor_id in range(1, 8):
                self._require_transport_result(
                    transport.enable_feedback(motor_id),
                    "refresh feedback for motor %d after enable" % motor_id,
                )
        self._record_arm_feedback_request(success=True)
        deadline = time.monotonic() + float(self.config.enable_feedback_timeout_s)
        while True:
            try:
                feedback = transport.feedback()
                arm_issue = self._arm_feedback_issue(
                    feedback, requested_at=gripper_feedback_requested_at,
                )
                if arm_issue is not None:
                    raise RuntimeError(arm_issue)
                current_joint_pose = self._read_joint_pose()
                item = feedback.get(7)
                if item is None or float(item.timestamp_s) < gripper_feedback_requested_at:
                    raise RuntimeError("gripper feedback has not refreshed after enable")
                if int(item.mode_state) != int(MotorModeState.MOTOR):
                    raise RuntimeError("gripper joint is not in Motor mode after enable")
                gripper_angle_from_feedback(
                    feedback, self.config.joint_directions, self.config.feedback_max_age_s,
                    self.config.joint_offsets,
                )
                with self._control_lock:
                    self._arm_feedback_error = None
                self._enabled = True
                self._gripper_enabled = True
                self._start_control_loop(current_joint_pose)
                return
            except (RuntimeError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "arm enable failed: six joints and a fresh Motor-mode gripper feedback frame "
                        "were not confirmed; the enable sequence may have left motors in their previous state"
                    )
                time.sleep(0.03)

    @staticmethod
    def _require_transport_result(result: object, operation: str) -> None:
        """Treat an explicit false transport result as a failed SDK command."""

        if result is False:
            raise RuntimeError("arm transport command failed: %s" % operation)

    def _move_j(self, target: JointPose, duration_s: float) -> None:
        self._require_enabled()
        self._require_control_loop(allow_feedback_recovery=True)
        # A prior interrupted motion must not block a newly commanded move;
        # a new interrupt during this move still aborts it at the next
        # trajectory checkpoint (same contract as _disable_at_safe_pose).
        self._interrupt.clear()
        validate_joint_target(target, self.config.joint_limits)
        command_target = target
        start = self._refresh_arm_feedback()
        points = list(
            joint_trajectory(
                start,
                command_target,
                duration_s,
                self.config.control_rate_hz,
                self.config.max_velocity_rad_s,
                self.config.max_acceleration_rad_s2,
                self.config.max_jerk_rad_s3,
            )
        )
        if not points:
            raise RuntimeError("arm MoveJ produced no trajectory points")
        deadline = time.monotonic() + points[-1].time_s + 1.5
        previous_time = 0.0
        for point in points:
            self._check_not_interrupted()
            self._set_arm_control_target(point.position, point.velocities_rad_s)
            sleep_time = point.time_s - previous_time
            previous_time = point.time_s
            if sleep_time > 0.0:
                self._interrupt.wait(sleep_time)
        while True:
            self._check_not_interrupted()
            measured = self._read_joint_pose()
            try:
                ensure_joint_arrival(command_target, measured, self.config.final_tolerance_rad)
                return
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                self._set_arm_control_target(command_target, (0.0,) * 6)
                self._interrupt.wait(0.03)

    def _disable_at_safe_pose(self, duration_s: float) -> dict[str, Any]:
        """Run the checked disable sequence on the arm's owning worker."""

        # A prior interrupted motion must not prevent an explicitly confirmed
        # safe-pose move. A new interrupt during this sequence still aborts it.
        self._interrupt.clear()
        status = self._enable_status()
        enabled = status["arm_enabled"]
        joint_enabled = status["joint_enabled"]
        # The gripper (joint 7) may legitimately be offline (thermal firmware
        # torque cut): the arm body is still parked and disabled, and the
        # gripper disable frame is sent best-effort by the transport.
        gripper_was_offline = status["gripper_enabled"] is not True
        if enabled is None:
            raise RuntimeError("cannot disable arm safely: %s; no disable command was sent" % status["reason"])
        if enabled is False:
            if all(joint_enabled[str(motor_id)] is False for motor_id in range(1, 7)):
                return {
                    "already_disabled": True,
                    "disable_command_sent": False,
                    "moved_to_disable_pose": False,
                    "disable_pose_joint_rad": DISABLE_JOINT.values_rad,
                    "gripper_was_offline": gripper_was_offline,
                }
            raise RuntimeError(
                "cannot disable arm safely: arm is partially enabled; "
                "no disable command was sent"
            )

        self._enabled = True
        expected_disable_pose = DISABLE_JOINT
        measured = self._read_joint_pose()
        moved = False
        try:
            ensure_joint_arrival(expected_disable_pose, measured, self.config.final_tolerance_rad)
        except RuntimeError:
            self._move_j(DISABLE_JOINT, duration_s)
            moved = True

        measured = self._read_joint_pose()
        ensure_joint_arrival(expected_disable_pose, measured, self.config.final_tolerance_rad)
        final_status = self._enable_status()
        if final_status["arm_enabled"] is not True:
            raise RuntimeError(
                "cannot disable arm safely: motor enable state changed while moving to the disable pose"
                "; no disable command was sent (%s)" % final_status["reason"]
            )
        # Stop the only Type-1 producer before the explicitly confirmed
        # disable frames are sent, so no hold target can follow a disable.
        if not self._stop_control_loop():
            raise RuntimeError(
                "cannot disable arm safely: arm control loop did not stop; no disable command was sent"
            )
        self._require_transport().emergency_stop()
        self._enabled = False
        self._gripper_enabled = False
        return {
            "already_disabled": False,
            "disable_command_sent": True,
            "moved_to_disable_pose": moved,
            "all_motors_enabled_before_disable": True,
            "disable_pose_joint_rad": expected_disable_pose.values_rad,
            "measured_joint_pose_rad": measured.values_rad,
            "gripper_was_offline": gripper_was_offline,
        }

    def _check_pose_reachable(
        self, target: BasePose, seed: Optional[JointPose], *, label: str
    ) -> JointPose:
        """Solve ``target`` exactly like MovePByJ would, without moving the arm.

        Same solver, same known-good fallback seeds, and the same margin-0
        boundary rescue as _move_to_pose_by_move_j, so a passing precheck means
        the execution-time solve passes too.
        """

        effective_seed = seed if seed is not None else self._refresh_arm_feedback()
        try:
            with self._kinematics_lock:
                solution = self._kinematics_for_motion().solve_ik(
                    target,
                    effective_seed,
                    fallback_seeds=(ZERO_JOINT, BOOK_DEBUG_JOINT),
                )
        except Exception as error:
            try:
                with self._kinematics_lock:
                    solution = self._kinematics_for_motion().solve_ik(
                        target,
                        effective_seed,
                        fallback_seeds=(ZERO_JOINT, BOOK_DEBUG_JOINT),
                        joint_limit_margin=0.0,
                    )
            except Exception:
                raise RuntimeError(
                    "arm pose unreachable (%s): IK did not converge for target %s from joint seed %s: %s"
                    % (label, target.values(), effective_seed.values_rad, error)
                ) from error
        try:
            validate_joint_target(solution, self.config.joint_limits)
        except ValueError as error:
            raise RuntimeError("arm pose unreachable (%s): %s" % (label, error)) from error
        return solution

    def _move_to_pose_by_move_j(self, target: BasePose, duration_s: float) -> None:
        self._require_enabled()
        self._require_control_loop(allow_feedback_recovery=True)
        seed = self._refresh_arm_feedback()
        try:
            with self._kinematics_lock:
                joint_target = self._kinematics_for_motion().solve_ik(
                    target,
                    seed,
                    # DLS convergence is seed-sensitive on reachable targets at
                    # the workspace edge; retry from the two known-good poses.
                    fallback_seeds=(ZERO_JOINT, BOOK_DEBUG_JOINT),
                )
        except Exception as error:
            # Same boundary rescue as _move_l: deep-shelf targets can sit
            # inside the conservative 0.05 rad solve margin while remaining
            # within the true URDF limits, so retry once without the margin.
            try:
                with self._kinematics_lock:
                    joint_target = self._kinematics_for_motion().solve_ik(
                        target,
                        seed,
                        fallback_seeds=(ZERO_JOINT, BOOK_DEBUG_JOINT),
                        joint_limit_margin=0.0,
                    )
            except Exception:
                raise RuntimeError(
                    "MovePByJ inverse kinematics failed for target %s from joint seed %s: %s"
                    % (target.values(), seed.values_rad, error)
                ) from error
        try:
            self._move_j(joint_target, duration_s)
        except Exception as error:
            raise RuntimeError(
                "MovePByJ MoveJ execution failed for joint target %s: %s"
                % (joint_target.values_rad, error)
            ) from error

    def _move_l(self, target: BasePose, duration_s: float) -> None:
        self._require_enabled()
        self._require_control_loop(allow_feedback_recovery=True)
        # Same interrupt-recovery contract as _move_j.
        self._interrupt.clear()
        start_joint = self._refresh_arm_feedback()
        with self._kinematics_lock:
            kinematics = self._kinematics_for_motion()
            start_pose = kinematics.forward_kinematics(start_joint)
        duration = float(duration_s)
        if not math.isfinite(duration) or duration <= 0.0:
            raise ValueError("MoveL duration must be finite and greater than zero")
        # Match the GUI SDK's synchronous MoveL sampling: 10 ms minimum spacing.
        steps = min(1200, max(50, int(math.ceil(duration / 0.01)), 2))
        seed = start_joint
        path = [start_joint]
        for index in range(1, steps + 1):
            self._check_not_interrupted()
            waypoint = linear_pose(start_pose, target, float(index) / steps)
            with self._kinematics_lock:
                candidate, error_norm = kinematics.ik_step(
                    waypoint,
                    seed,
                    damping=8e-3,
                    max_step=0.25,
                    max_iter=8,
                    converge_eps=3e-4,
                )
            if error_norm > 3e-4:
                with self._kinematics_lock:
                    try:
                        seed = kinematics.solve_ik(waypoint, candidate, max_iter=120, eps=3e-4)
                    except RuntimeError:
                        # Deep-shelf push steps are only solvable within 0.05 rad
                        # of a joint limit, where the conservative margin rejects
                        # every iterate. ik_step already permits margin-0
                        # solutions, so retry without the margin;
                        # validate_joint_target below still enforces the true
                        # URDF limits — this widens solver convergence, not the
                        # allowed joint space.
                        seed = kinematics.solve_ik(
                            waypoint,
                            candidate,
                            max_iter=120,
                            eps=3e-4,
                            joint_limit_margin=0.0,
                        )
            else:
                seed = candidate
            validate_joint_target(seed, self.config.joint_limits)
            path.append(seed)

        positions = tuple(path)
        velocities, accelerations = joint_path_derivatives(positions, duration)
        velocity_peak = max((abs(value) for row in velocities for value in row), default=0.0)
        acceleration_peak = max((abs(value) for row in accelerations for value in row), default=0.0)
        duration_scale = max(
            1.0,
            velocity_peak / max(self.config.velocity_limit_rad_s, 1e-9),
            math.sqrt(acceleration_peak / max(self.config.max_acceleration_rad_s2, 1e-9)),
        )
        actual_duration = duration * duration_scale
        if duration_scale > 1.0:
            velocities, accelerations = joint_path_derivatives(positions, actual_duration)

        deadline = time.monotonic() + actual_duration + 1.5
        previous_time = 0.0
        for index, position in enumerate(positions[1:], start=1):
            self._check_not_interrupted()
            self._set_arm_control_target(position, velocities[index])
            point_time = actual_duration * index / steps
            sleep_time = point_time - previous_time
            previous_time = point_time
            if sleep_time > 0.0:
                self._interrupt.wait(sleep_time)
        while True:
            self._check_not_interrupted()
            current = self._read_joint_pose()
            try:
                ensure_joint_arrival(positions[-1], current, self.config.final_tolerance_rad)
                return
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                self._set_arm_control_target(positions[-1], (0.0,) * 6)
                self._interrupt.wait(0.03)

    def _move_gripper(
        self, target_deg: float, command: GripperCommand, monitored: bool
    ) -> Optional[dict[str, object]]:
        self._require_enabled()
        # Same interrupt-recovery contract as _move_j.
        self._interrupt.clear()
        # A new open/close supersedes any previous carry hold; a successful
        # close re-arms it below (an open never does).
        self._clear_gripper_carry()
        transport = self._require_transport()
        # _refresh_gripper_feedback rejects a joint 7 outside Motor mode with
        # the gripper-specific error and marks only the gripper offline.
        self._refresh_gripper_feedback(transport)
        target_rad = math.radians(float(target_deg))
        lower, upper = self.config.joint_limits[7]
        if not lower <= target_rad <= upper:
            raise ValueError("gripper target %.2f deg is outside configured range" % float(target_deg))
        if not monitored:
            # Opening must be feedback-checked as well. A one-shot CAN target can
            # be lost or ignored while still making the workflow look successful.
            open_command = replace(
                command,
                close_speed_deg_s=command.open_speed_deg_s,
                effort=0.0,
                start_effort=0.0,
                start_boost_s=0.0,
            )
            result = self._close_gripper_monitored(
                transport, target_rad, open_command, register_carry=False
            )
            if result["reason"] != "target_reached":
                raise RuntimeError(
                    "gripper open failed to reach %.2f deg: %s at %.2f deg"
                    % (
                        float(target_deg),
                        result["reason"],
                        math.degrees(float(result["final_angle_rad"])),
                    )
                )
            return

        return self._close_gripper_monitored(transport, target_rad, command)

    def _refresh_gripper_feedback(self, transport: ArmTransport) -> float:
        """Request one current Type-2 frame before sending a gripper target.

        This is feedback subscription only; it deliberately does not send a
        motor enable, disable, or motion frame. It prevents a stale reading
        from being used as the starting point for the Type-1 gripper command.
        """

        requested_at = time.time()
        with self._can_send_lock:
            self._require_transport_result(
                transport.enable_feedback(7), "refresh feedback for gripper command"
            )
        deadline = time.monotonic() + float(self.config.enable_feedback_timeout_s)
        last_reason = "gripper feedback is missing"
        while time.monotonic() < deadline:
            feedback = transport.feedback()
            item = feedback.get(7)
            if item is None:
                last_reason = "gripper feedback is missing"
            elif float(item.timestamp_s) < requested_at:
                last_reason = "gripper feedback did not refresh"
            elif int(item.mode_state) != int(MotorModeState.MOTOR):
                # Only the gripper is marked offline; the arm body keeps working.
                self._gripper_enabled = False
                raise RuntimeError(
                    "夹爪电机未使能（可能过热保护掉扭矩），请先冷却后再操作夹爪"
                )
            else:
                try:
                    self._gripper_enabled = True
                    return gripper_angle_from_feedback(
                        feedback, self.config.joint_directions, self.config.feedback_max_age_s,
                        self.config.joint_offsets,
                    )
                except RuntimeError as error:
                    last_reason = str(error)
            self._interrupt.wait(0.03)
        raise RuntimeError(
            "gripper feedback refresh failed before motion command: %s; no gripper motion was sent" % last_reason
        )

    def _feedback_keepalive_interval_s(self) -> float:
        interval_s = float(self.config.feedback_keepalive_interval_s)
        max_age_s = float(self.config.feedback_max_age_s)
        if not math.isfinite(interval_s) or interval_s <= 0.0:
            raise ValueError("arm feedback_keepalive_interval_s must be a finite value greater than zero")
        if not math.isfinite(max_age_s) or max_age_s <= 0.0:
            raise ValueError("arm feedback_max_age_s must be a finite value greater than zero")
        if interval_s >= max_age_s:
            raise ValueError(
                "arm feedback_keepalive_interval_s must be less than feedback_max_age_s"
            )
        return interval_s

    def _record_arm_feedback_request(self, success: bool, error: Optional[str] = None) -> None:
        """Record Type-24 request diagnostics without changing motor state."""

        with self._control_lock:
            self._last_arm_feedback_request_s = time.time()
            self._last_arm_feedback_request_error = None if success else error

    def _request_arm_feedback(self) -> float:
        """Request Type-2 feedback for 1--6 without enabling, disabling, or moving motors."""

        transport = self._require_transport()
        requested_at = time.time()
        try:
            with self._can_send_lock:
                for motor_id in range(1, 7):
                    self._require_transport_result(
                        transport.enable_feedback(motor_id),
                        "request feedback for arm joint %d" % motor_id,
                    )
        except Exception as error:
            self._record_arm_feedback_request(success=False, error=str(error))
            raise
        self._record_arm_feedback_request(success=True)
        return requested_at

    def _arm_feedback_issue(
        self, feedback: Dict[int, MotorFeedback], requested_at: Optional[float] = None
    ) -> Optional[str]:
        """Return a precise reason when a 1--6 feedback snapshot is unusable."""

        now = time.time()
        max_age_s = float(self.config.feedback_max_age_s)
        for motor_id in range(1, 7):
            item = feedback.get(motor_id)
            if item is None:
                return "joint %d feedback is missing" % motor_id
            timestamp_s = float(item.timestamp_s)
            if requested_at is not None and timestamp_s < requested_at:
                return "joint %d feedback did not refresh after the Type-24 request" % motor_id
            age_s = now - timestamp_s
            if age_s > max_age_s:
                return "joint %d feedback is stale (age %.3fs, limit %.3fs)" % (
                    motor_id, age_s, max_age_s,
                )
            # 故障码不冻结目标更新（锁存型故障可由「故障恢复」清除）；
            # 缺失/过期/非 Motor 模式仍然拦截。
            if int(item.mode_state) != int(MotorModeState.MOTOR):
                return "joint %d is not in Motor mode" % motor_id
        return None

    def _set_arm_feedback_error(self, reason: str) -> None:
        """Freeze future target updates while preserving the current hold target."""

        with self._control_lock:
            if self._arm_feedback_error is None:
                self._arm_feedback_error = str(reason)

    def _refresh_arm_feedback(self) -> JointPose:
        """Require a post-request 1--6 feedback snapshot before new arm motion."""

        self._require_control_loop(allow_feedback_recovery=True)
        requested_at = self._request_arm_feedback()
        deadline = time.monotonic() + float(self.config.enable_feedback_timeout_s)
        last_reason = "arm feedback is missing"
        while time.monotonic() < deadline:
            feedback = self._require_transport().feedback()
            issue = self._arm_feedback_issue(feedback, requested_at=requested_at)
            if issue is None:
                try:
                    pose = joint_pose_from_feedback(
                        feedback, self.config.joint_directions,
                        self.config.feedback_max_age_s, self.config.joint_offsets,
                    )
                except RuntimeError as error:
                    last_reason = str(error)
                else:
                    with self._control_lock:
                        self._arm_feedback_error = None
                    return pose
            else:
                last_reason = issue
            self._interrupt.wait(0.03)
        failure = "arm feedback refresh failed before motion command: %s; no new arm motion target was published" % last_reason
        self._set_arm_feedback_error(failure)
        raise RuntimeError(failure)

    def _close_gripper_monitored(
        self,
        transport: ArmTransport,
        target_rad: float,
        command: GripperCommand,
        *,
        register_carry: bool = True,
    ) -> dict[str, object]:
        """Progressively close while detecting book contact from sustained feedback loss."""

        close_speed = math.radians(abs(float(command.close_speed_deg_s)))
        monitor_period_s = max(0.01, float(command.monitor_period_s))
        configured_timeout_s = max(float(command.timeout_s), monitor_period_s)
        target_tolerance = math.radians(abs(float(command.target_tolerance_deg)))
        stall_tolerance = math.radians(abs(float(command.stall_tolerance_deg)))
        stall_time_s = max(0.05, float(command.stall_time_s))
        min_monitor_s = max(0.0, float(command.min_monitor_s))
        hold_margin = math.radians(max(0.0, float(command.hold_margin_deg)))
        command_lead_s = max(0.0, float(command.command_lead_s))
        configured_stall_lead = math.radians(max(0.0, float(command.stall_lead_threshold_deg)))
        timeout_margin_s = max(0.0, float(command.close_timeout_margin_s))
        start_boost_s = max(0.0, float(command.start_boost_s))
        contact_hold_offset = math.radians(max(0.0, float(command.contact_hold_offset_deg)))
        contact_hold_duration_s = max(0.0, float(command.contact_hold_duration_s))
        contact_hold_rate_hz = float(command.contact_hold_rate_hz)
        effort = float(command.effort)
        startup_effort = max(effort, float(command.start_effort))
        values = (
            close_speed, monitor_period_s, configured_timeout_s, target_tolerance, stall_tolerance,
            stall_time_s, min_monitor_s, hold_margin, command_lead_s, configured_stall_lead,
            timeout_margin_s, start_boost_s, contact_hold_offset, contact_hold_duration_s,
            contact_hold_rate_hz, effort, startup_effort,
        )
        if (
            not all(math.isfinite(value) for value in values)
            or close_speed <= 0.0
            or contact_hold_rate_hz <= 0.0
        ):
            raise ValueError("gripper monitored close requires finite positive speed and timing values")

        start_angle = gripper_angle_from_feedback(
            transport.feedback(), self.config.joint_directions,
            self.config.feedback_max_age_s, self.config.joint_offsets,
        )
        direction = 1.0 if target_rad >= start_angle else -1.0
        expected_travel_s = abs(target_rad - start_angle) / close_speed
        timeout_s = min(configured_timeout_s, expected_travel_s + timeout_margin_s)
        max_command_feedback_gap = close_speed * command_lead_s + hold_margin
        if configured_stall_lead > max_command_feedback_gap + 1e-9:
            raise ValueError(
                "gripper object-contact lead threshold %.2f deg exceeds the reachable %.2f deg"
                % (math.degrees(configured_stall_lead), math.degrees(max_command_feedback_gap))
            )
        command_angle = float(start_angle)
        last_feedback = float(start_angle)
        contact_window_start_angle = float(start_angle)
        contact_since: Optional[float] = None
        contact_eligible_after_s = max(min_monitor_s, start_boost_s)
        start_time = time.monotonic()
        last_command_time = start_time
        reason = "timeout"
        contact_hold_details: dict[str, object] = {}

        while True:
            self._check_not_interrupted()
            now = time.monotonic()
            elapsed = now - start_time
            last_feedback = gripper_angle_from_feedback(
                transport.feedback(), self.config.joint_directions,
                self.config.feedback_max_age_s, self.config.joint_offsets,
            )
            remaining = direction * (target_rad - last_feedback)
            if remaining <= target_tolerance:
                command_angle = target_rad
                reason = "target_reached"
                break

            command_feedback_gap = max(0.0, direction * (command_angle - last_feedback))
            progress = direction * (last_feedback - contact_window_start_angle)
            if command_feedback_gap < configured_stall_lead or elapsed < contact_eligible_after_s:
                contact_window_start_angle = last_feedback
                contact_since = None
            elif progress > stall_tolerance:
                contact_window_start_angle = last_feedback
                contact_since = None
            else:
                if contact_since is None:
                    contact_since = now
                elif now - contact_since >= stall_time_s:
                    command_angle = last_feedback + direction * min(
                        contact_hold_offset,
                        max(0.0, remaining),
                    )
                    reason = "object_contact"
                    command_angle, last_feedback, contact_hold_details = self._hold_gripper_contact(
                        transport,
                        target_rad,
                        direction,
                        command,
                        contact_hold_offset,
                        contact_hold_duration_s,
                        contact_hold_rate_hz,
                        effort,
                    )
                    break

            if elapsed >= timeout_s:
                command_angle = last_feedback + direction * min(hold_margin, max(0.0, remaining))
                reason = "timeout"
                break

            desired = command_angle + direction * close_speed * max(0.0, now - last_command_time)
            last_command_time = now
            if direction > 0.0:
                command_angle = min(
                    min(desired, target_rad),
                    last_feedback + close_speed * command_lead_s + hold_margin,
                )
            else:
                command_angle = max(
                    max(desired, target_rad),
                    last_feedback - close_speed * command_lead_s - hold_margin,
                )
            active_effort = startup_effort if elapsed < start_boost_s else effort
            self._send_gripper_target(transport, command_angle, command, active_effort)
            self._interrupt.wait(monitor_period_s)

        self._check_not_interrupted()
        self._send_gripper_target(transport, command_angle, command, effort)
        carry_details: dict[str, object] = {}
        if (
            register_carry
            and reason in ("object_contact", "target_reached")
            and self._control_running.is_set()
        ):
            # Stage two of the grip-and-relax: the control loop keeps the book
            # with a reduced-torque carry frame instead of the closing effort.
            # When the loop is not running (special enable timing) the single
            # frame above is all that is sent.
            carry_lead = math.radians(max(0.0, float(command.carry_lead_deg)))
            carry_effort = float(command.carry_effort)
            if (
                not math.isfinite(carry_lead)
                or not math.isfinite(carry_effort)
                or carry_effort < 0.0
            ):
                raise ValueError(
                    "gripper carry hold requires finite non-negative effort and lead"
                )
            carry_target = command_angle + direction * carry_lead
            self._set_gripper_carry(carry_target, command.kp, command.kd, carry_effort)
            carry_details = {
                "carry_hold": True,
                "carry_target_angle_rad": carry_target,
                "carry_effort": carry_effort,
                "carry_lead_deg": math.degrees(carry_lead),
            }
        return {
            "reason": reason,
            "target_angle_rad": target_rad,
            "final_angle_rad": last_feedback,
            "command_angle_rad": command_angle,
            "elapsed_s": time.monotonic() - start_time,
            "effective_timeout_s": timeout_s,
            "expected_travel_s": expected_travel_s,
            **contact_hold_details,
            **carry_details,
        }

    def _hold_gripper_contact(
        self,
        transport: ArmTransport,
        target_rad: float,
        direction: float,
        command: GripperCommand,
        contact_hold_offset: float,
        contact_hold_duration_s: float,
        contact_hold_rate_hz: float,
        effort: float,
    ) -> tuple[float, float, dict[str, object]]:
        """Refresh feedback and keep a bounded closing lead after object contact."""

        period_s = 1.0 / contact_hold_rate_hz
        deadline = time.monotonic() + contact_hold_duration_s
        command_angle = 0.0
        last_feedback = 0.0
        command_count = 0
        while True:
            self._check_not_interrupted()
            now = time.monotonic()
            if now >= deadline:
                break
            last_feedback = gripper_angle_from_feedback(
                transport.feedback(),
                self.config.joint_directions,
                self.config.feedback_max_age_s,
                self.config.joint_offsets,
            )
            remaining = direction * (target_rad - last_feedback)
            command_angle = last_feedback + direction * min(contact_hold_offset, max(0.0, remaining))
            self._send_gripper_target(transport, command_angle, command, effort)
            command_count += 1
            self._interrupt.wait(min(period_s, max(0.0, deadline - time.monotonic())))

        self._check_not_interrupted()
        return command_angle, last_feedback, {
            "contact_hold_applied": True,
            "contact_hold_offset_deg": math.degrees(contact_hold_offset),
            "contact_hold_duration_s": contact_hold_duration_s,
            "contact_hold_rate_hz": contact_hold_rate_hz,
            "contact_hold_command_count": command_count,
        }

    def _send_gripper_target(
        self, transport: ArmTransport, target_rad: float, command: GripperCommand, effort: float
    ) -> None:
        direction = self._joint_direction(7)
        motor_position = float(target_rad) * direction + float(self.config.joint_offsets[7])
        with self._can_send_lock:
            self._require_transport_result(
                transport.send_motion(
                    7, motor_position, 0.0, float(command.kp), float(command.kd), float(effort)
                ),
                "send gripper motion target",
            )

    def _start_control_loop(self, initial_target: JointPose) -> None:
        """Start the persistent Type-1 producer for arm joints 1--6.

        The startup motor-enable sequence deliberately remains in ``_enable``.
        This method only begins holding the feedback-confirmed position after
        that sequence has completed successfully.
        """

        rate_hz = float(self.config.control_rate_hz)
        if not math.isfinite(rate_hz) or rate_hz <= 0.0:
            raise ValueError("arm control_rate_hz must be a finite value greater than zero")
        # Validate before creating the thread, so invalid feedback timing is
        # reported synchronously instead of terminating the loop silently.
        self._feedback_keepalive_interval_s()
        if not self._stop_control_loop():
            raise RuntimeError("arm control loop did not stop before restart")
        with self._control_lock:
            self._control_target = initial_target
            self._control_velocities = (0.0,) * 6
            self._control_error = None
        self._control_stop.clear()
        self._control_running.set()
        self._control_thread = threading.Thread(
            target=self._control_loop_main,
            name="bookarm-arm-control",
            daemon=True,
        )
        self._control_thread.start()

    def _stop_control_loop(self, timeout_s: float = 1.0) -> bool:
        """Stop the 1--6 Type-1 producer without changing motor enable state."""

        self._control_stop.set()
        self._control_running.clear()
        thread = self._control_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout_s)))
            if thread.is_alive():
                return False
        self._control_thread = None
        # The carry hold dies with its producer: any later enable/restart must
        # re-establish it through a fresh successful close.
        self._clear_gripper_carry()
        return True

    def _set_gripper_carry(
        self, target_rad: float, kp: float, kd: float, effort: float
    ) -> None:
        """Arm the low-torque carry hold; the control loop re-sends it."""

        with self._control_lock:
            self._gripper_carry = {
                "target_rad": float(target_rad),
                "kp": float(kp),
                "kd": float(kd),
                "effort": float(effort),
            }

    def _clear_gripper_carry(self) -> None:
        with self._control_lock:
            self._gripper_carry = None

    def _send_gripper_carry_frame(
        self, carry: Dict[str, float], gripper_feedback: Optional[Any]
    ) -> None:
        """Send one carry-hold frame for joint 7 from the control loop.

        A gripper that dropped out of Motor mode (thermal firmware torque cut)
        stops the carry hold instead of erroring the arm control loop. While
        the gripper runs hot the carry torque is halved with hysteresis.
        """

        now = time.time()
        fresh = (
            gripper_feedback is not None
            and now - float(gripper_feedback.timestamp_s)
            <= float(self.config.feedback_max_age_s)
        )
        if not fresh or int(gripper_feedback.mode_state) != int(MotorModeState.MOTOR):
            print(
                "[ARM] gripper motor is not in Motor mode; carry hold stopped",
                file=sys.stderr,
            )
            self._gripper_enabled = False
            self._clear_gripper_carry()
            return
        temperature = float(gripper_feedback.temperature_c)
        with self._control_lock:
            derated = self._gripper_carry_derated
        if temperature >= float(self.config.gripper_derate_onset_c):
            derated = True
        elif temperature < float(self.config.gripper_derate_release_c):
            derated = False
        # Between release and onset the previous state holds (5 C hysteresis).
        with self._control_lock:
            self._gripper_carry_derated = derated
        effort = float(carry["effort"])
        if derated:
            effort = max(float(self.config.gripper_derate_min_effort), effort / 2.0)
        direction = self._joint_direction(7)
        motor_position = carry["target_rad"] * direction + float(self.config.joint_offsets[7])
        with self._can_send_lock:
            self._require_transport_result(
                self._require_transport().send_motion(
                    7,
                    motor_position,
                    0.0,
                    carry["kp"],
                    carry["kd"],
                    effort,
                ),
                "send gripper carry-hold frame",
            )

    def _control_loop_main(self) -> None:
        """Continuously hold/update the six arm targets at the configured rate."""

        period_s = 1.0 / float(self.config.control_rate_hz)
        feedback_interval_s = self._feedback_keepalive_interval_s()
        carry_every_ticks = max(
            1, int(round(float(self.config.control_rate_hz) / _GRIPPER_CARRY_RATE_HZ))
        )
        tick_index = 0
        next_tick = time.monotonic()
        next_feedback_request = next_tick
        while not self._control_stop.is_set():
            wait_s = next_tick - time.monotonic()
            if wait_s > 0.0 and self._control_stop.wait(wait_s):
                break
            if self._control_stop.is_set() or not self._enabled:
                break
            now = time.monotonic()
            if now >= next_feedback_request:
                try:
                    self._request_arm_feedback()
                except BaseException as error:
                    self._set_arm_feedback_error(
                        "arm feedback keepalive request failed: %s" % error
                    )
                next_feedback_request = now + feedback_interval_s
            feedback = self._require_transport().feedback()
            feedback_issue = self._arm_feedback_issue(feedback)
            if feedback_issue is not None:
                self._set_arm_feedback_error(
                    "arm feedback keepalive is unhealthy: %s" % feedback_issue
                )
            with self._control_lock:
                target = self._control_target
                velocities = self._control_velocities
                carry = self._gripper_carry
            try:
                self._send_arm_target(target, velocities)
                tick_index += 1
                if carry is not None and tick_index % carry_every_ticks == 0:
                    self._send_gripper_carry_frame(carry, feedback.get(7))
            except BaseException as error:
                # Keep the physical enable state untouched. A failed CAN send
                # must be visible to the next commanded arm motion, not hidden
                # behind a silently dead holding loop.
                with self._control_lock:
                    self._control_error = "arm 200 Hz control loop stopped: %s" % error
                self._control_stop.set()
                break
            next_tick += period_s
            if next_tick < time.monotonic():
                next_tick = time.monotonic()
        self._control_running.clear()

    def _require_control_loop(self, allow_feedback_recovery: bool = False) -> None:
        with self._control_lock:
            error = self._control_error
            feedback_error = self._arm_feedback_error
        if error is not None:
            raise RuntimeError(error)
        if feedback_error is not None and not allow_feedback_recovery:
            raise RuntimeError("%s; no new arm motion target was published" % feedback_error)
        if not self._control_running.is_set():
            raise RuntimeError("arm 200 Hz control loop is not running; no arm motion was sent")

    def _set_arm_control_target(
        self, target: JointPose, velocity_rad_s: float | Sequence[float]
    ) -> None:
        """Publish a target consumed by the persistent Type-1 control loop."""

        if isinstance(velocity_rad_s, (int, float)):
            velocities = (float(velocity_rad_s),) * 6
        else:
            velocities = tuple(float(value) for value in velocity_rad_s)
            if len(velocities) != 6:
                raise ValueError("arm velocity target requires exactly six values")
        if not all(math.isfinite(value) for value in velocities):
            raise ValueError("arm velocity target values must be finite")
        self._require_control_loop()
        with self._control_lock:
            self._control_target = target
            self._control_velocities = velocities

    def _send_arm_target(
        self, target: JointPose, velocity_rad_s: float | Sequence[float]
    ) -> None:
        """Emit one six-axis Type-1 control tick for the persistent loop."""

        transport = self._require_transport()
        if isinstance(velocity_rad_s, (int, float)):
            velocities = (float(velocity_rad_s),) * 6
        else:
            velocities = tuple(float(value) for value in velocity_rad_s)
            if len(velocities) != 6:
                raise ValueError("arm velocity target requires exactly six values")
        if not all(math.isfinite(value) for value in velocities):
            raise ValueError("arm velocity target values must be finite")
        gravity_torques = self._gravity_torques(target)
        with self._can_send_lock:
            for motor_id, (joint_value, joint_velocity, gravity_torque) in enumerate(
                zip(target.values_rad, velocities, gravity_torques), start=1
            ):
                direction = self._joint_direction(motor_id)
                motor_position = float(joint_value) * direction + float(self.config.joint_offsets[motor_id])
                motor_velocity = min(
                    max(float(joint_velocity), -float(self.config.velocity_limit_rad_s)),
                    float(self.config.velocity_limit_rad_s),
                ) * direction
                motor_torque = (
                    float(gravity_torque)
                    * direction
                    * float(self.config.gravity_feedforward_ratio)
                    * float(self.config.gravity_joint_scale.get(motor_id, 1.0))
                )
                self._require_transport_result(
                    transport.send_motion(
                        motor_id, motor_position, motor_velocity,
                        float(self.config.default_kp), float(self.config.default_kd), motor_torque,
                    ),
                    "send motion target for motor %d" % motor_id,
                )

    def _read_joint_pose(self) -> JointPose:
        return joint_pose_from_feedback(
            self._require_transport().feedback(), self.config.joint_directions,
            self.config.feedback_max_age_s, self.config.joint_offsets,
        )

    def _gravity_torques(self, target: JointPose) -> tuple[float, ...]:
        """Return logical-joint gravity feedforward for a direct Type-1 command."""

        ratio = float(self.config.gravity_feedforward_ratio)
        if not math.isfinite(ratio):
            raise ValueError("gravity feedforward ratio must be finite")
        if ratio == 0.0:
            return (0.0,) * 6
        # Pinocchio stores mutable kinematics data, so the control thread and
        # synchronous IK/FK calls must not use it concurrently.
        with self._kinematics_lock:
            torques = tuple(self._kinematics_for_motion().compute_gravity(target))
        if len(torques) != 6 or not all(math.isfinite(value) for value in torques):
            raise RuntimeError("arm gravity model returned invalid six-joint torque values")
        return torques

    def _joint_direction(self, motor_id: int) -> float:
        direction = float(self.config.joint_directions[motor_id])
        if not math.isfinite(direction) or abs(direction) < 1e-9:
            raise ValueError("arm joint %d direction must be a non-zero finite value" % motor_id)
        return direction

    def _kinematics_for_motion(self) -> PinocchioKinematics:
        if self._kinematics is None:
            self._kinematics = PinocchioKinematics(
                self.config.urdf_path,
                self.config.tcp_offset,
                joint_limits=self.config.joint_limits,
                inertia_config_path=self.config.inertia_config_path,
            )
        return self._kinematics

    def _health(self) -> dict[str, object]:
        transport = self._require_transport()
        feedback = transport.feedback()
        now = time.time()
        try:
            self._read_joint_pose()
            gripper_angle_from_feedback(
                feedback, self.config.joint_directions,
                self.config.feedback_max_age_s, self.config.joint_offsets,
            )
            healthy = True
            reason = None
        except RuntimeError as error:
            healthy = False
            reason = str(error)
        with self._control_lock:
            control_error = self._control_error
            arm_feedback_error = self._arm_feedback_error
            last_request_s = self._last_arm_feedback_request_s
            last_request_error = self._last_arm_feedback_request_error
        joint_feedback = {}
        for motor_id in range(1, 8):
            item = feedback.get(motor_id)
            if item is None:
                joint_feedback[str(motor_id)] = {"age_s": None, "mode_state": None, "fault_code": None}
            else:
                joint_feedback[str(motor_id)] = {
                    "age_s": max(0.0, now - float(item.timestamp_s)),
                    "mode_state": int(item.mode_state),
                    "fault_code": int(item.fault_code),
                }
        return {
            "enabled": self._enabled,
            "healthy_feedback": healthy,
            "reason": reason,
            "can_fps": transport.can_fps(),
            "feedback_motor_ids": sorted(feedback.keys()),
            "joint_feedback": joint_feedback,
            "control_loop_running": self._control_running.is_set(),
            "control_loop_error": control_error,
            "arm_feedback_error": arm_feedback_error,
            "feedback_keepalive_interval_s": self.config.feedback_keepalive_interval_s,
            "last_arm_feedback_request_s": last_request_s,
            "last_arm_feedback_request_age_s": (
                None if last_request_s is None else max(0.0, now - last_request_s)
            ),
            "last_arm_feedback_request_error": last_request_error,
        }

    def _enable_status(self) -> dict[str, object]:
        """Enable Type 2 reporting, then interpret the mode bits in its feedback IDs.

        Type 24 only controls feedback reports; unlike Type 3 it does not enable a
        motor. A motor that does not respond remains explicitly unknown.
        """

        transport = self._require_transport()
        for motor_id in range(1, 8):
            transport.enable_feedback(motor_id)

        deadline = time.monotonic() + float(self.config.enable_feedback_timeout_s)
        while time.monotonic() < deadline:
            feedback = transport.feedback()
            if all(
                motor_id in feedback
                and time.time() - float(feedback[motor_id].timestamp_s) <= float(self.config.feedback_max_age_s)
                for motor_id in range(1, 8)
            ):
                break
            time.sleep(0.03)

        feedback = transport.feedback()
        joint_enabled: Dict[str, Optional[bool]] = {}
        joint_modes: Dict[str, Optional[str]] = {}
        fault_codes: Dict[str, Optional[int]] = {}
        unknown_joints = []
        mode_names = {
            int(MotorModeState.RESET): "reset",
            int(MotorModeState.CALIBRATION): "calibration",
            int(MotorModeState.MOTOR): "motor",
        }
        now = time.time()
        for motor_id in range(1, 8):
            item = feedback.get(motor_id)
            key = str(motor_id)
            if item is None or now - float(item.timestamp_s) > float(self.config.feedback_max_age_s):
                joint_enabled[key] = None
                joint_modes[key] = None
                fault_codes[key] = None
                unknown_joints.append(motor_id)
                continue
            mode_state = int(item.mode_state)
            joint_enabled[key] = mode_state == int(MotorModeState.MOTOR)
            joint_modes[key] = mode_names.get(mode_state, "unknown(%d)" % mode_state)
            fault_codes[key] = int(item.fault_code)

        known_states = list(joint_enabled.values())
        all_enabled: Optional[bool]
        if any(enabled is False for enabled in known_states):
            all_enabled = False
        elif any(enabled is None for enabled in known_states):
            all_enabled = None
        else:
            all_enabled = True
        # The arm body (joints 1--6) and the gripper (joint 7) are reported
        # separately: an overheated gripper drops out of Motor mode while the
        # arm body keeps working and must remain usable.
        arm_states = [joint_enabled[str(motor_id)] for motor_id in range(1, 7)]
        arm_enabled: Optional[bool]
        if any(state is False for state in arm_states):
            arm_enabled = False
        elif any(state is None for state in arm_states):
            arm_enabled = None
        else:
            arm_enabled = True
        gripper_enabled = joint_enabled["7"]
        reason = None
        if unknown_joints:
            reason = "cannot determine enable state: missing or stale feedback for joint(s) %s" % ", ".join(
                str(motor_id) for motor_id in unknown_joints
            )
        elif all_enabled is False:
            reason = "one or more joints are not in Motor mode"

        return {
            "all_enabled": all_enabled,
            "arm_enabled": arm_enabled,
            "gripper_enabled": gripper_enabled,
            "can_fps": transport.can_fps(),
            "fault_codes": fault_codes,
            "feedback_motor_ids": sorted(feedback.keys()),
            "joint_enabled": joint_enabled,
            "joint_modes": joint_modes,
            "reason": reason,
        }

    def _is_enabled(self) -> bool:
        status = self._enable_status()
        arm_enabled = status["arm_enabled"]
        if arm_enabled is None:
            self._enabled = False
            self._gripper_enabled = False
            raise RuntimeError("cannot determine physical arm enable state: %s" % status["reason"])
        self._enabled = bool(arm_enabled)
        self._gripper_enabled = status["gripper_enabled"] is True
        if self._enabled and not self._control_running.is_set():
            # A previous owner process may have left the motors in Motor mode
            # after it exited. Adopt the arm by starting the local Type-1 hold
            # producer at the feedback-confirmed pose; an enabled arm must
            # never be reported ready without exactly one hold loop owning it.
            current_joint_pose = self._read_joint_pose()
            self._start_control_loop(current_joint_pose)
        return self._enabled

    def _require_transport(self) -> ArmTransport:
        if self._transport is None:
            raise RuntimeError("arm transport is not connected")
        return self._transport

    def _require_enabled(self) -> None:
        """Arm-body gate: joints 1--6 in Motor mode (tracked by enable/status)."""

        if not self._enabled:
            raise RuntimeError("arm is not enabled")

    def _require_gripper_enabled(self) -> None:
        if not self._gripper_enabled:
            raise RuntimeError("夹爪电机未使能（可能过热保护掉扭矩），请先冷却后再操作夹爪")

    def _check_not_interrupted(self) -> None:
        if self._interrupt.is_set():
            raise RuntimeError("arm motion interrupted by emergency stop")
