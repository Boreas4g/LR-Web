#!/usr/bin/env python3
"""在实机上测试 SDK 内置的零位、取书图书调试位和失能前位。

从仓库根目录运行：
    python3 scripts/arm/arm_builtin_pose_test.py zero --can-interface can0 --confirm
    python3 scripts/arm/arm_builtin_pose_test.py book-debug --can-interface can0 --duration 2.5 --confirm
    python3 scripts/arm/arm_builtin_pose_test.py disable-pose --can-interface can0 --confirm

脚本绝不会自动失能电机。只要本次运行尝试过使能机械臂，退出前会在终端询问；
只有获得明确确认后才会发送失能命令。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any, Optional, Sequence


# 支持在未安装 bookarm 包的源码仓库中直接运行。
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from bookarm.devices.arm import ArmDeviceConfig, ArmService


def positive_float(value: str) -> float:
    """Parse a finite command-line value greater than zero."""

    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise argparse.ArgumentTypeError("must be a finite value greater than zero")
    return parsed


def integer(value: str) -> int:
    """Parse decimal or 0x-prefixed integer values."""

    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer, optionally using 0x prefix") from error


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="测试 EL-A3 SDK 内置的零位、取书图书调试位和失能前位。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("target", choices=("zero", "book-debug", "disable-pose"), help="要测试的内置关节位姿")
    parser.add_argument("--config", type=Path, help="可选 ArmDeviceConfig JSON；命令行参数优先")
    parser.add_argument("--backend", choices=("socketcan", "slcan"), help="机械臂通信后端")
    parser.add_argument("--can-interface", help="SocketCAN 接口；未设 --slcan-port 时也作为 SLCAN 串口")
    parser.add_argument("--slcan-port", help="SLCAN 串口，例如 /dev/ttyUSB0")
    parser.add_argument("--slcan-baudrate", type=integer, help="SLCAN 串口波特率")
    parser.add_argument("--can-bitrate", type=integer, help="SLCAN 使用的 CAN 波特率")
    parser.add_argument("--host-can-id", type=integer, help="主机 CAN ID，支持十进制或 0x 前缀")
    parser.add_argument("--control-rate-hz", type=positive_float, help="MoveJ 控制频率")
    parser.add_argument("--feedback-max-age", type=positive_float, help="最大反馈时效，单位秒")
    parser.add_argument("--enable-feedback-timeout", type=positive_float, help="使能反馈超时，单位秒")
    parser.add_argument("--final-tolerance", type=positive_float, help="最终关节容差，单位 rad")
    parser.add_argument("--duration", type=positive_float, default=3.0, help="MoveJ 时长，单位秒")
    parser.add_argument("--confirm", action="store_true", help="确认允许实机使能并运动")
    return parser.parse_args(argv)


def _load_config(path: Optional[Path]) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError("cannot read config file %s: %s" % (path, error)) from error
    except json.JSONDecodeError as error:
        raise ValueError("invalid JSON config %s: %s" % (path, error)) from error
    if not isinstance(payload, dict):
        raise ValueError("arm config must be a JSON object")
    unknown = sorted(set(payload) - set(ArmDeviceConfig.__dataclass_fields__))
    if unknown:
        raise ValueError("unsupported arm config fields: %s" % ", ".join(unknown))
    return dict(payload)


def build_device_config(args: argparse.Namespace) -> ArmDeviceConfig:
    """Merge an optional JSON config with explicit connection arguments."""

    values = _load_config(args.config)
    option_to_field = {
        "backend": "backend",
        "can_interface": "can_interface",
        "slcan_port": "slcan_port",
        "slcan_baudrate": "slcan_baudrate",
        "can_bitrate": "can_bitrate",
        "host_can_id": "host_can_id",
        "control_rate_hz": "control_rate_hz",
        "feedback_max_age": "feedback_max_age_s",
        "enable_feedback_timeout": "enable_feedback_timeout_s",
        "final_tolerance": "final_tolerance_rad",
    }
    for option, field in option_to_field.items():
        value = getattr(args, option)
        if value is not None:
            values[field] = value
    if "urdf_path" in values:
        values["urdf_path"] = Path(values["urdf_path"])
    if "tcp_offset" in values:
        values["tcp_offset"] = tuple(float(value) for value in values["tcp_offset"])
    if "joint_directions" in values:
        values["joint_directions"] = {int(key): float(value) for key, value in values["joint_directions"].items()}
    if "joint_offsets" in values:
        values["joint_offsets"] = {int(key): float(value) for key, value in values["joint_offsets"].items()}
    if "gravity_joint_scale" in values:
        values["gravity_joint_scale"] = {
            int(key): float(value) for key, value in values["gravity_joint_scale"].items()
        }
    if "joint_limits" in values:
        values["joint_limits"] = {
            int(key): tuple(float(limit) for limit in limits)
            for key, limits in values["joint_limits"].items()
        }
    try:
        return ArmDeviceConfig(**values)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid arm config: %s" % error) from error


def confirm_disable() -> bool:
    """Request an explicit terminal confirmation before disabling the motors."""

    if not sys.stdin.isatty():
        print("非交互终端无法确认失能；机械臂保持当前使能状态。", file=sys.stderr)
        return False
    try:
        reply = input("本次测试已尝试使能机械臂。是否在退出前失能？[y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print("\n未收到失能确认；机械臂保持当前使能状态。", file=sys.stderr)
        return False
    return reply.strip().lower() in {"y", "yes", "是"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.confirm:
        print("执行失败: 此脚本会使能并移动实机；请在确认现场安全后加 --confirm。", file=sys.stderr)
        return 2
    try:
        config = build_device_config(args)
    except ValueError as error:
        print("配置错误: %s" % error, file=sys.stderr)
        return 2

    arm = ArmService(config)
    started = False
    enable_attempted = False
    result = 0
    try:
        started = True
        arm.start()
        print("[OK] 机械臂通信已连接。")

        enable_attempted = True
        arm.enable()
        print("[OK] 1--7 号电机已使能，反馈正常。")

        if args.target == "zero":
            arm.move_to_zero(args.duration)
            label = "零位"
        elif args.target == "book-debug":
            arm.move_to_book_debug(args.duration)
            label = "取书图书调试位"
        else:
            arm.move_to_disable_pose(args.duration)
            label = "失能前位"
        values = arm.read_joint_pose().values_rad
        print("[OK] 已 MoveJ 到%s，当前关节角 (rad):" % label, " ".join("%.6f" % value for value in values))
    except KeyboardInterrupt:
        arm.interrupt_motion()
        result = 130
        print("\n测试被用户中断。", file=sys.stderr)
    except (RuntimeError, TimeoutError, ValueError, OSError) as error:
        result = 1
        print("测试失败: %s" % error, file=sys.stderr)
    finally:
        if enable_attempted:
            if confirm_disable():
                try:
                    result_data = arm.disable_at_safe_pose()
                    print("[OK] 机械臂已到达失能位并发送失能命令：%s" % json.dumps(result_data, ensure_ascii=False))
                except (RuntimeError, OSError) as error:
                    result = 1
                    print("失能命令发送失败: %s" % error, file=sys.stderr)
            else:
                print("未发送失能命令；关闭通信后电机保持当前使能状态。")
        if started:
            try:
                arm.close()
                print("机械臂通信已关闭。")
            except Exception as error:
                result = 1
                print("关闭机械臂通信失败: %s" % error, file=sys.stderr)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
