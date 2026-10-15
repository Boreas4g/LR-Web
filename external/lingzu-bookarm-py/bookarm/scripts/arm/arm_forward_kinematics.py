#!/usr/bin/env python3
"""根据给定的六轴关节构型计算机械臂末端正运动学位姿。

此脚本只加载 URDF 并计算，不连接 CAN/SLCAN，也不会使能或移动机械臂。

从仓库根目录运行：
    python3 scripts/arm/arm_forward_kinematics.py --joints 0 0 0 0 0 0

指定工具中心点（TCP）偏移：
    python3 scripts/arm/arm_forward_kinematics.py \
        --joints 0 0.610865 -0.785398 0 0 0 \
        --tcp-offset 0 0 0.10 0 0 0
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Optional, Sequence


# 支持在未安装 bookarm 包的源码仓库中直接运行。
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from bookarm.devices.arm import DEFAULT_TCP_OFFSET
from bookarm.devices.arm.kinematics import PinocchioKinematics
from bookarm.domain.models.pose import JointPose


DEFAULT_URDF_PATH = SOURCE_ROOT / "bookarm" / "resources" / "urdf" / "el_a3.urdf"


def finite_float(value: str) -> float:
    """Parse a finite floating-point command-line value."""

    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    return parsed


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="根据六轴关节构型计算 EL-A3 末端位姿；不连接或控制实机。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--joints",
        type=finite_float,
        nargs=6,
        required=True,
        metavar=("J1", "J2", "J3", "J4", "J5", "J6"),
        help="六轴关节构型，单位 rad。",
    )
    parser.add_argument(
        "--urdf-path",
        type=Path,
        default=DEFAULT_URDF_PATH,
        help="机械臂 URDF 路径。",
    )
    parser.add_argument(
        "--tcp-offset",
        type=finite_float,
        nargs=6,
        metavar=("X", "Y", "Z", "RX", "RY", "RZ"),
        help="相对 end_effector 的 TCP 偏移，单位 m 和 rad；默认使用 BookArm 标定 TCP。",
    )
    return parser.parse_args(argv)


def format_result(joints: JointPose, pose) -> dict[str, object]:
    """Build an explicit unit-labelled result for terminal output."""

    pose_values = pose.values()
    return {
        "joint_configuration_rad": list(joints.values_rad),
        "end_effector_base_pose_m_rad": list(pose_values),
        "position_m": {"x": pose.x_m, "y": pose.y_m, "z": pose.z_m},
        "orientation_rpy_rad": {"rx": pose.rx_rad, "ry": pose.ry_rad, "rz": pose.rz_rad},
        "orientation_rpy_deg": {
            "roll": math.degrees(pose.rx_rad),
            "pitch": math.degrees(pose.ry_rad),
            "yaw": math.degrees(pose.rz_rad),
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        joints = JointPose.from_values(args.joints)
        tcp_offset = DEFAULT_TCP_OFFSET if args.tcp_offset is None else tuple(args.tcp_offset)
        kinematics = PinocchioKinematics(args.urdf_path, tcp_offset)
        pose = kinematics.forward_kinematics(joints)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print("正运动学计算失败: %s" % error, file=sys.stderr)
        return 1

    print(json.dumps(format_result(joints, pose), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
