#!/usr/bin/env python3
"""通过 BookArm SDK 测试夹爪打开或闭合到指定角度。

从仓库根目录运行：
    python3 scripts/gripper/gripper_test.py open --angle 30 --confirm
    python3 scripts/gripper/gripper_test.py close --angle 80 --confirm

脚本会使能 1--7 号电机，但只向夹爪发送位置命令。退出时只关闭 CAN
通信，不会发送失能帧，也不会移动六轴机械臂到失能位。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Optional, Sequence


SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from bookarm.devices.arm import ArmDeviceConfig, ArmService
from bookarm.domain.plans.take_book import GripperCommand


def finite_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是数字") from error
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError("必须是有限数字")
    return result


def positive_float(value: str) -> float:
    result = finite_float(value)
    if result <= 0.0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return result


def integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是十进制或 0x 开头的整数") from error


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    defaults = GripperCommand()
    parser = argparse.ArgumentParser(
        description="通过机械臂 SDK 测试夹爪打开或闭合到指定角度。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("action", choices=("open", "close"), help="夹爪动作")
    parser.add_argument("--angle", type=finite_float, required=True, metavar="DEG", help="目标角度，单位度")
    parser.add_argument(
        "--speed-deg-s", type=positive_float, default=defaults.close_speed_deg_s,
        help="夹爪运动速度，单位度/秒",
    )
    parser.add_argument("--kp", type=positive_float, default=defaults.kp, help="位置增益")
    parser.add_argument("--kd", type=positive_float, default=defaults.kd, help="阻尼增益")
    parser.add_argument("--effort", type=finite_float, default=defaults.effort, help="闭合力矩前馈")
    parser.add_argument("--timeout", type=positive_float, default=defaults.timeout_s, help="动作超时，单位秒")
    parser.add_argument(
        "--tolerance-deg", type=positive_float, default=defaults.target_tolerance_deg,
        help="到位容差，单位度",
    )
    parser.add_argument("--backend", choices=("socketcan", "slcan"), default="socketcan")
    parser.add_argument("--can-interface", default="can0", help="SocketCAN 接口")
    parser.add_argument("--slcan-port", help="SLCAN 串口，例如 /dev/ttyUSB0")
    parser.add_argument("--slcan-baudrate", type=integer, default=2000000)
    parser.add_argument("--can-bitrate", type=integer, default=1000000)
    parser.add_argument("--host-can-id", type=integer, default=0xFD)
    parser.add_argument(
        "--confirm", action="store_true",
        help="确认允许使能实机并驱动夹爪",
    )
    return parser.parse_args(argv)


def make_command(args: argparse.Namespace) -> GripperCommand:
    defaults = GripperCommand()
    values = {
        "open_deg": defaults.open_deg,
        "close_deg": defaults.close_deg,
        "open_speed_deg_s": defaults.open_speed_deg_s,
        "close_speed_deg_s": defaults.close_speed_deg_s,
        "effort": float(args.effort),
        "kp": float(args.kp),
        "kd": float(args.kd),
        "timeout_s": float(args.timeout),
        "target_tolerance_deg": float(args.tolerance_deg),
    }
    if args.action == "open":
        values["open_deg"] = float(args.angle)
        values["open_speed_deg_s"] = float(args.speed_deg_s)
    else:
        values["close_deg"] = float(args.angle)
        values["close_speed_deg_s"] = float(args.speed_deg_s)
    return GripperCommand(**values)


def make_device_config(args: argparse.Namespace) -> ArmDeviceConfig:
    if args.backend == "slcan" and not args.slcan_port:
        raise ValueError("使用 --backend slcan 时必须提供 --slcan-port")
    return ArmDeviceConfig(
        backend=args.backend,
        can_interface=args.can_interface,
        slcan_port=args.slcan_port,
        slcan_baudrate=int(args.slcan_baudrate),
        can_bitrate=int(args.can_bitrate),
        host_can_id=int(args.host_can_id),
    )


def validate_target(angle_deg: float, config: ArmDeviceConfig) -> None:
    lower_rad, upper_rad = config.joint_limits[7]
    lower_deg, upper_deg = math.degrees(lower_rad), math.degrees(upper_rad)
    if not lower_deg <= float(angle_deg) <= upper_deg:
        raise ValueError(
            "夹爪目标角 %.2f° 超出 SDK 限位 [%.2f°, %.2f°]"
            % (float(angle_deg), lower_deg, upper_deg)
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.confirm:
        print("执行失败：该操作会使能实机并驱动夹爪，请加 --confirm", file=sys.stderr)
        return 2

    try:
        config = make_device_config(args)
        validate_target(args.angle, config)
        command = make_command(args)
    except ValueError as error:
        print("配置错误：%s" % error, file=sys.stderr)
        return 2

    arm = ArmService(config)
    started = False
    try:
        started = True
        print("正在连接机械臂：backend=%s, interface=%s" % (config.backend, config.can_interface))
        arm.start()
        print("正在使能 1--7 号电机并检查反馈……")
        arm.enable()

        if args.action == "open":
            arm.open_gripper(command.open_deg, command)
            print("[OK] 夹爪已打开到 %.2f°。" % command.open_deg)
            return 0

        result = arm.close_gripper_monitored(command)
        print("闭合监测结果：%s" % json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        reason = str(result.get("reason", "unknown"))
        if reason == "target_reached":
            print("[OK] 夹爪已闭合到 %.2f°。" % command.close_deg)
            return 0
        if reason == "object_contact":
            print("[OK] 夹爪检测到书本接触，已保持夹持位置。")
            return 0
        print("[FAIL] 夹爪闭合监测超时，未确认到位。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        arm.interrupt_motion()
        print("\n已中断夹爪动作；正在关闭通信，电机保持当前使能状态。", file=sys.stderr)
        return 130
    except Exception as error:
        print("[FAIL] gripper-%s: %s" % (args.action, error), file=sys.stderr)
        return 1
    finally:
        if started:
            try:
                arm.close()
                print("机械臂通信已关闭；未发送电机失能命令。")
            except Exception as error:
                print("关闭机械臂通信失败：%s" % error, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())


# 启动方式（从仓库根目录执行）：
#   打开到 30°：python3 scripts/gripper/gripper_test.py open --angle 30 --confirm
#   闭合到 80°：python3 scripts/gripper/gripper_test.py close --angle 80 --confirm
