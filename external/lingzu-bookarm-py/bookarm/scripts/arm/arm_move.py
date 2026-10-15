#!/usr/bin/env python3
"""BookArm 机械臂 SDK 命令行测试工具。

每次运行独占一次机械臂连接。若本次运行执行过使能，退出前会在终端询问是否
失能；未确认时只关闭通信，不会改变电机使能状态。会使能电机或带动机械臂的
命令必须提供 ``--confirm``，以避免误操作实机状态。

从仓库根目录运行示例：
    # 关节读取检测
        python3 scripts/arm/arm_move.py read --can-interface can0 --confirm  
    # 不使能，只检测电机模式
        python3 scripts/arm/arm_move.py is-enabled --can-interface can0  
    # 机械臂movej归零测试
        python3 scripts/arm/arm_move.py move-j  --duration 3 --confirm
    # 机械臂movej逆解运动测试
        python3 scripts/arm/arm_move.py move-j --can-interface can0 --joints 0 0 0 0 0 0  --duration 3 --confirm
    # 机械臂movel
        python3 scripts/arm/arm_move.py move-l --pose -0.12 1.69 0.19 1.57 0 1.57 --duration 3 --confirm
    # 受控失能：到达失能位并由反馈确认后才失能
    python3 scripts/arm/arm_move.py disable --can-interface can0 --confirm
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any, Callable, Dict, Optional, Sequence


# 支持在未安装 BookArm 包的源码仓库中直接运行。
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from bookarm.devices.arm import ArmDeviceConfig, ArmService
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.domain.plans.take_book import GripperCommand


def finite_float(value: str) -> float:
    """Parse a finite floating-point command-line value."""

    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    return parsed


def positive_float(value: str) -> float:
    """Parse a finite value greater than zero."""

    parsed = finite_float(value)
    if parsed <= 0.0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def integer(value: str) -> int:
    """Parse decimal or 0x-prefixed integer values such as a CAN host ID."""

    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer, optionally using 0x prefix") from error


def _connection_arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--config",
        type=Path,
        help="可选 ArmDeviceConfig JSON；命令行参数优先",
    )
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
    parser.add_argument("--urdf-path", type=Path, help="位姿和直线运动使用的 URDF")
    parser.add_argument(
        "--tcp-offset",
        type=finite_float,
        nargs=6,
        metavar=("X", "Y", "Z", "RX", "RY", "RZ"),
        help="TCP 偏移，单位 m 和 rad；用于位姿和直线运动",
    )
    return parser


def _confirmation_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="确认允许改变实机使能状态或运动",
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    connection = _connection_arguments()
    parser = argparse.ArgumentParser(
        description="通过 BookArm arm SDK 测试 EL-A3 六轴机械臂和夹爪。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", required=True, title="子命令")

    health = subcommands.add_parser(
        "health", parents=[connection], help="查询通信和反馈健康状态，不使能电机"
    )
    health.add_argument(
        "--enable",
        action="store_true",
        help="查询前使能电机；同时需要 --confirm",
    )
    _confirmation_argument(health)

    subcommands.add_parser(
        "is-enabled", parents=[connection],
        help="不使能电机；请求反馈并检查 1--7 轴是否处于 Motor 模式",
    )

    read = subcommands.add_parser(
        "read", parents=[connection], help="使能后读取六轴关节角"
    )
    _confirmation_argument(read)

    enable = subcommands.add_parser(
        "enable", parents=[connection], help="测试电机使能和健康反馈"
    )
    _confirmation_argument(enable)

    move_j = subcommands.add_parser(
        "move-j", parents=[connection], help="执行六轴关节空间运动"
    )
    move_j.add_argument(
        "--joints", type=finite_float, nargs=6, required=True,
        metavar=("J1", "J2", "J3", "J4", "J5", "J6"), help="目标关节角，单位 rad",
    )
    move_j.add_argument("--duration", type=positive_float, required=True, help="运动时长，单位秒")
    _confirmation_argument(move_j)

    move_pose = subcommands.add_parser(
        "move-pose", parents=[connection], help="逆解后执行笛卡尔位姿点到点运动"
    )
    move_pose.add_argument(
        "--pose", type=finite_float, nargs=6, required=True,
        metavar=("X", "Y", "Z", "RX", "RY", "RZ"), help="目标基座位姿，单位 m 和 rad",
    )
    move_pose.add_argument("--duration", type=positive_float, required=True, help="运动时长，单位秒")
    _confirmation_argument(move_pose)

    move_p_by_j = subcommands.add_parser(
        "move-p-by-j", parents=[connection], help="逆解后通过 MoveJ 执行目标末端位姿运动"
    )
    move_p_by_j.add_argument(
        "--pose", type=finite_float, nargs=6, required=True,
        metavar=("X", "Y", "Z", "RX", "RY", "RZ"), help="目标基座位姿，单位 m 和 rad",
    )
    move_p_by_j.add_argument("--duration", type=positive_float, required=True, help="运动时长，单位秒")
    _confirmation_argument(move_p_by_j)

    move_l = subcommands.add_parser(
        "move-l", parents=[connection], help="执行笛卡尔直线运动"
    )
    move_l.add_argument(
        "--pose", type=finite_float, nargs=6, required=True,
        metavar=("X", "Y", "Z", "RX", "RY", "RZ"), help="目标基座位姿，单位 m 和 rad",
    )
    move_l.add_argument("--duration", type=positive_float, required=True, help="运动时长，单位秒")
    _confirmation_argument(move_l)

    gripper_options = argparse.ArgumentParser(add_help=False)
    gripper_options.add_argument("--angle", type=finite_float, help="目标夹爪角，单位度")
    gripper_options.add_argument("--kp", type=positive_float, default=30.0, help="夹爪位置增益")
    gripper_options.add_argument("--kd", type=positive_float, default=2.0, help="夹爪阻尼增益")
    gripper_options.add_argument("--effort", type=finite_float, default=0.50, help="夹爪力矩前馈")
    gripper_options.add_argument("--timeout", type=positive_float, default=10.0, help="夹爪闭合到位超时")
    gripper_options.add_argument(
        "--tolerance-deg", type=positive_float, default=2.0, help="夹爪闭合到位容差，单位度"
    )

    gripper_open = subcommands.add_parser(
        "gripper-open", parents=[connection, gripper_options], help="打开夹爪（默认目标为 0 度）"
    )
    _confirmation_argument(gripper_open)

    gripper_close = subcommands.add_parser(
        "gripper-close", parents=[connection, gripper_options],
        help="关闭夹爪并等待反馈到位（默认目标为 108.5 度）",
    )
    _confirmation_argument(gripper_close)

    disable = subcommands.add_parser(
        "disable", parents=[connection], help="到失能位并确认到位后，向 1--7 号电机发送失能命令"
    )
    _confirmation_argument(disable)
    return parser.parse_args(argv)


def _load_config(path: Optional[Path]) -> Dict[str, Any]:
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
    allowed = set(ArmDeviceConfig.__dataclass_fields__)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("unsupported arm config fields: %s" % ", ".join(unknown))
    return dict(payload)


def build_device_config(args: argparse.Namespace) -> ArmDeviceConfig:
    """Merge optional JSON configuration and connection command-line options."""

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
        "urdf_path": "urdf_path",
        "tcp_offset": "tcp_offset",
    }
    for option, field in option_to_field.items():
        value = getattr(args, option, None)
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


def _require_confirmation(args: argparse.Namespace) -> None:
    if not getattr(args, "confirm", False):
        raise ValueError("this command can change motor state or move the real arm; rerun it with --confirm")


def _command_requires_confirmation(args: argparse.Namespace) -> bool:
    return args.command not in {"health", "is-enabled"} or bool(getattr(args, "enable", False))


def _print_health(arm: ArmService) -> None:
    print(json.dumps(arm.health(), ensure_ascii=False, indent=2, sort_keys=True))


def _make_gripper_command(args: argparse.Namespace, *, closing: bool) -> GripperCommand:
    angle = args.angle if args.angle is not None else (108.5 if closing else 0.0)
    return GripperCommand(
        open_deg=float(angle) if not closing else 0.0,
        close_deg=float(angle) if closing else 108.5,
        effort=float(args.effort),
        kp=float(args.kp),
        kd=float(args.kd),
        timeout_s=float(args.timeout),
        target_tolerance_deg=float(args.tolerance_deg),
    )


def _run_command(args: argparse.Namespace, arm: ArmService, enable_arm: Callable[[], None]) -> None:
    if args.command == "health":
        if args.enable:
            _require_confirmation(args)
            enable_arm()
        _print_health(arm)
        return

    if args.command == "is-enabled":
        print(json.dumps(arm.enable_status(), ensure_ascii=False, indent=2, sort_keys=True))
        return

    if args.command == "read":
        _require_confirmation(args)
        enable_arm()
        print("关节角 (rad):", " ".join("%.6f" % value for value in arm.read_joint_pose().values_rad))
        return

    if args.command == "enable":
        _require_confirmation(args)
        enable_arm()
        _print_health(arm)
        return

    if args.command == "move-j":
        _require_confirmation(args)
        target = JointPose.from_values(args.joints)
        enable_arm()
        arm.move_j(target, args.duration)
        print("MoveJ 完成，关节角 (rad):", " ".join("%.6f" % value for value in arm.read_joint_pose().values_rad))
        return

    if args.command == "move-p-by-j":
        _require_confirmation(args)
        target = BasePose.from_values(args.pose)
        print(
            "[INFO] move-p-by-j: 目标位姿 (m, rad):",
            " ".join("%.6f" % value for value in target.values()),
            flush=True,
        )
        print("[INFO] move-p-by-j: 正在使能机械臂并检查反馈。", flush=True)
        enable_arm()
        print("[INFO] move-p-by-j: 使能成功，正在逆解；逆解成功后执行 MoveJ。", flush=True)
        arm.move_to_pose_by_move_j(target, args.duration)
        print("[OK] move-p-by-j: 逆解及 MoveJ 运动完成。", flush=True)
        print("关节角 (rad):", " ".join("%.6f" % value for value in arm.read_joint_pose().values_rad))
        return

    if args.command in {"move-pose", "move-l"}:
        _require_confirmation(args)
        target = BasePose.from_values(args.pose)
        enable_arm()
        if args.command == "move-pose":
            arm.move_to_pose_by_move_j(target, args.duration)
            print("MovePByJ 位姿点到点运动完成")
        else:
            arm.move_l(target, args.duration)
            print("笛卡尔直线运动完成")
        print("关节角 (rad):", " ".join("%.6f" % value for value in arm.read_joint_pose().values_rad))
        return

    if args.command in {"gripper-open", "gripper-close"}:
        _require_confirmation(args)
        closing = args.command == "gripper-close"
        command = _make_gripper_command(args, closing=closing)
        enable_arm()
        if closing:
            result = arm.close_gripper_monitored(command)
            reason = str(result["reason"])
            if reason == "target_reached":
                print("夹爪关闭并确认到位")
            elif reason == "object_contact":
                print("夹爪检测到书本接触，已锁定当前夹持位置")
            else:
                print("夹爪闭合监测超时，已锁定当前保持位置")
        else:
            arm.open_gripper(command.open_deg, command)
            print("夹爪打开命令已发送")
        return

    if args.command == "disable":
        _require_confirmation(args)
        result = arm.disable_at_safe_pose()
        print("[OK] 机械臂已到达失能位并发送失能命令：%s" % json.dumps(result, ensure_ascii=False))
        return

    raise ValueError("unsupported command: %s" % args.command)


def _confirm_disable_after_enable() -> bool:
    """Ask whether to disable after this CLI session has enabled the arm."""

    if not sys.stdin.isatty():
        print("非交互终端无法确认失能；机械臂保持当前使能状态。", file=sys.stderr)
        return False
    try:
        reply = input("本次运行已尝试使能机械臂。是否在退出前失能？[y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print("\n未收到失能确认；机械臂保持当前使能状态。", file=sys.stderr)
        return False
    return reply.strip().lower() in {"y", "yes"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if _command_requires_confirmation(args) and not args.confirm:
        print(
            "执行失败: this command can change motor state or move the real arm; rerun it with --confirm",
            file=sys.stderr,
        )
        return 2
    try:
        config = build_device_config(args)
    except ValueError as error:
        print("配置错误: %s" % error, file=sys.stderr)
        return 2

    arm = ArmService(config)
    started = False
    enable_attempted = False

    def enable_arm() -> None:
        nonlocal enable_attempted
        enable_attempted = True
        arm.enable()

    try:
        # Mark lifecycle ownership before start so a failed connection still stops its worker.
        started = True
        arm.start()
        _run_command(args, arm, enable_arm)
        return 0
    except KeyboardInterrupt:
        arm.interrupt_motion()
        print("\n已由用户中断，正在关闭通信；电机保持当前使能状态。", file=sys.stderr)
        return 130
    except Exception as error:
        print("[FAIL] %s: %s" % (args.command, error), file=sys.stderr)
        return 1
    finally:
        if started:
            try:
                if enable_attempted and _confirm_disable_after_enable():
                    result = arm.disable_at_safe_pose()
                    print("[OK] 机械臂已到达失能位并发送失能命令：%s" % json.dumps(result, ensure_ascii=False))
                arm.close()
            except Exception as error:
                print("关闭机械臂通信失败: %s" % error, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
