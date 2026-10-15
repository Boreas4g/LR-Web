#!/usr/bin/env python3
"""YZ-AIM 升降台实机调试与 SDK 功能验收工具。

从仓库根目录运行。默认仅读取状态，不会使能或移动升降台：

    python3 scripts/lift/lift_move.py --port /dev/lift_port

常用的低速绝对高度测试（BookArm 工作坐标统一使用毫米）：

    # 到达取书高度
    python3 scripts/lift/lift_move.py --port /dev/lift_port move-height \
        --height-mm 270 --speed-level 2 --timeout-s 45 --confirm

    # 到达顶部高度
        python3 scripts/lift/lift_move.py --port /dev/lift_port move-height \
            --height-mm 1600 --speed-level 2 --timeout-s 45 --confirm

    # 到达底部
    python3 scripts/lift/lift_move.py --port /dev/lift_port move-height \
            --height-mm 10 --speed-level 2 --timeout-s 45 --confirm

所有会改变驱动状态、参数、坐标系或机械位置的命令都要求 ``--confirm``。
机械找零、清除绝对坐标、设置电子齿轮、保存参数、设置开机角度和改波特率
不会被 ``all`` 之类的批量操作自动执行，因为它们可能改变标定或造成机械风险。
在上机前确认外部急停、行程、机械方向、负载和防护均处于安全状态。

本脚本覆盖 AIM SDK 中适用于单台升降台的已定义功能。SDK 对 0x7A 改地址
命令没有公开帧格式，故意不提供该测试入口；不应猜测该命令的数据格式。
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
from enum import IntEnum
import json
import math
from pathlib import Path
import sys
import time
from typing import Any, Sequence


# 支持在未安装依赖包的源码仓库中直接运行。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
AIM_SDK_SOURCE_ROOT = PROJECT_ROOT.parent.parent / "aim_sdk"
if str(AIM_SDK_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(AIM_SDK_SOURCE_ROOT))

from aim_sdk import AimDrive, LiftPlatform
from aim_sdk.models import BaudRate, SpecialFunction


MUTATING_COMMANDS = frozenset({
    "enable",
    "disable",
    "move-height",
    "move-absolute",
    "move-relative",
    "move-origin",
    "clear-position",
    "configure-motion",
    "set-direction",
    "set-weak-field",
    "set-electronic-gear",
    "set-stationary-output",
    "set-special",
    "home-mechanical",
    "home-switch",
    "set-powerup-angle",
    "set-host-baudrate",
    "save-parameters",
    "prepare-baudrate",
    "raw-write",
})

SPECIAL_FUNCTIONS = {
    "pulse-direction": SpecialFunction.PULSE_DIRECTION,
    "encoder-follow": SpecialFunction.ENCODER_FOLLOW,
    "pwm-speed": SpecialFunction.PWM_SPEED,
    "zout-simulated-home": SpecialFunction.ZOUT_SIMULATED_HOME,
}


def _finite_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be finite")
    return number


def _positive_number(value: str) -> float:
    number = _finite_number(value)
    if number <= 0.0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer, optionally using 0x prefix") from error


def _positive_integer(value: str) -> int:
    number = _integer(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _uint16(value: str) -> int:
    number = _integer(value)
    if not 0 <= number <= 0xFFFF:
        raise argparse.ArgumentTypeError("must be in 0..65535")
    return number


def _pulse_i32(value: str) -> int:
    number = _integer(value)
    if not -(1 << 31) <= number <= (1 << 31) - 1:
        raise argparse.ArgumentTypeError("must fit a signed 32-bit position")
    return number


def _speed_level(value: str) -> int:
    level = _integer(value)
    if level not in LiftPlatform.SPEED_PROFILES:
        raise argparse.ArgumentTypeError("must be one of 1, 2, or 3")
    return level


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Direct YZ-AIM lift SDK commissioning tool; no command defaults to read-only status."
    )
    parser.add_argument("--port", default="/dev/lift_port", help="RS485 serial device path")
    parser.add_argument("--address", type=_positive_integer, default=1, help="Modbus drive address")
    parser.add_argument("--baudrate", type=_positive_integer, default=19200, help="current RS485 baud rate")
    parser.add_argument(
        "--serial-timeout-s", type=_positive_number, default=0.2,
        help="timeout for each Modbus request (default: %(default)s)",
    )
    parser.add_argument("--retries", type=_integer, default=1, help="Modbus retry count (default: %(default)s)")
    parser.add_argument(
        "--confirm", action="store_true",
        help="acknowledge that the requested command can change live hardware or configuration",
    )
    parser.add_argument(
        "--leave-output-enabled", action="store_true",
        help="do not disable output/Modbus on exit when this script enabled them",
    )

    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("status", help="read drive status, target pulse and calibrated height")

    calibration = commands.add_parser("calibration", help="read AIM feedback and show height/pulse calibration")
    calibration.add_argument("--height-mm", type=_finite_number, help="also convert this calibrated height to pulses")

    monitor = commands.add_parser("monitor", help="stream read-only feedback until Ctrl-C or count is reached")
    monitor.add_argument("--interval-s", type=_positive_number, default=0.5)
    monitor.add_argument("--count", type=_positive_integer, default=20)

    wait_position = commands.add_parser("wait-position", help="wait read-only until absolute feedback reaches a pulse target")
    wait_position.add_argument("--pulses", type=_pulse_i32, required=True)
    wait_position.add_argument("--timeout-s", type=_positive_number, default=10.0)
    wait_position.add_argument("--tolerance-pulses", type=_uint16, default=10)

    commands.add_parser("enable", help="enable Modbus control and servo output")
    disable = commands.add_parser("disable", help="disable servo output, then Modbus control")
    disable.add_argument("--keep-modbus", action="store_true", help="only disable servo output")
    commands.add_parser("emergency-stop", help="send AIM software emergency-stop sequence")

    move_height = commands.add_parser("move-height", help="move by calibrated height through LiftPlatform")
    move_height.add_argument("--height-mm", type=_finite_number, required=True, help="AIM calibrated height, 10..1700 mm")
    move_height.add_argument("--speed-level", type=_speed_level, default=1, help="AIM profile: 1=slow, 2=medium, 3=fast")
    move_height.add_argument("--timeout-s", type=_positive_number, default=45.0)
    move_height.add_argument("--tolerance-pulses", type=_uint16, default=10)

    move_absolute = commands.add_parser("move-absolute", help="move to a raw absolute pulse position")
    move_absolute.add_argument("--pulses", type=_pulse_i32, required=True)
    move_absolute.add_argument("--wait", action="store_true", help="wait for absolute feedback")
    move_absolute.add_argument("--timeout-s", type=_positive_number, default=10.0)
    move_absolute.add_argument("--tolerance-pulses", type=_uint16, default=10)
    move_absolute.add_argument(
        "--allow-zero-clear", action="store_true",
        help="permit raw zero when the AIM gear numerator is zero; this clears the coordinate system",
    )

    move_relative = commands.add_parser("move-relative", help="move by a signed raw pulse increment")
    move_relative.add_argument("--pulses", type=_pulse_i32, required=True)
    move_relative.add_argument("--method", choices=("register", "vendor"), default="register")

    move_origin = commands.add_parser("move-origin", help="move to AIM communication-mode origin pulse 1")
    move_origin.add_argument("--wait", action="store_true")
    move_origin.add_argument("--timeout-s", type=_positive_number, default=10.0)
    commands.add_parser("clear-position", help="clear AIM absolute coordinate system to pulse zero")

    configure_motion = commands.add_parser("configure-motion", help="write documented motion/controller settings")
    configure_motion.add_argument("--max-speed-rpm", type=_uint16, required=True)
    configure_motion.add_argument("--acceleration-rpm-s", type=_uint16, required=True)
    configure_motion.add_argument("--position-kp", type=_uint16)
    configure_motion.add_argument("--speed-kp", type=_finite_number)
    configure_motion.add_argument("--speed-integral-ms", type=_positive_integer)
    configure_motion.add_argument("--speed-feedforward-v-per-krpm", type=_finite_number)

    weak_field = commands.add_parser("set-weak-field", help="write documented raw weak-field-angle value")
    weak_field.add_argument("--raw", type=_uint16, required=True)

    direction = commands.add_parser("set-direction", help="set external DIR polarity")
    direction.add_argument("--clockwise-when-active", action=argparse.BooleanOptionalAction, required=True)

    gear = commands.add_parser("set-electronic-gear", help="set external pulse electronic gear ratio")
    gear.add_argument("--numerator", type=_uint16, required=True)
    gear.add_argument("--denominator", type=_positive_integer, required=True)

    stationary = commands.add_parser("set-stationary-output", help="write raw stationary maximum-output register")
    stationary.add_argument("--raw", type=_uint16, required=True)

    special = commands.add_parser("set-special", help="set a non-homing special function")
    special.add_argument("--mode", choices=tuple(SPECIAL_FUNCTIONS), required=True)
    special.add_argument("--save", action="store_true", help="persist the special function")

    home_mechanical = commands.add_parser("home-mechanical", help="start mechanical-stall homing; ensure safe travel")
    home_mechanical.add_argument("--to-encoder-zero", action="store_true")
    home_mechanical.add_argument("--save", action="store_true")
    home_switch = commands.add_parser("home-switch", help="start EN-input home-switch homing")
    home_switch.add_argument("--save", action="store_true")

    powerup = commands.add_parser("set-powerup-angle", help="configure a one-turn power-up positioning angle")
    powerup.add_argument("--pulses", type=_positive_integer, required=True)
    powerup.add_argument("--save", action="store_true", help="persist setting; SDK default is deliberately overridden to false")

    save = commands.add_parser("save-parameters", help="persist current drive parameters")
    save.add_argument("--timeout-s", type=_positive_number, default=2.0)

    baud = commands.add_parser("prepare-baudrate", help="stage AIM baud-rate change; power-cycle before reconnecting")
    baud.add_argument("--new-baudrate", type=int, choices=tuple(int(item) for item in BaudRate), required=True)

    host_baud = commands.add_parser("set-host-baudrate", help="change this process's serial handle baud rate; no drive write")
    host_baud.add_argument("--new-baudrate", type=int, choices=tuple(int(item) for item in BaudRate), required=True)

    raw_read = commands.add_parser("raw-read", help="read one or more raw 16-bit holding registers")
    raw_read.add_argument("--start-register", type=_uint16, required=True)
    raw_read.add_argument("--count", type=_positive_integer, default=1)

    raw_write = commands.add_parser("raw-write", help="write raw 16-bit holding registers; use only with manual-defined values")
    raw_write.add_argument("--start-register", type=_uint16, required=True)
    raw_write.add_argument("--values", type=_uint16, nargs="+", required=True)
    # People naturally place the safety acknowledgement after the subcommand.
    # Normalize that form so both ``--confirm move-height`` and
    # ``move-height --confirm`` are accepted by argparse's global option.
    values = list(sys.argv[1:] if argv is None else argv)
    if "--confirm" in values:
        values = [item for item in values if item != "--confirm"]
        values.insert(0, "--confirm")
    return parser.parse_args(values)


class AimSession:
    """Track only enable-state changes initiated by this short-lived script."""

    def __init__(self, drive: AimDrive, leave_output_enabled: bool) -> None:
        self.drive = drive
        self.leave_output_enabled = leave_output_enabled
        self.enabled_output = False
        self.enabled_modbus = False

    def ensure_modbus(self) -> None:
        if not self.drive.is_modbus_enabled():
            self.drive.enable_modbus()
            self.enabled_modbus = True

    def ensure_motion_ready(self) -> None:
        self.ensure_modbus()
        if not self.drive.is_output_enabled():
            self.drive.enable_output()
            self.enabled_output = True
        self.drive.assert_motion_ready()

    def enable(self) -> None:
        self.ensure_motion_ready()

    def disable(self, keep_modbus: bool = False) -> None:
        self.drive.disable_output()
        self.enabled_output = False
        if not keep_modbus:
            self.drive.disable_modbus()
            self.enabled_modbus = False

    def close(self) -> None:
        try:
            if not self.leave_output_enabled:
                try:
                    if self.enabled_output:
                        self.drive.disable_output()
                finally:
                    if self.enabled_modbus:
                        self.drive.disable_modbus()
        finally:
            self.drive.close()


def _json_value(value: Any) -> Any:
    if isinstance(value, IntEnum):
        return {"name": value.name, "value": int(value)}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _report_status(drive: AimDrive, lift: LiftPlatform) -> None:
    status = asdict(drive.read_status())
    status["target_position_pulses"] = drive.read_target_position()
    status["electronic_gear_numerator"] = drive.electronic_gear_numerator()
    status["electronic_gear_denominator"] = drive.electronic_gear_denominator()
    status["stationary_max_output_raw"] = drive.read_stationary_max_output_raw()
    status["calibrated_height_cm"] = lift.read_height_cm()
    status["calibrated_height_mm"] = float(lift.read_height_cm()) * 10.0
    print(json.dumps(_json_value(status), ensure_ascii=False, indent=2, sort_keys=True))


def _require_confirmation(args: argparse.Namespace) -> None:
    if args.command in MUTATING_COMMANDS and not args.confirm:
        raise RuntimeError("%s changes live hardware or configuration; repeat with --confirm" % args.command)


def _run_command(args: argparse.Namespace, session: AimSession, lift: LiftPlatform) -> None:
    drive = session.drive
    command = args.command or "status"
    if command == "status":
        _report_status(drive, lift)
    elif command == "calibration":
        feedback_pulses = drive.read_absolute_position()
        payload: dict[str, Any] = {
            "feedback_pulses": feedback_pulses,
            "feedback_height_cm": lift.absolute_position_to_height(feedback_pulses),
            "feedback_height_mm": float(lift.absolute_position_to_height(feedback_pulses)) * 10.0,
            "minimum_height_mm": float(lift.MIN_HEIGHT_CM) * 10.0,
            "maximum_height_mm": float(lift.MAX_HEIGHT_CM) * 10.0,
        }
        if args.height_mm is not None:
            target_cm = args.height_mm / 10.0
            pulses = lift.height_to_absolute_position(target_cm)
            payload["requested_height_mm"] = args.height_mm
            payload["requested_target_pulses"] = pulses
            payload["round_trip_height_mm"] = float(lift.absolute_position_to_height(pulses)) * 10.0
        print(json.dumps(_json_value(payload), ensure_ascii=False, indent=2, sort_keys=True))
    elif command == "monitor":
        for index in range(args.count):
            print("--- sample %d/%d ---" % (index + 1, args.count))
            _report_status(drive, lift)
            if index + 1 < args.count:
                time.sleep(args.interval_s)
    elif command == "wait-position":
        actual = drive.wait_until_at_position(
            args.pulses,
            timeout_s=args.timeout_s,
            tolerance_pulses=args.tolerance_pulses,
        )
        print("Position reached: %d pulses." % actual)
        _report_status(drive, lift)
    elif command == "enable":
        session.enable()
        _report_status(drive, lift)
    elif command == "disable":
        session.disable(keep_modbus=args.keep_modbus)
        _report_status(drive, lift)
    elif command == "emergency-stop":
        drive.emergency_stop()
        print("AIM software emergency-stop sequence sent. Use the physical E-stop for a safety-rated stop.")
        _report_status(drive, lift)
    elif command == "move-height":
        session.ensure_motion_ready()
        target_cm = args.height_mm / 10.0
        target_pulses = lift.move_to_height(
            target_cm,
            speed=args.speed_level,
            wait=True,
            timeout_s=args.timeout_s,
            tolerance_pulses=args.tolerance_pulses,
        )
        print("Reached %.3f mm (AIM target %d pulses)." % (float(lift.read_height_cm()) * 10.0, target_pulses))
        _report_status(drive, lift)
    elif command == "move-absolute":
        session.ensure_motion_ready()
        drive.move_absolute(
            args.pulses,
            wait=args.wait,
            timeout_s=args.timeout_s,
            tolerance_pulses=args.tolerance_pulses,
            allow_zero_clear=args.allow_zero_clear,
        )
        _report_status(drive, lift)
    elif command == "move-relative":
        session.ensure_motion_ready()
        if args.method == "vendor":
            drive.move_relative_vendor(args.pulses)
        else:
            drive.move_relative(args.pulses)
        print("Relative command sent; inspect feedback with the status command.")
        _report_status(drive, lift)
    elif command == "move-origin":
        session.ensure_motion_ready()
        drive.move_to_origin(wait=args.wait, timeout_s=args.timeout_s)
        _report_status(drive, lift)
    elif command == "clear-position":
        drive.clear_absolute_position(acknowledge=True)
        _report_status(drive, lift)
    elif command == "configure-motion":
        session.ensure_modbus()
        configuration = drive.configure_motion(
            max_speed_rpm=args.max_speed_rpm,
            acceleration_rpm_s=args.acceleration_rpm_s,
            position_kp=args.position_kp,
            speed_kp=args.speed_kp,
            speed_integral_ms=args.speed_integral_ms,
            speed_feedforward_v_per_krpm=args.speed_feedforward_v_per_krpm,
        )
        print(json.dumps(_json_value(asdict(configuration)), ensure_ascii=False, indent=2, sort_keys=True))
    elif command == "set-weak-field":
        session.ensure_modbus()
        drive.set_weak_field_angle_raw(args.raw)
        print("Weak-field raw value written: %d" % args.raw)
    elif command == "set-direction":
        session.ensure_modbus()
        drive.set_direction_polarity(args.clockwise_when_active)
        print("Direction polarity written: clockwise_when_dir_active=%s" % args.clockwise_when_active)
    elif command == "set-electronic-gear":
        session.ensure_modbus()
        drive.set_electronic_gear(args.numerator, args.denominator)
        print("Electronic gear written: %d/%d" % (args.numerator, args.denominator))
    elif command == "set-stationary-output":
        session.ensure_modbus()
        drive.set_stationary_max_output_raw(args.raw)
        print("Stationary maximum output raw value: %d" % drive.read_stationary_max_output_raw())
    elif command == "set-special":
        session.ensure_modbus()
        drive.set_special_function(SPECIAL_FUNCTIONS[args.mode], save=args.save)
        print("Special function set to %s; persisted=%s" % (args.mode, args.save))
    elif command == "home-mechanical":
        session.ensure_motion_ready()
        drive.home_mechanical(to_encoder_zero=args.to_encoder_zero, save=args.save)
        print("Mechanical homing started; it may stall the motor by design. Monitor physical motion.")
    elif command == "home-switch":
        session.ensure_motion_ready()
        drive.home_switch(save=args.save)
        print("Home-switch homing started. Monitor physical motion.")
    elif command == "set-powerup-angle":
        session.ensure_modbus()
        drive.set_powerup_angle(args.pulses, save=args.save)
        print("Power-up angle set to %d pulses; persisted=%s" % (args.pulses, args.save))
    elif command == "save-parameters":
        session.ensure_modbus()
        drive.save_parameters(timeout_s=args.timeout_s)
        print("Drive parameters saved.")
    elif command == "prepare-baudrate":
        session.ensure_modbus()
        new_baudrate = drive.configure_baud_rate(BaudRate(args.new_baudrate))
        print(
            "AIM baud-rate change to %d staged. Power-cycle the drive, then reconnect with --baudrate %d."
            % (new_baudrate, new_baudrate)
        )
    elif command == "set-host-baudrate":
        drive.set_host_baud_rate(BaudRate(args.new_baudrate))
        print("Host serial handle baud rate set to %d; no drive register was written." % args.new_baudrate)
    elif command == "raw-read":
        values = drive.read_registers(args.start_register, args.count)
        print(json.dumps({"start_register": args.start_register, "values": values}, indent=2))
    elif command == "raw-write":
        session.ensure_modbus()
        drive.write_registers(args.start_register, args.values)
        print(json.dumps({"start_register": args.start_register, "written_values": args.values}, indent=2))
    else:
        raise RuntimeError("unsupported command: %s" % command)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.address <= 247:
        print("Lift test failed: --address must be in 1..247", file=sys.stderr)
        return 2
    if args.retries < 0:
        print("Lift test failed: --retries must not be negative", file=sys.stderr)
        return 2
    args.command = args.command or "status"
    try:
        _require_confirmation(args)
        drive = AimDrive.connect(
            args.port,
            address=args.address,
            baudrate=args.baudrate,
            timeout_s=args.serial_timeout_s,
            retries=args.retries,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print("Lift test connection failed: %s" % error, file=sys.stderr)
        return 1

    session = AimSession(drive, leave_output_enabled=args.leave_output_enabled)
    try:
        _run_command(args, session, LiftPlatform(drive))
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Use the physical emergency stop if motion is unsafe.", file=sys.stderr)
        return 130
    except Exception as error:
        print("Lift test failed: %s" % error, file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
