#!/usr/bin/env python3
"""连接、查看状态、使能或失能 YZ-AIM 升降台驱动器。

从仓库根目录运行示例：
    # 仅读取连接和驱动状态，不改变设备状态。
    python3 scripts/lift/lift_control.py --port /dev/lift_port status

    # 连接后使能 Modbus 控制与伺服输出。
    python3 scripts/lift/lift_control.py --port /dev/lift_port enable --confirm

    # 失能伺服输出和 Modbus 控制。
    python3 scripts/lift/lift_control.py --port /dev/lift_port disable --confirm

连接只在脚本运行期间保持；``enable`` 写入的 AIM 使能状态会保留在驱动器中，
直到执行 ``disable``、断电或驱动器自身状态发生变化。本脚本不发送任何位置指令。
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from enum import IntEnum
import json
from pathlib import Path
import sys
from typing import Any, Sequence


# 支持在未安装依赖包的源码仓库中直接运行。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
AIM_SDK_SOURCE_ROOT = PROJECT_ROOT.parent.parent / "aim_sdk"
if str(AIM_SDK_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(AIM_SDK_SOURCE_ROOT))

from aim_sdk import AimDrive, LiftPlatform


def _positive_integer(value: str) -> int:
    try:
        number = int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _positive_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if number <= 0.0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect to one YZ-AIM lift and read, enable, or disable it; this script never moves the lift."
    )
    parser.add_argument("--port", default="/dev/lift_port", help="RS485 serial device path")
    parser.add_argument("--address", type=_positive_integer, default=1, help="Modbus drive address")
    parser.add_argument("--baudrate", type=_positive_integer, default=19200, help="current RS485 baud rate")
    parser.add_argument(
        "--serial-timeout-s", type=_positive_number, default=0.2,
        help="timeout for each Modbus request (default: %(default)s)",
    )
    parser.add_argument("--retries", type=int, default=1, help="Modbus retry count (default: %(default)s)")
    parser.add_argument(
        "--confirm", action="store_true",
        help="acknowledge that enable or disable changes the live drive state",
    )
    parser.add_argument(
        "action", nargs="?", choices=("status", "enable", "disable"), default="status",
        help="status is read-only; enable and disable require --confirm",
    )

    # Accept the confirmation after the action too, e.g. ``enable --confirm``.
    values = list(sys.argv[1:] if argv is None else argv)
    if "--confirm" in values:
        values = [item for item in values if item != "--confirm"]
        values.insert(0, "--confirm")
    return parser.parse_args(values)


def _json_value(value: Any) -> Any:
    if isinstance(value, IntEnum):
        return {"name": value.name, "value": int(value)}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _print_status(drive: AimDrive) -> None:
    """Report the state after the requested operation using only feedback reads."""
    lift = LiftPlatform(drive)
    status = asdict(drive.read_status())
    absolute_pulses = drive.read_absolute_position()
    status.update({
        "absolute_position_pulses": absolute_pulses,
        "calibrated_height_mm": float(lift.absolute_position_to_height(absolute_pulses)) * 10.0,
    })
    print(json.dumps(_json_value(status), ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.address <= 247:
        print("Lift control failed: --address must be in 1..247", file=sys.stderr)
        return 2
    if args.retries < 0:
        print("Lift control failed: --retries must not be negative", file=sys.stderr)
        return 2
    if args.action != "status" and not args.confirm:
        print("Lift control failed: %s changes drive state; repeat with --confirm" % args.action, file=sys.stderr)
        return 2

    drive: AimDrive | None = None
    try:
        print("Connecting to AIM lift on %s at %d baud..." % (args.port, args.baudrate))
        drive = AimDrive.connect(
            args.port,
            address=args.address,
            baudrate=args.baudrate,
            timeout_s=args.serial_timeout_s,
            retries=args.retries,
        )
        print("Connected.")
        if args.action == "enable":
            drive.enable_modbus()
            drive.enable_output()
            print("Modbus control and servo output enabled.")
        elif args.action == "disable":
            # Output is disabled first so the lift cannot move during teardown.
            drive.disable_output()
            drive.disable_modbus()
            print("Servo output and Modbus control disabled.")
        _print_status(drive)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Use the physical emergency stop if the lift is unsafe.", file=sys.stderr)
        return 130
    except Exception as error:
        print("Lift control failed: %s" % error, file=sys.stderr)
        return 1
    finally:
        if drive is not None:
            drive.close()
            print("Disconnected.")


if __name__ == "__main__":
    raise SystemExit(main())
